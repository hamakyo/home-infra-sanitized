#!/usr/bin/env python3
"""Safely invoke restic using stacks/backup/.env without shell metacharacter issues.

Usage:
  python3 stacks/backup/run_restic.py snapshots
  python3 stacks/backup/run_restic.py stats --mode raw-data
  python3 stacks/backup/run_restic.py restore latest --target /tmp/restore
  python3 stacks/backup/run_restic.py --fileserver snapshots
  python3 stacks/backup/run_restic.py --fileserver restore latest --target /tmp/restore-fileserver
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from backup_fileserver import derive_fileserver_cfg
from backup_phase1_db import DEFAULT_ENV_FILE, build_restic_env, load_env_file, validate_restic_env


def main() -> int:
    args = list(sys.argv[1:])
    use_fileserver = False
    if args and args[0] == "--fileserver":
        use_fileserver = True
        args = args[1:]

    cfg = load_env_file(DEFAULT_ENV_FILE)
    validate_restic_env(cfg)
    if use_fileserver:
        cfg = derive_fileserver_cfg(cfg)

    restic_bin = cfg.get("RESTIC_BIN") or str(Path.home() / ".local" / "bin" / "restic")
    env = build_restic_env(cfg)
    os.execvpe(restic_bin, [restic_bin, *args], env)


if __name__ == "__main__":
    raise SystemExit(main())
