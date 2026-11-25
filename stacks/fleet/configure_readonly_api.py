#!/usr/bin/env python3
"""Idempotently provision a least-privilege read-only Fleet API observer user & token.

Run on ubuntu-desktop:
  python3 stacks/fleet/configure_readonly_api.py [--env-file stacks/opencode/jobs/.env]

Security properties:
- Creates a Fleet user with `global_role = 'observer'` and `api_only = 1`
  (no web UI login, strictly read-only across hosts/software/vulnerabilities).
- Issues a fresh 32-byte URL-safe session token in Fleet MySQL `sessions`.
- Writes `FLEET_API_URL` and `FLEET_API_TOKEN` ONLY into the target chmod-600
  `.env` file (never prints the token to stdout).
"""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import subprocess
from pathlib import Path

DEFAULT_API_URL = "https://fleet.home.example.com/api/v1/fleet"
USER_EMAIL = "opencode-ro@home.example.com"
USER_NAME = "OpenCode ReadOnly"


def mysql_exec(sql: str) -> str:
    proc = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            "fleet-mysql",
            "sh",
            "-c",
            'mysql -N -B -u fleet -p"$MYSQL_PASSWORD" fleet',
        ],
        input=sql,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def sql_str(val: str) -> str:
    return "'" + val.replace("\\", "\\\\").replace("'", "\\'") + "'"


def upsert_env(env_path: Path, updates: dict[str, str]) -> None:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        matched = False
        for k, v in updates.items():
            if line.strip().startswith(f"{k}="):
                out.append(f"{k}={v}")
                seen.add(k)
                matched = True
                break
        if not matched:
            out.append(line)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(env_path, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description="Provision read-only Fleet API observer & token.")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path("/home/infra-admin/dev/home-infra/stacks/opencode/jobs/.env"),
    )
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    args = parser.parse_args()

    existing_id = mysql_exec(f"SELECT id FROM users WHERE email = {sql_str(USER_EMAIL)};")
    if existing_id:
        user_id = int(existing_id.splitlines()[0].strip())
        mysql_exec(
            f"UPDATE users SET name = {sql_str(USER_NAME)}, global_role = 'observer', "
            f"api_only = 1, sso_enabled = 0, admin_forced_password_reset = 0 "
            f"WHERE id = {user_id};"
        )
    else:
        locked_pw = "!" + secrets.token_hex(28)
        salt = secrets.token_hex(16)
        mysql_exec(
            "INSERT INTO users (password, salt, name, email, admin_forced_password_reset, "
            "gravatar_url, position, sso_enabled, global_role, api_only, mfa_enabled) VALUES ("
            f"{sql_str(locked_pw)}, {sql_str(salt)}, {sql_str(USER_NAME)}, {sql_str(USER_EMAIL)}, "
            "0, '', 'OpenCode Batch Observer', 0, 'observer', 1, 0);"
        )
        user_id = int(mysql_exec(f"SELECT id FROM users WHERE email = {sql_str(USER_EMAIL)};").splitlines()[0].strip())

    # Fleet generates 64-byte base64 session keys.
    token = base64.b64encode(secrets.token_bytes(64)).decode("ascii")
    mysql_exec(f"DELETE FROM sessions WHERE user_id = {user_id};")
    mysql_exec(
        f"INSERT INTO sessions (user_id, `key`) VALUES ({user_id}, {sql_str(token)});"
    )

    upsert_env(
        args.env_file,
        {
            "FLEET_API_URL": args.api_url.rstrip("/"),
            "FLEET_API_TOKEN": token,
        },
    )
    print(f"OK: fleet api_only observer user_id={user_id} token written to {args.env_file} (chmod 600)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
