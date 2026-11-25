#!/usr/bin/env python3
"""Daily encrypted backup of ubuntu-desktop infrastructure state to Backblaze B2 via restic.

Targets on ubuntu-desktop:
- Databases (7):
  - Zabbix 8.0 LTS (PostgreSQL 16: pg_dump)
  - NetBox v4.7 (PostgreSQL 18: pg_dump + media/reports/scripts directories)
  - Authentik 2026.8.5 (PostgreSQL 16: pg_dump)
  - Fleet Free v4.95.0 (MySQL 8.4: mysqldump)
  - Infra DWH / Grafana / n8n (PostgreSQL 17: infra_dwh, grafana_app, n8n_app)
- Stack secret files (stacks/**/.env, permissions preserved as 0600 in staging)
- Host recovery configs (user crontab, ~/.ssh/authorized_keys, custom /etc/ files, container metadata)

Usage:
  python3 stacks/backup/backup_phase1_db.py --dry-run
  python3 stacks/backup/backup_phase1_db.py --init
  python3 stacks/backup/backup_phase1_db.py
  python3 stacks/backup/backup_phase1_db.py --check
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
DEFAULT_ENV_FILE = SCRIPT_DIR / ".env"
STATE_DIR = Path.home() / ".local" / "state" / "infra-backup"
STAGING_DIR = STATE_DIR / "staging"
RUNS_DIR = STATE_DIR / "runs"
LOCK_FILE = STATE_DIR / "backup.lock"
LOG_RETENTION_DAYS = 30
MIN_DUMP_BYTES = 1024

HOST_ETC_FILES = (
    "/etc/NetworkManager/dispatcher.d/99-eno1-e1000e-tuning",
    "/etc/apt/apt.conf.d/52unattended-upgrades-local",
    "/etc/avahi/services/samba.service",
    "/etc/fstab",
    "/etc/modprobe.d/usb-ssd-disable-uas.conf",
    "/etc/samba/smb.conf",
    "/etc/ssh/sshd_config.d/10-hardening.conf",
    "/etc/sysctl.d/99-tailscale-nonlocal-bind.conf",
    "/etc/systemd/system/docker.service.d/10-after-tailscaled.conf",
    "/etc/systemd/system/smbd.service.d/10-after-tailscaled.conf",
    "/etc/udev/rules.d/99-usb-ssd-no-autosuspend.rules",
    "/etc/zabbix/zabbix_agent2.d/10-home-infra.conf",
)

JST = dt.timezone(dt.timedelta(hours=9))


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        raise FileNotFoundError(f"Env file not found: {path}")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise PermissionError(
            f"Env file {path} has insecure permissions ({oct(mode)}); run chmod 600 {path}"
        )
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        env[key] = val
    return env


def sanitize_text(text: str, secrets: list[str]) -> str:
    out = text
    for secret in secrets:
        if secret and len(secret) >= 4:
            out = out.replace(secret, "***REDACTED***")
    return out


def validate_restic_env(cfg: dict[str, str]) -> None:
    required = (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "RESTIC_REPOSITORY",
        "RESTIC_PASSWORD",
    )
    missing = []
    for key in required:
        val = cfg.get(key, "").strip()
        if not val or val.startswith("replace-with-"):
            missing.append(key)
    if missing:
        raise ValueError(
            "Required backup credentials are unset or still placeholders in .env: "
            + ", ".join(missing)
        )


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def dir_size_bytes(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        if p.is_file() and not p.is_symlink():
            total += p.stat().st_size
    return total


def container_image(container: str) -> str:
    proc = subprocess.run(
        ["docker", "inspect", "--format", "{{.Config.Image}}", container],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def run_dump_to_file(cmd: list[str], out_path: Path) -> dict[str, object]:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    with out_path.open("wb") as f:
        proc = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE)
    elapsed = round(time.monotonic() - t0, 2)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Dump failed for {out_path.name} (rc={proc.returncode}): {err}")
    size = out_path.stat().st_size
    if size < MIN_DUMP_BYTES:
        raise RuntimeError(f"Dump file {out_path.name} is unexpectedly small ({size} bytes)")
    return {
        "file": str(out_path.relative_to(STAGING_DIR)),
        "bytes": size,
        "sha256": sha256_file(out_path),
        "duration_sec": elapsed,
    }


def copy_netbox_dirs(target_root: Path) -> list[dict[str, object]]:
    copied: list[dict[str, object]] = []
    for sub in ("media", "reports", "scripts"):
        dest = target_root / sub
        if dest.exists():
            shutil.rmtree(dest)
        proc = subprocess.run(
            ["docker", "cp", f"netbox:/opt/netbox/netbox/{sub}", str(dest)],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"docker cp failed for netbox/{sub}: {proc.stderr.strip()}")
        copied.append({"dir": f"netbox/{sub}", "bytes": dir_size_bytes(dest)})
    return copied


def collect_stack_env_files(target_root: Path) -> list[dict[str, object]]:
    """Copy all stacks/**/.env files into 0700/0600 staging without logging secret values."""
    stacks_dir = REPO_ROOT / "stacks"
    env_files = sorted(
        p for p in stacks_dir.rglob(".env") if p.is_file() and not p.is_symlink()
    )
    if not env_files:
        raise RuntimeError(f"No stack .env files found under {stacks_dir}")

    collected: list[dict[str, object]] = []
    target_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target_root, 0o700)

    for src in env_files:
        rel = src.relative_to(REPO_ROOT)
        dest = target_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(src, dest)
        os.chmod(dest, 0o600)
        collected.append(
            {
                "path": str(rel),
                "staged": str(dest.relative_to(STAGING_DIR)),
                "bytes": dest.stat().st_size,
                "sha256": sha256_file(dest),
            }
        )
    return collected


def read_os_pretty_name() -> str:
    os_release = Path("/etc/os-release")
    if not os_release.exists():
        return "unknown"
    for line in os_release.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"')
    return "unknown"


def list_running_containers() -> list[dict[str, str]]:
    proc = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}\t{{.Image}}"],
        capture_output=True,
        text=True,
        check=True,
    )
    items: list[dict[str, str]] = []
    for line in sorted(proc.stdout.splitlines()):
        if "\t" not in line:
            continue
        name, image = line.split("\t", 1)
        items.append({"name": name, "image": image})
    return items


def collect_host_configs(target_root: Path) -> dict[str, object]:
    """Collect user crontab, SSH authorized_keys, custom /etc/ configs, and host metadata."""
    target_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target_root, 0o700)

    info: dict[str, object] = {
        "crontab": None,
        "ssh_authorized_keys": None,
        "etc_files": [],
        "missing_etc_files": [],
    }

    cron_proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if cron_proc.returncode == 0:
        cron_out = target_root / "crontab.txt"
        cron_out.write_text(cron_proc.stdout, encoding="utf-8")
        os.chmod(cron_out, 0o600)
        info["crontab"] = {
            "staged": str(cron_out.relative_to(STAGING_DIR)),
            "bytes": cron_out.stat().st_size,
            "sha256": sha256_file(cron_out),
            "lines": len([l for l in cron_proc.stdout.splitlines() if l.strip() and not l.strip().startswith("#")]),
        }
    elif "no crontab" not in cron_proc.stderr.lower():
        raise RuntimeError(f"crontab -l failed (rc={cron_proc.returncode}): {cron_proc.stderr.strip()}")

    auth_keys = Path.home() / ".ssh" / "authorized_keys"
    if auth_keys.is_file() and not auth_keys.is_symlink():
        dest = target_root / "ssh" / "authorized_keys"
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(auth_keys, dest)
        os.chmod(dest, 0o600)
        info["ssh_authorized_keys"] = {
            "staged": str(dest.relative_to(STAGING_DIR)),
            "bytes": dest.stat().st_size,
            "sha256": sha256_file(dest),
        }

    etc_copied: list[dict[str, object]] = []
    etc_missing: list[str] = []
    for raw_path in HOST_ETC_FILES:
        src = Path(raw_path)
        if not src.is_file():
            etc_missing.append(raw_path)
            continue
        dest = target_root / src.relative_to("/")
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(src, dest)
        os.chmod(dest, 0o600)
        etc_copied.append(
            {
                "source": raw_path,
                "staged": str(dest.relative_to(STAGING_DIR)),
                "bytes": dest.stat().st_size,
                "sha256": sha256_file(dest),
            }
        )
    info["etc_files"] = etc_copied
    info["missing_etc_files"] = etc_missing

    meta = {
        "captured_at_jst": dt.datetime.now(JST).isoformat(timespec="seconds"),
        "hostname": os.uname().nodename,
        "kernel": os.uname().release,
        "os_pretty_name": read_os_pretty_name(),
        "containers": list_running_containers(),
    }
    meta_out = target_root / "host_meta.json"
    meta_out.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(meta_out, 0o600)
    info["host_meta"] = {
        "staged": str(meta_out.relative_to(STAGING_DIR)),
        "bytes": meta_out.stat().st_size,
        "sha256": sha256_file(meta_out),
        "container_count": len(meta["containers"]),
    }
    return info


def dump_all_targets() -> dict[str, object]:
    if STAGING_DIR.exists():
        shutil.rmtree(STAGING_DIR)
    STAGING_DIR.mkdir(parents=True, mode=0o700)
    os.chmod(STAGING_DIR, 0o700)

    manifest: dict[str, object] = {
        "created_at_jst": dt.datetime.now(JST).isoformat(timespec="seconds"),
        "host": os.uname().nodename,
        "databases": {},
        "directories": [],
        "env_files": [],
        "host_configs": {},
    }

    pg_targets = [
        ("zabbix", "zabbix-postgres", "zabbix", "zabbix"),
        ("netbox", "netbox-postgres", "netbox", "netbox"),
        ("authentik", "authentik-postgres", "authentik", "authentik"),
        ("infra_dwh", "infra-dwh-postgres", "postgres", "infra_dwh"),
        ("grafana_app", "infra-dwh-postgres", "postgres", "grafana_app"),
        ("n8n_app", "infra-dwh-postgres", "postgres", "n8n_app"),
    ]
    for name, container, user, db in pg_targets:
        out_path = STAGING_DIR / name / f"{name}.sql"
        info = run_dump_to_file(
            [
                "docker",
                "exec",
                container,
                "pg_dump",
                "-U",
                user,
                "-d",
                db,
                "--format=plain",
                "--no-owner",
                "--no-acl",
            ],
            out_path,
        )
        info["container"] = container
        info["image"] = container_image(container)
        manifest["databases"][name] = info

    fleet_out = STAGING_DIR / "fleet" / "fleet.sql"
    fleet_info = run_dump_to_file(
        [
            "docker",
            "exec",
            "fleet-mysql",
            "sh",
            "-c",
            'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysqldump '
            "--single-transaction --routines --triggers --quick --hex-blob "
            "--set-gtid-purged=OFF -u root fleet",
        ],
        fleet_out,
    )
    fleet_info["container"] = "fleet-mysql"
    fleet_info["image"] = container_image("fleet-mysql")
    manifest["databases"]["fleet"] = fleet_info

    manifest["directories"] = copy_netbox_dirs(STAGING_DIR / "netbox")
    manifest["env_files"] = collect_stack_env_files(STAGING_DIR / "secrets" / "env")
    manifest["host_configs"] = collect_host_configs(STAGING_DIR / "host")
    manifest["total_bytes"] = dir_size_bytes(STAGING_DIR)

    manifest_path = STAGING_DIR / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.chmod(manifest_path, 0o600)
    return manifest


def build_restic_env(cfg: dict[str, str]) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "RESTIC_REPOSITORY",
        "RESTIC_PASSWORD",
    ):
        env[key] = cfg[key]
    return env


def run_restic(
    cfg: dict[str, str], args: list[str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    restic_bin = cfg.get("RESTIC_BIN") or str(Path.home() / ".local" / "bin" / "restic")
    if not Path(restic_bin).exists():
        raise FileNotFoundError(f"restic binary not found: {restic_bin}")
    return subprocess.run(
        [restic_bin, *args],
        env=build_restic_env(cfg),
        capture_output=True,
        text=True,
        check=check,
    )


def init_repo_if_needed(cfg: dict[str, str]) -> str:
    probe = run_restic(cfg, ["cat", "config"], check=False)
    if probe.returncode == 0:
        return "already_initialized"
    proc = run_restic(cfg, ["init"], check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"restic init failed: {(proc.stderr or proc.stdout).strip()}")
    return "initialized"


def parse_snapshot_id(stdout: str) -> str:
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("message_type") == "summary" and obj.get("snapshot_id"):
            return str(obj["snapshot_id"])[:12]
    return "unknown"


def send_zabbix_metrics(cfg: dict[str, str], metrics: dict[str, object]) -> dict[str, object]:
    host = cfg.get("ZABBIX_HOST", "ubuntu-desktop")
    container = cfg.get("ZABBIX_SERVER_CONTAINER", "zabbix-server")
    lines = []
    for key, val in metrics.items():
        sval = str(val).replace("\n", " ").replace("\r", " ")
        if isinstance(val, str):
            escaped = sval.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'"{host}" {key} "{escaped}"')
        else:
            lines.append(f'"{host}" {key} {sval}')
    payload = "\n".join(lines) + "\n"
    proc = subprocess.run(
        ["docker", "exec", "-i", container, "zabbix_sender", "-z", "127.0.0.1", "-i", "-"],
        input=payload,
        capture_output=True,
        text=True,
    )
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def write_run_log(record: dict[str, object]) -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = dt.datetime.now(JST).strftime("%Y-%m-%d_%H%M%S")
    path = RUNS_DIR / f"{stamp}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)

    cutoff = time.time() - LOG_RETENTION_DAYS * 86400
    for old in RUNS_DIR.glob("*.json"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 1 DB backup to Backblaze B2 via restic")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--init", action="store_true", help="Initialize restic repository if needed")
    parser.add_argument("--dry-run", action="store_true", help="Dump DBs and verify only (no B2 upload)")
    parser.add_argument("--no-prune", action="store_true", help="Skip restic forget --prune")
    parser.add_argument("--no-zabbix", action="store_true", help="Skip Zabbix Trapper reporting")
    parser.add_argument("--check", action="store_true", help="Run restic check (--read-data-subset=10%)")
    args = parser.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE_DIR, 0o700)

    lock_fd = LOCK_FILE.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another backup_phase1_db.py instance is already running.", file=sys.stderr)
        return 2

    started_at = time.monotonic()
    cfg: dict[str, str] = {}
    secrets_to_mask: list[str] = []
    record: dict[str, object] = {
        "started_at_jst": dt.datetime.now(JST).isoformat(timespec="seconds"),
        "mode": (
            "init"
            if args.init
            else "check"
            if args.check
            else "dry-run"
            if args.dry_run
            else "backup"
        ),
        "status": "unknown",
    }

    try:
        if args.env_file.exists():
            cfg = load_env_file(args.env_file)
            secrets_to_mask = [
                cfg.get("AWS_ACCESS_KEY_ID", ""),
                cfg.get("AWS_SECRET_ACCESS_KEY", ""),
                cfg.get("RESTIC_PASSWORD", ""),
            ]
        elif not args.dry_run:
            raise FileNotFoundError(f"Env file not found: {args.env_file}")

        if args.init:
            validate_restic_env(cfg)
            state = init_repo_if_needed(cfg)
            record["status"] = "ok"
            record["init_state"] = state
            print(json.dumps(record, ensure_ascii=False))
            return 0

        if args.check:
            validate_restic_env(cfg)
            proc = run_restic(cfg, ["check", "--read-data-subset=10%"], check=False)
            if proc.returncode != 0:
                raise RuntimeError(f"restic check failed: {(proc.stderr or proc.stdout).strip()}")
            record["status"] = "ok"
            record["duration_sec"] = round(time.monotonic() - started_at, 2)
            record["output"] = sanitize_text(proc.stdout.strip(), secrets_to_mask)
            write_run_log(record)
            print(json.dumps(record, ensure_ascii=False))
            return 0

        manifest = dump_all_targets()
        total_bytes = int(manifest["total_bytes"])
        record["manifest"] = manifest

        if args.dry_run:
            record["status"] = "ok"
            record["duration_sec"] = round(time.monotonic() - started_at, 2)
            write_run_log(record)
            print(json.dumps(record, ensure_ascii=False, indent=2))
            return 0

        validate_restic_env(cfg)
        backup_proc = run_restic(
            cfg,
            [
                "backup",
                "--json",
                "--tag",
                "phase1-db",
                "--host",
                cfg.get("ZABBIX_HOST", "ubuntu-desktop"),
                str(STAGING_DIR),
            ],
            check=False,
        )
        if backup_proc.returncode != 0:
            raise RuntimeError(
                f"restic backup failed (rc={backup_proc.returncode}): "
                f"{(backup_proc.stderr or backup_proc.stdout).strip()}"
            )
        snapshot_id = parse_snapshot_id(backup_proc.stdout)
        record["snapshot_id"] = snapshot_id

        if not args.no_prune:
            keep_daily = cfg.get("KEEP_DAILY", "7")
            keep_weekly = cfg.get("KEEP_WEEKLY", "4")
            keep_monthly = cfg.get("KEEP_MONTHLY", "12")
            prune_proc = run_restic(
                cfg,
                [
                    "forget",
                    "--tag",
                    "phase1-db",
                    "--keep-daily",
                    str(keep_daily),
                    "--keep-weekly",
                    str(keep_weekly),
                    "--keep-monthly",
                    str(keep_monthly),
                    "--prune",
                ],
                check=False,
            )
            if prune_proc.returncode != 0:
                raise RuntimeError(
                    f"restic forget --prune failed (rc={prune_proc.returncode}): "
                    f"{(prune_proc.stderr or prune_proc.stdout).strip()}"
                )
            record["pruned"] = True

        duration_sec = int(round(time.monotonic() - started_at))
        record["status"] = "ok"
        record["duration_sec"] = duration_sec
        db_count = len(manifest["databases"])
        env_count = len(manifest["env_files"])
        etc_count = len(manifest["host_configs"].get("etc_files", []))
        msg = (
            f"ok snapshot={snapshot_id} dbs={db_count} env={env_count} "
            f"etc={etc_count} bytes={total_bytes} duration={duration_sec}s"
        )
        record["message"] = msg

        if not args.no_zabbix:
            zbx_res = send_zabbix_metrics(
                cfg,
                {
                    "backup.phase1_db.status": 0,
                    "backup.phase1_db.last_success": int(time.time()),
                    "backup.phase1_db.duration_sec": duration_sec,
                    "backup.phase1_db.dump_bytes": total_bytes,
                    "backup.phase1_db.message": msg,
                },
            )
            record["zabbix"] = zbx_res

        write_run_log(record)
        print(json.dumps(record, ensure_ascii=False))
        return 0

    except Exception as exc:
        duration_sec = int(round(time.monotonic() - started_at))
        err_msg = sanitize_text(str(exc), secrets_to_mask)
        record["status"] = "error"
        record["duration_sec"] = duration_sec
        record["error"] = err_msg
        if not args.dry_run and not args.no_zabbix and not args.init:
            try:
                record["zabbix"] = send_zabbix_metrics(
                    cfg,
                    {
                        "backup.phase1_db.status": 1,
                        "backup.phase1_db.duration_sec": duration_sec,
                        "backup.phase1_db.message": f"error: {err_msg[:200]}",
                    },
                )
            except Exception as zexc:
                record["zabbix_error"] = sanitize_text(str(zexc), secrets_to_mask)
        write_run_log(record)
        print(json.dumps(record, ensure_ascii=False), file=sys.stderr)
        return 1

    finally:
        if STAGING_DIR.exists():
            shutil.rmtree(STAGING_DIR, ignore_errors=True)
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
        finally:
            lock_fd.close()


if __name__ == "__main__":
    raise SystemExit(main())
