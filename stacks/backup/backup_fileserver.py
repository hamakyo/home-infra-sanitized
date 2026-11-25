#!/usr/bin/env python3
"""Daily encrypted backup of /mnt/files/important to Backblaze B2 via restic.

Uses the existing Backblaze B2 bucket and credentials from stacks/backup/.env,
writing to a dedicated restic repository prefix (`fileserver-important`) so
lifecycle, locking, and prune operations stay isolated from `infra-db`.

Usage:
  python3 stacks/backup/backup_fileserver.py --dry-run
  python3 stacks/backup/backup_fileserver.py --init
  python3 stacks/backup/backup_fileserver.py
  python3 stacks/backup/backup_fileserver.py --check
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import fnmatch
import json
import os
import sys
import time
from pathlib import Path

from backup_phase1_db import (
    DEFAULT_ENV_FILE,
    JST,
    LOG_RETENTION_DAYS,
    STATE_DIR,
    init_repo_if_needed,
    load_env_file,
    parse_snapshot_id,
    run_restic,
    sanitize_text,
    send_zabbix_metrics,
    validate_restic_env,
)

MOUNT_POINT = Path("/mnt/files")
SOURCE_DIR = Path("/mnt/files/important")
RUNS_DIR = STATE_DIR / "runs-fileserver"
LOCK_FILE = STATE_DIR / "fileserver.lock"
REPO_SUFFIX = "fileserver-important"
BACKUP_TAG = "fileserver-important"
EXCLUDES = (".DS_Store", "._*", "Thumbs.db", "desktop.ini")


def derive_fileserver_cfg(cfg: dict[str, str]) -> dict[str, str]:
    derived = dict(cfg)
    override = derived.get("FILESERVER_RESTIC_REPOSITORY", "").strip()
    if override and not override.startswith("replace-with-"):
        derived["RESTIC_REPOSITORY"] = override
        return derived

    base = derived.get("RESTIC_REPOSITORY", "").strip().rstrip("/")
    if "/" not in base:
        raise ValueError(f"Cannot derive fileserver repository from RESTIC_REPOSITORY: {base}")
    parent, _ = base.rsplit("/", 1)
    derived["RESTIC_REPOSITORY"] = f"{parent}/{REPO_SUFFIX}"
    return derived


def is_excluded(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in EXCLUDES)


def scan_source(source_dir: Path) -> tuple[int, int]:
    total_bytes = 0
    file_count = 0
    for root, dirs, files in os.walk(source_dir):
        dirs[:] = [d for d in dirs if not is_excluded(d)]
        for fname in files:
            if is_excluded(fname):
                continue
            fpath = Path(root) / fname
            if fpath.is_file() and not fpath.is_symlink():
                total_bytes += fpath.stat().st_size
                file_count += 1
    return total_bytes, file_count


def assert_mount_ready(mount_point: Path, source_dir: Path) -> None:
    if not os.path.ismount(str(mount_point)):
        raise RuntimeError(
            f"Mount point {mount_point} is not mounted; refusing to back up unmounted path."
        )
    if not source_dir.is_dir():
        raise RuntimeError(f"Source directory does not exist: {source_dir}")


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
    parser = argparse.ArgumentParser(
        description="Fileserver (/mnt/files/important) backup to Backblaze B2 via restic"
    )
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--init", action="store_true", help="Initialize restic repository if needed")
    parser.add_argument("--dry-run", action="store_true", help="Verify mount and scan files only")
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
        print("Another backup_fileserver.py instance is already running.", file=sys.stderr)
        return 2

    started_at = time.monotonic()
    cfg: dict[str, str] = {}
    fs_cfg: dict[str, str] = {}
    secrets_to_mask: list[str] = []
    record: dict[str, object] = {
        "started_at_jst": dt.datetime.now(JST).isoformat(timespec="seconds"),
        "source": str(SOURCE_DIR),
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
            validate_restic_env(cfg)
            fs_cfg = derive_fileserver_cfg(cfg)
            record["repository"] = fs_cfg["RESTIC_REPOSITORY"]
        elif not args.dry_run:
            raise FileNotFoundError(f"Env file not found: {args.env_file}")

        if args.init:
            state = init_repo_if_needed(fs_cfg)
            record["status"] = "ok"
            record["init_state"] = state
            print(json.dumps(record, ensure_ascii=False))
            return 0

        if args.check:
            proc = run_restic(fs_cfg, ["check", "--read-data-subset=10%"], check=False)
            if proc.returncode != 0:
                raise RuntimeError(f"restic check failed: {(proc.stderr or proc.stdout).strip()}")
            record["status"] = "ok"
            record["duration_sec"] = round(time.monotonic() - started_at, 2)
            record["output"] = sanitize_text(proc.stdout.strip(), secrets_to_mask)
            write_run_log(record)
            print(json.dumps(record, ensure_ascii=False))
            return 0

        assert_mount_ready(MOUNT_POINT, SOURCE_DIR)
        source_bytes, file_count = scan_source(SOURCE_DIR)
        record["source_bytes"] = source_bytes
        record["file_count"] = file_count

        if args.dry_run:
            record["status"] = "ok"
            record["duration_sec"] = round(time.monotonic() - started_at, 2)
            write_run_log(record)
            print(json.dumps(record, ensure_ascii=False, indent=2))
            return 0

        backup_args = [
            "backup",
            "--json",
            "--tag",
            BACKUP_TAG,
            "--host",
            fs_cfg.get("ZABBIX_HOST", "ubuntu-desktop"),
        ]
        for pat in EXCLUDES:
            backup_args.extend(["--exclude", pat])
        backup_args.append(str(SOURCE_DIR))

        backup_proc = run_restic(fs_cfg, backup_args, check=False)
        if backup_proc.returncode != 0:
            raise RuntimeError(
                f"restic backup failed (rc={backup_proc.returncode}): "
                f"{(backup_proc.stderr or backup_proc.stdout).strip()}"
            )
        snapshot_id = parse_snapshot_id(backup_proc.stdout)
        record["snapshot_id"] = snapshot_id

        if not args.no_prune:
            keep_daily = fs_cfg.get("KEEP_DAILY", "7")
            keep_weekly = fs_cfg.get("KEEP_WEEKLY", "4")
            keep_monthly = fs_cfg.get("KEEP_MONTHLY", "12")
            prune_proc = run_restic(
                fs_cfg,
                [
                    "forget",
                    "--tag",
                    BACKUP_TAG,
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
        msg = f"ok snapshot={snapshot_id} files={file_count} bytes={source_bytes} duration={duration_sec}s"
        record["message"] = msg

        if not args.no_zabbix:
            zbx_res = send_zabbix_metrics(
                fs_cfg,
                {
                    "backup.fileserver.status": 0,
                    "backup.fileserver.last_success": int(time.time()),
                    "backup.fileserver.duration_sec": duration_sec,
                    "backup.fileserver.source_bytes": source_bytes,
                    "backup.fileserver.message": msg,
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
        if not args.dry_run and not args.no_zabbix and not args.init and cfg:
            try:
                record["zabbix"] = send_zabbix_metrics(
                    cfg,
                    {
                        "backup.fileserver.status": 1,
                        "backup.fileserver.duration_sec": duration_sec,
                        "backup.fileserver.message": f"error: {err_msg[:200]}",
                    },
                )
            except Exception as zexc:
                record["zabbix_error"] = sanitize_text(str(zexc), secrets_to_mask)
        write_run_log(record)
        print(json.dumps(record, ensure_ascii=False), file=sys.stderr)
        return 1

    finally:
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
        finally:
            lock_fd.close()


if __name__ == "__main__":
    raise SystemExit(main())
