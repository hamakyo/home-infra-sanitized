#!/usr/bin/env python3
"""Disaster Recovery (DR) restore helper for ubuntu-desktop infrastructure.

Restores encrypted snapshots from Backblaze B2 (backup-home-infra-example/infra-db) via restic,
verifies every file's SHA-256 against manifest.json, and optionally deploys .env files
and restores all 7 databases with least-privilege PostgreSQL grants intact.

Credentials resolution order:
  1. Environment variables (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, RESTIC_REPOSITORY, RESTIC_PASSWORD)
     — intended for a brand-new DR host before stacks/backup/.env exists.
  2. Fallback to stacks/backup/.env (chmod 600) when present.

Safe usage examples:
  # Read-only verification drill (safe to run anytime on live host):
  python3 stacks/backup/restore_dr.py --phase verify

  # Step-by-step DR on a fresh Ubuntu host:
  python3 stacks/backup/restore_dr.py --phase fetch --workdir /tmp/dr-restore
  python3 stacks/backup/restore_dr.py --phase env --workdir /tmp/dr-restore --tailscale-ip 100.x.y.z
  python3 stacks/backup/restore_dr.py --phase db --workdir /tmp/dr-restore --confirm-db-overwrite
  rm -rf /tmp/dr-restore
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
DEFAULT_ENV_FILE = SCRIPT_DIR / ".env"
DEFAULT_RESTIC_BIN = Path.home() / ".local" / "bin" / "restic"

STACK_ORDER = (
    "caddy",
    "authentik",
    "zabbix",
    "netbox",
    "fleet",
    "data-platform",
    "n8n",
    "opencode",
)

DB_SERVICES_BY_STACK = (
    ("zabbix", ["zabbix-postgres"]),
    ("netbox", ["netbox-postgres"]),
    ("authentik", ["authentik-postgres"]),
    ("fleet", ["fleet-mysql"]),
    ("data-platform", ["infra-dwh-postgres"]),
)

APP_CONTAINERS_TO_STOP = (
    "zabbix-server",
    "zabbix-web",
    "netbox",
    "netbox-worker",
    "authentik-server",
    "authentik-worker",
    "fleet",
    "infra-dwh-grafana",
    "infra-rag-mcp",
    "n8n",
)

TAILSCALE_IP_RE = re.compile(r"^100\.(6[4-9]|[7-9]\d|1[0-1]\d|12[0-7])\.\d{1,3}\.\d{1,3}$")
ALLOWED_ENV_REL_RE = re.compile(r"^stacks/[a-z0-9-]+(?:/[a-z0-9-]+)?/\.env$")


def resolve_under(base_dir: Path, rel_path: str, *, label: str) -> Path:
    """Resolve a manifest-supplied relative path and verify it stays strictly under base_dir."""
    if not isinstance(rel_path, str) or not rel_path or rel_path.startswith("/") or "\\" in rel_path:
        raise ValueError(f"Invalid relative path for {label}: {rel_path!r}")
    base = base_dir.resolve()
    candidate = (base / rel_path).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"Path traversal rejected for {label}: {rel_path!r}") from exc
    if candidate == base:
        raise ValueError(f"Path must point inside {label}, not root: {rel_path!r}")
    return candidate


def validate_workdir(workdir: Path) -> Path:
    """Ensure --workdir is a dedicated subdirectory under system temp and outside REPO_ROOT."""
    resolved = workdir.resolve()
    repo_resolved = REPO_ROOT.resolve()
    allowed_tmp_roots = {Path("/tmp").resolve(), Path(tempfile.gettempdir()).resolve()}
    if not any(resolved != root and resolved.is_relative_to(root) for root in allowed_tmp_roots):
        raise ValueError(
            f"Refusing unsafe --workdir outside temp directory: {resolved} "
            f"(expected a subdirectory under /tmp or {tempfile.gettempdir()})"
        )
    if resolved == repo_resolved or resolved.is_relative_to(repo_resolved) or repo_resolved.is_relative_to(resolved):
        raise ValueError(f"Refusing --workdir overlapping repository root: {resolved}")
    return resolved


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_env_file(path: Path) -> dict[str, str]:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise PermissionError(f"Insecure permissions ({oct(mode)}) on {path}; run chmod 600")
    cfg: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        cfg[k] = v
    return cfg


def build_restic_env(env_file: Path) -> tuple[dict[str, str], str]:
    file_cfg: dict[str, str] = {}
    if env_file.exists():
        file_cfg = load_env_file(env_file)

    env = os.environ.copy()
    required = (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "RESTIC_REPOSITORY",
        "RESTIC_PASSWORD",
    )
    missing = []
    for key in required:
        val = env.get(key) or file_cfg.get(key, "")
        if not val or val.startswith("replace-with-"):
            missing.append(key)
        else:
            env[key] = val
    if missing:
        raise ValueError(
            "Missing restic/B2 credentials (set env vars or create stacks/backup/.env): "
            + ", ".join(missing)
        )

    restic_bin = env.get("RESTIC_BIN") or file_cfg.get("RESTIC_BIN") or str(DEFAULT_RESTIC_BIN)
    if not Path(restic_bin).exists():
        which_bin = shutil.which("restic")
        if which_bin:
            restic_bin = which_bin
        else:
            raise FileNotFoundError(f"restic binary not found at {restic_bin} or in PATH")
    return env, restic_bin


def locate_staging_dir(workdir: Path) -> Path:
    candidates = list(workdir.rglob("manifest.json"))
    if not candidates:
        raise FileNotFoundError(f"manifest.json not found under {workdir}")
    if len(candidates) > 1:
        raise RuntimeError(f"Multiple manifest.json files found under {workdir}: {candidates}")
    return candidates[0].parent


def fetch_snapshot(workdir: Path, snapshot: str, env_file: Path) -> Path:
    workdir = validate_workdir(workdir)
    env, restic_bin = build_restic_env(env_file)
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, mode=0o700)
    os.chmod(workdir, 0o700)

    proc = subprocess.run(
        [restic_bin, "restore", snapshot, "--target", str(workdir)],
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"restic restore failed (rc={proc.returncode}): {proc.stderr.strip()}")
    return locate_staging_dir(workdir)


def verify_staging(staging_dir: Path) -> dict[str, object]:
    manifest = json.loads((staging_dir / "manifest.json").read_text(encoding="utf-8"))

    verified_dbs: list[str] = []
    for name, info in manifest.get("databases", {}).items():
        path = resolve_under(staging_dir, info["file"], label=f"db:{name}")
        if not path.is_file():
            raise FileNotFoundError(f"Missing DB dump in snapshot: {path}")
        actual = sha256_file(path)
        if actual != info["sha256"]:
            raise RuntimeError(f"SHA-256 mismatch for DB {name}: expected {info['sha256']}, got {actual}")
        verified_dbs.append(name)

    verified_envs: list[str] = []
    for item in manifest.get("env_files", []):
        rel_dest = str(item.get("path", ""))
        if not ALLOWED_ENV_REL_RE.match(rel_dest):
            raise ValueError(f"Refusing unexpected .env destination path in manifest: {rel_dest!r}")
        resolve_under(REPO_ROOT, rel_dest, label="env_dest")
        path = resolve_under(staging_dir, item["staged"], label=f"env_staged:{rel_dest}")
        if not path.is_file():
            raise FileNotFoundError(f"Missing .env in snapshot: {path}")
        actual = sha256_file(path)
        if actual != item["sha256"]:
            raise RuntimeError(f"SHA-256 mismatch for {rel_dest}")
        verified_envs.append(rel_dest)

    host_cfg = manifest.get("host_configs", {})
    verified_etc = 0
    for item in host_cfg.get("etc_files", []):
        path = resolve_under(staging_dir, item["staged"], label="etc_staged")
        if not path.is_file():
            raise FileNotFoundError(f"Missing host config in snapshot: {path}")
        if sha256_file(path) != item["sha256"]:
            raise RuntimeError(f"SHA-256 mismatch for {item['source']}")
        verified_etc += 1

    for key in ("crontab", "ssh_authorized_keys", "host_meta"):
        entry = host_cfg.get(key)
        if entry:
            path = resolve_under(staging_dir, entry["staged"], label=f"host:{key}")
            if not path.is_file():
                raise FileNotFoundError(f"Missing host item {key}: {path}")
            if sha256_file(path) != entry["sha256"]:
                raise RuntimeError(f"SHA-256 mismatch for host item {key}")

    return {
        "created_at_jst": manifest.get("created_at_jst"),
        "source_host": manifest.get("host"),
        "databases_verified": verified_dbs,
        "env_files_verified": len(verified_envs),
        "etc_files_verified": verified_etc,
        "crontab_verified": bool(host_cfg.get("crontab")),
        "authorized_keys_verified": bool(host_cfg.get("ssh_authorized_keys")),
        "host_meta_verified": bool(host_cfg.get("host_meta")),
    }


def update_env_key(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    replaced = False
    out: list[str] = []
    for line in lines:
        if line.startswith(f"{key}="):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def restore_env_files(
    staging_dir: Path,
    *,
    force: bool = False,
    tailscale_ip: str | None = None,
) -> dict[str, object]:
    if tailscale_ip and not TAILSCALE_IP_RE.match(tailscale_ip):
        raise ValueError(f"Invalid Tailscale IPv4 address (expected 100.64.0.0/10): {tailscale_ip}")

    manifest = json.loads((staging_dir / "manifest.json").read_text(encoding="utf-8"))
    env_items = manifest.get("env_files", [])
    if not env_items:
        raise RuntimeError("Snapshot manifest contains no env_files (use a snapshot from DR Step 1 or later)")

    restored: list[str] = []
    skipped: list[str] = []
    for item in env_items:
        rel_dest = str(item.get("path", ""))
        if not ALLOWED_ENV_REL_RE.match(rel_dest):
            raise ValueError(f"Refusing unexpected .env destination path in manifest: {rel_dest!r}")
        src = resolve_under(staging_dir, item["staged"], label=f"env_staged:{rel_dest}")
        if sha256_file(src) != item["sha256"]:
            raise RuntimeError(f"SHA-256 mismatch before copying {rel_dest}")
        dest = resolve_under(REPO_ROOT, rel_dest, label="env_dest")
        if dest.exists() and not force:
            skipped.append(rel_dest)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        os.chmod(dest, 0o600)
        restored.append(rel_dest)

    ip_updated: list[str] = []
    if tailscale_ip:
        for rel, key in (
            ("stacks/caddy/.env", "CADDY_BIND_IP"),
            ("stacks/zabbix/.env", "ZBX_BIND_IP"),
        ):
            target = REPO_ROOT / rel
            if target.is_file():
                update_env_key(target, key, tailscale_ip)
                ip_updated.append(f"{rel}:{key}")

    return {
        "restored": restored,
        "skipped_existing": skipped,
        "tailscale_ip_updated": ip_updated,
    }


def run_cmd(cmd: list[str], *, cwd: Path | None = None, stdin_path: Path | None = None, input_text: str | None = None) -> None:
    if stdin_path is not None:
        with stdin_path.open("rb") as f:
            proc = subprocess.run(cmd, cwd=cwd, stdin=f, capture_output=True)
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Command failed ({' '.join(cmd)}): {err}")
        return

    proc = subprocess.run(cmd, cwd=cwd, input=input_text, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed ({' '.join(cmd)}): {(proc.stderr or proc.stdout).strip()}")


def wait_container_healthy(container: str, timeout_sec: int = 90) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        proc = subprocess.run(
            ["docker", "inspect", "--format", "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}", container],
            capture_output=True,
            text=True,
        )
        status = proc.stdout.strip()
        if proc.returncode == 0 and status in ("healthy", "running"):
            return
        time.sleep(2)
    raise TimeoutError(f"Container {container} did not become healthy within {timeout_sec}s")


def recreate_pg_db(container: str, superuser: str, dbname: str, owner: str) -> None:
    sql = (
        f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        f"WHERE datname = '{dbname}' AND pid <> pg_backend_pid();\n"
        f"DROP DATABASE IF EXISTS {dbname};\n"
        f"CREATE DATABASE {dbname} OWNER {owner};\n"
    )
    run_cmd(
        ["docker", "exec", "-i", container, "psql", "-v", "ON_ERROR_STOP=1", "-U", superuser, "-d", "postgres"],
        input_text=sql,
    )


def restore_databases(staging_dir: Path) -> dict[str, object]:
    verify_staging(staging_dir)
    manifest = json.loads((staging_dir / "manifest.json").read_text(encoding="utf-8"))

    # 1. Ensure DB containers are up
    for stack, services in DB_SERVICES_BY_STACK:
        run_cmd(["docker", "compose", "up", "-d", *services], cwd=REPO_ROOT / "stacks" / stack)
        for svc in services:
            wait_container_healthy(svc)

    # 2. Stop app containers to release DB locks cleanly
    for cname in APP_CONTAINERS_TO_STOP:
        subprocess.run(["docker", "stop", cname], capture_output=True, text=True)

    restored_dbs: list[str] = []

    # 3. Single-DB PostgreSQL stacks (zabbix, netbox, authentik)
    for name, container, user, db in (
        ("zabbix", "zabbix-postgres", "zabbix", "zabbix"),
        ("netbox", "netbox-postgres", "netbox", "netbox"),
        ("authentik", "authentik-postgres", "authentik", "authentik"),
    ):
        sql_path = staging_dir / manifest["databases"][name]["file"]
        recreate_pg_db(container, user, db, user)
        run_cmd(
            ["docker", "exec", "-i", container, "psql", "-v", "ON_ERROR_STOP=1", "-U", user, "-d", db],
            stdin_path=sql_path,
        )
        restored_dbs.append(name)

    # 4. Multi-DB PostgreSQL 17 (infra_dwh, grafana_app, n8n_app) with least-privilege grants
    infra_sql = staging_dir / manifest["databases"]["infra_dwh"]["file"]
    recreate_pg_db("infra-dwh-postgres", "postgres", "infra_dwh", "postgres")
    run_cmd(
        ["docker", "exec", "-i", "infra-dwh-postgres", "psql", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "infra_dwh"],
        stdin_path=infra_sql,
    )
    infra_grants = """
REVOKE ALL ON DATABASE infra_dwh FROM PUBLIC;
GRANT CONNECT ON DATABASE infra_dwh TO etl_writer, bi_reader, rag_reader;
GRANT CREATE ON DATABASE infra_dwh TO etl_writer;
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT schemaname, tablename FROM pg_tables WHERE schemaname IN ('raw','stg','mart','rag') LOOP
    EXECUTE format('ALTER TABLE %I.%I OWNER TO etl_writer', r.schemaname, r.tablename);
  END LOOP;
  FOR r IN SELECT sequence_schema, sequence_name FROM information_schema.sequences WHERE sequence_schema IN ('raw','stg','mart','rag') LOOP
    EXECUTE format('ALTER SEQUENCE %I.%I OWNER TO etl_writer', r.sequence_schema, r.sequence_name);
  END LOOP;
END$$;
GRANT ALL PRIVILEGES ON SCHEMA raw, stg, mart, rag TO etl_writer;
REVOKE ALL ON SCHEMA raw, stg, public FROM bi_reader, rag_reader;
GRANT USAGE ON SCHEMA mart, rag TO bi_reader, rag_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA mart TO bi_reader, rag_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA rag TO rag_reader;
GRANT SELECT ON rag.documents, rag.eval_runs, rag.eval_results TO bi_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE etl_writer IN SCHEMA mart GRANT SELECT ON TABLES TO bi_reader, rag_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE etl_writer IN SCHEMA rag GRANT SELECT ON TABLES TO rag_reader;
"""
    run_cmd(
        ["docker", "exec", "-i", "infra-dwh-postgres", "psql", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "infra_dwh"],
        input_text=infra_grants,
    )
    restored_dbs.append("infra_dwh")

    # grafana_app
    grafana_sql = staging_dir / manifest["databases"]["grafana_app"]["file"]
    recreate_pg_db("infra-dwh-postgres", "postgres", "grafana_app", "grafana_app")
    run_cmd(
        ["docker", "exec", "-i", "infra-dwh-postgres", "psql", "-v", "ON_ERROR_STOP=1", "-U", "grafana_app", "-d", "grafana_app"],
        stdin_path=grafana_sql,
    )
    run_cmd(
        ["docker", "exec", "-i", "infra-dwh-postgres", "psql", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "postgres"],
        input_text="REVOKE ALL ON DATABASE grafana_app FROM PUBLIC; GRANT CONNECT ON DATABASE grafana_app TO grafana_app;\n",
    )
    restored_dbs.append("grafana_app")

    # n8n_app (with isolated n8n_ro read-only role)
    n8n_sql = staging_dir / manifest["databases"]["n8n_app"]["file"]
    recreate_pg_db("infra-dwh-postgres", "postgres", "n8n_app", "n8n_app")
    run_cmd(
        ["docker", "exec", "-i", "infra-dwh-postgres", "psql", "-v", "ON_ERROR_STOP=1", "-U", "n8n_app", "-d", "n8n_app"],
        stdin_path=n8n_sql,
    )
    n8n_grants = """
REVOKE ALL ON DATABASE n8n_app FROM PUBLIC;
GRANT ALL ON DATABASE n8n_app TO n8n_app;
GRANT CONNECT ON DATABASE n8n_app TO n8n_ro;
GRANT USAGE ON SCHEMA public TO n8n_ro;
REVOKE CREATE ON SCHEMA public FROM n8n_ro;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM n8n_ro;
ALTER ROLE n8n_ro IN DATABASE n8n_app SET default_transaction_read_only = on;
DO $$
BEGIN
  IF to_regclass('public.workflow_entity') IS NOT NULL THEN
    GRANT SELECT ON TABLE public.workflow_entity TO n8n_ro;
  END IF;
  IF to_regclass('public.execution_entity') IS NOT NULL THEN
    GRANT SELECT ON TABLE public.execution_entity TO n8n_ro;
  END IF;
  IF to_regclass('public.credentials_entity') IS NOT NULL THEN
    REVOKE ALL ON TABLE public.credentials_entity FROM n8n_ro;
  END IF;
  IF to_regclass('public.execution_data') IS NOT NULL THEN
    REVOKE ALL ON TABLE public.execution_data FROM n8n_ro;
  END IF;
END
$$;
"""
    run_cmd(
        ["docker", "exec", "-i", "infra-dwh-postgres", "psql", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "n8n_app"],
        input_text=n8n_grants,
    )
    restored_dbs.append("n8n_app")

    # 5. Fleet MySQL 8.4
    fleet_sql = staging_dir / manifest["databases"]["fleet"]["file"]
    run_cmd(
        [
            "docker",
            "exec",
            "-i",
            "fleet-mysql",
            "sh",
            "-c",
            'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -u root -e '
            '"DROP DATABASE IF EXISTS fleet; CREATE DATABASE fleet CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"',
        ]
    )
    run_cmd(
        [
            "docker",
            "exec",
            "-i",
            "fleet-mysql",
            "sh",
            "-c",
            'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -u root fleet',
        ],
        stdin_path=fleet_sql,
    )
    restored_dbs.append("fleet")

    # 6. Start all 8 stacks in dependency order
    for stack in STACK_ORDER:
        run_cmd(["docker", "compose", "up", "-d"], cwd=REPO_ROOT / "stacks" / stack)

    # 7. Restore NetBox media/reports/scripts directories into running netbox container
    wait_container_healthy("netbox", timeout_sec=120)
    copied_netbox: list[str] = []
    for sub in ("media", "reports", "scripts"):
        src_dir = staging_dir / "netbox" / sub
        if src_dir.is_dir():
            run_cmd(["docker", "cp", f"{src_dir}/.", f"netbox:/opt/netbox/netbox/{sub}/"])
            copied_netbox.append(f"netbox/{sub}")

    return {
        "databases_restored": restored_dbs,
        "netbox_dirs_restored": copied_netbox,
        "stacks_started": list(STACK_ORDER),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="DR restore helper for ubuntu-desktop infrastructure")
    parser.add_argument(
        "--phase",
        choices=("verify", "fetch", "env", "db"),
        default="verify",
        help="verify (read-only drill), fetch (download+verify to workdir), env (restore .env files), db (restore all 7 DBs and start stacks)",
    )
    parser.add_argument("--snapshot", default="latest", help="restic snapshot ID (default: latest)")
    parser.add_argument("--workdir", type=Path, default=None, help="staging directory for fetch/env/db phases")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE, help="fallback backup .env file")
    parser.add_argument("--tailscale-ip", default=None, help="optional new Tailscale IP to sync into caddy/zabbix .env during --phase env")
    parser.add_argument("--force", action="store_true", help="allow overwriting existing .env files during --phase env")
    parser.add_argument(
        "--confirm-db-overwrite",
        action="store_true",
        help="required safety confirmation before dropping and restoring live databases in --phase db",
    )
    args = parser.parse_args()

    os.umask(0o077)

    if args.phase == "verify":
        tmp = Path(tempfile.mkdtemp(prefix="dr-verify-"))
        os.chmod(tmp, 0o700)
        try:
            staging = fetch_snapshot(tmp / "restore", args.snapshot, args.env_file)
            summary = verify_staging(staging)
            print(json.dumps({"phase": "verify", "status": "ok", "snapshot": args.snapshot, **summary}, ensure_ascii=False, indent=2))
            return 0
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    workdir = args.workdir or Path("/tmp/dr-restore")

    if args.phase == "fetch":
        staging = fetch_snapshot(workdir, args.snapshot, args.env_file)
        summary = verify_staging(staging)
        print(
            json.dumps(
                {"phase": "fetch", "status": "ok", "workdir": str(workdir), "staging": str(staging), **summary},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.phase == "db" and not args.confirm_db_overwrite:
        print(
            "Refusing to overwrite databases without --confirm-db-overwrite.",
            file=sys.stderr,
        )
        return 2

    staging = locate_staging_dir(workdir)

    if args.phase == "env":
        result = restore_env_files(staging, force=args.force, tailscale_ip=args.tailscale_ip)
        print(json.dumps({"phase": "env", "status": "ok", **result}, ensure_ascii=False, indent=2))
        return 0

    if args.phase == "db":
        result = restore_databases(staging)
        print(json.dumps({"phase": "db", "status": "ok", **result}, ensure_ascii=False, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
