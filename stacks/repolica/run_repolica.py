#!/usr/bin/env python3
"""Run Repolica one-shot Git replication + status verification + Zabbix Trapper reporting.

Non-resident runner designed for cron (02:00 JST daily). Runs the hardened
`repolica:local` container (`--read-only`, `--cap-drop ALL`, non-root 65532,
`--tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m` (self-executing GIT_ASKPASS requires no /tmp exec)) with zero
resident memory overhead.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
STACK_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_FILE = STACK_DIR / ".env"
DEFAULT_CONFIG_FILE = STACK_DIR / "repolica.yaml"
DEFAULT_IMAGE = "repolica:local"
DEFAULT_VOLUME = "repolica-data"
STATE_DIR = Path.home() / ".local/state/repolica"
RUNS_DIR = STATE_DIR / "runs"
LOCK_FILE = STATE_DIR / "repolica.lock"
LOG_RETENTION_DAYS = 30
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
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    for req in ("REPOLICA_GITHUB_TOKEN", "REPOLICA_GITLAB_TOKEN"):
        if not env.get(req):
            raise ValueError(f"Missing required secret key in {path}: {req}")
    return env


def sanitize_text(text: str, secrets_map: dict[str, str]) -> str:
    out = text
    for val in secrets_map.values():
        if val and len(val) >= 8:
            out = out.replace(val, "[REDACTED]")
    return out


def run_repolica_container(
    env_file: Path,
    config_file: Path,
    image: str,
    volume: str,
    subargs: list[str],
    timeout_sec: int = 600,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        "docker",
        "run",
        "--rm",
        "--init",
        "--read-only",
        "--security-opt",
        "no-new-privileges:true",
        "--cap-drop",
        "ALL",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=64m",
        "--env-file",
        str(env_file),
        "-v",
        f"{config_file}:/etc/repolica/repolica.yaml:ro",
        "-v",
        f"{volume}:/var/lib/repolica/data",
        image,
        *subargs,
        "--config",
        "/etc/repolica/repolica.yaml",
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)


def send_zabbix_metrics(metrics: dict[str, object], host: str = "ubuntu-desktop") -> dict[str, object]:
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
        ["docker", "exec", "-i", "zabbix-server", "zabbix_sender", "-z", "127.0.0.1", "-i", "-"],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
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
    parser = argparse.ArgumentParser(description="Run Repolica Git replication and Zabbix reporting")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--config-file", type=Path, default=DEFAULT_CONFIG_FILE)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--volume", default=DEFAULT_VOLUME)
    parser.add_argument("--status-only", action="store_true", help="Skip sync and run status --json only")
    parser.add_argument("--no-zabbix", action="store_true", help="Skip Zabbix Trapper reporting")
    args = parser.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(json.dumps({"status": "error", "error": "another repolica run is already active"}))
        return 2

    started = time.time()
    started_jst = dt.datetime.now(JST).isoformat(timespec="seconds")
    secrets_map: dict[str, str] = {}
    record: dict[str, object] = {
        "started_at_jst": started_jst,
        "mode": "status-only" if args.status_only else "sync",
        "status": "error",
    }

    try:
        secrets_map = load_env_file(args.env_file)
        if not args.config_file.is_file():
            raise FileNotFoundError(f"Config file not found: {args.config_file}")

        sync_rc = 0
        sync_out = ""
        if not args.status_only:
            sync_proc = run_repolica_container(
                args.env_file, args.config_file, args.image, args.volume, ["sync", "--once"]
            )
            sync_rc = sync_proc.returncode
            sync_out = sanitize_text((sync_proc.stdout + "\n" + sync_proc.stderr).strip(), secrets_map)
            record["sync_rc"] = sync_rc

        status_proc = run_repolica_container(
            args.env_file, args.config_file, args.image, args.volume, ["status", "--json"]
        )
        status_stdout = sanitize_text(status_proc.stdout.strip(), secrets_map)
        status_stderr = sanitize_text(status_proc.stderr.strip(), secrets_map)
        record["status_rc"] = status_proc.returncode

        summary: dict[str, int] = {"total": 0, "healthy": 0, "behind": 0, "diverged": 0, "error": 0}
        status_data = None
        if status_stdout:
            try:
                status_data = json.loads(status_stdout)
                if isinstance(status_data, dict) and isinstance(status_data.get("summary"), dict):
                    for k in summary:
                        summary[k] = int(status_data["summary"].get(k, 0))
            except json.JSONDecodeError:
                pass

        unhealthy = summary["behind"] + summary["diverged"] + summary["error"]
        duration = int(round(time.time() - started))
        ok = (sync_rc == 0) and (status_proc.returncode == 0) and (unhealthy == 0) and (summary["total"] > 0)

        record["summary"] = summary
        record["unhealthy"] = unhealthy
        record["duration_sec"] = duration
        if status_data is not None:
            record["results"] = status_data.get("results", [])

        if ok:
            record["status"] = "ok"
            msg = f"ok healthy={summary['healthy']}/{summary['total']} duration={duration}s"
            record["message"] = msg
        else:
            err_detail = sync_out or status_stderr or f"unhealthy={unhealthy} sync_rc={sync_rc} status_rc={status_proc.returncode}"
            record["status"] = "error"
            record["error"] = err_detail[:600]
            msg = f"error healthy={summary['healthy']}/{summary['total']} unhealthy={unhealthy} duration={duration}s"
            record["message"] = msg

        if not args.no_zabbix:
            zbx_metrics: dict[str, object] = {
                "repolica.sync.status": 0 if ok else 1,
                "repolica.sync.duration_sec": duration,
                "repolica.repos.total": summary["total"],
                "repolica.repos.healthy": summary["healthy"],
                "repolica.repos.unhealthy": unhealthy,
                "repolica.sync.message": msg[:250],
            }
            if ok:
                zbx_metrics["repolica.sync.last_success"] = int(time.time())
            record["zabbix"] = send_zabbix_metrics(zbx_metrics)

        write_run_log(record)
        print(json.dumps(record, ensure_ascii=False))
        return 0 if ok else 1
    except Exception as exc:
        duration = int(round(time.time() - started))
        err_msg = sanitize_text(str(exc), secrets_map)
        record["duration_sec"] = duration
        record["error"] = err_msg[:600]
        record["message"] = f"exception: {err_msg[:200]}"
        if not args.no_zabbix:
            try:
                record["zabbix"] = send_zabbix_metrics(
                    {
                        "repolica.sync.status": 1,
                        "repolica.sync.duration_sec": duration,
                        "repolica.sync.message": str(record["message"])[:250],
                    }
                )
            except Exception as zexc:
                record["zabbix_error"] = str(zexc)
        write_run_log(record)
        print(json.dumps(record, ensure_ascii=False))
        return 1
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(main())
