#!/usr/bin/env python3
"""Idempotently provision a least-privilege read-only Zabbix 8.0 API user & token.

Run on ubuntu-desktop:
  python3 stacks/zabbix/configure_readonly_api.py [--env-file stacks/opencode/jobs/.env]

Security properties:
- Creates a custom Zabbix User role (`OpenCode Read-Only API`) with UI/actions/
  modules/services/devices disabled and API allowlist restricted strictly to
  `problem.get`, `trigger.get`, `host.get`, `event.get`, `hostgroup.get`.
- Creates user group `OpenCode Read-Only` with `gui_access = 3` (no frontend
  login) and read-only permission (`2`) on monitored host groups only.
- Generates a 64-hex API token, stores its SHA-512 hash in Zabbix `token`
  table, and writes the plaintext token ONLY into the target chmod-600 `.env`
  file (never printed to stdout).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import subprocess
import time
from pathlib import Path

ROLE_NAME = "OpenCode Read-Only API"
USRGRP_NAME = "OpenCode Read-Only"
USERNAME = "opencode-ro"
TOKEN_NAME = "opencode-daily-health"
ALLOWED_HOST_GROUPS = ("Zabbix servers", "Home/Network", "Home/PC", "Home/Server")
ALLOWED_API_METHODS = ("problem.get", "trigger.get", "host.get", "event.get", "hostgroup.get")


def psql(sql: str) -> str:
    proc = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            "zabbix-postgres",
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "zabbix",
            "-d",
            "zabbix",
            "-tAc",
            sql,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def next_id(table: str, pk: str) -> int:
    val = psql(f"SELECT COALESCE(MAX({pk}), 0) + 1 FROM {table};")
    return int(val)


def ensure_role() -> int:
    existing = psql(f"SELECT roleid FROM role WHERE name = {sql_str(ROLE_NAME)};")
    if existing:
        roleid = int(existing)
    else:
        roleid = next_id("role", "roleid")
        psql(
            f"INSERT INTO role (roleid, name, type, readonly) "
            f"VALUES ({roleid}, {sql_str(ROLE_NAME)}, 1, 0);"
        )

    psql(f"DELETE FROM role_rule WHERE roleid = {roleid};")
    rules: list[tuple[str, int, int, str]] = [
        ("ui.default_access", 0, 0, ""),
        ("actions.default_access", 0, 0, ""),
        ("modules.default_access", 0, 0, ""),
        ("services.read", 0, 0, ""),
        ("services.write", 0, 0, ""),
        ("devices.access", 0, 0, ""),
        ("devices.actions.default_access", 0, 0, ""),
        ("api.access", 0, 1, ""),
        ("api.mode", 0, 1, ""),
    ]
    for idx, method in enumerate(ALLOWED_API_METHODS):
        rules.append((f"api.method.{idx}", 1, 0, method))

    rule_id = next_id("role_rule", "role_ruleid")
    values_sql = []
    for name, rtype, vint, vstr in rules:
        values_sql.append(
            f"({rule_id}, {roleid}, {rtype}, {sql_str(name)}, {vint}, {sql_str(vstr)})"
        )
        rule_id += 1
    psql(
        "INSERT INTO role_rule (role_ruleid, roleid, type, name, value_int, value_str) VALUES "
        + ", ".join(values_sql)
        + ";"
    )
    return roleid


def ensure_usrgrp() -> int:
    existing = psql(f"SELECT usrgrpid FROM usrgrp WHERE name = {sql_str(USRGRP_NAME)};")
    if existing:
        usrgrpid = int(existing)
        psql(f"UPDATE usrgrp SET gui_access = 3, users_status = 0 WHERE usrgrpid = {usrgrpid};")
    else:
        usrgrpid = next_id("usrgrp", "usrgrpid")
        psql(
            f"INSERT INTO usrgrp (usrgrpid, name, gui_access, users_status, debug_mode) "
            f"VALUES ({usrgrpid}, {sql_str(USRGRP_NAME)}, 3, 0, 0);"
        )

    psql(f"DELETE FROM rights WHERE groupid = {usrgrpid};")
    right_id = next_id("rights", "rightid")
    for hg_name in ALLOWED_HOST_GROUPS:
        hg_id = psql(f"SELECT groupid FROM hstgrp WHERE name = {sql_str(hg_name)};")
        if hg_id:
            psql(
                f"INSERT INTO rights (rightid, groupid, permission, id) "
                f"VALUES ({right_id}, {usrgrpid}, 2, {int(hg_id)});"
            )
            right_id += 1
    return usrgrpid


def ensure_user(roleid: int, usrgrpid: int) -> int:
    existing = psql(f"SELECT userid FROM users WHERE username = {sql_str(USERNAME)};")
    if existing:
        userid = int(existing)
        psql(f"UPDATE users SET roleid = {roleid} WHERE userid = {userid};")
    else:
        userid = next_id("users", "userid")
        # Locked unusable placeholder password (no frontend access anyway).
        locked_pw = "!locked-" + secrets.token_hex(20)
        psql(
            f"INSERT INTO users (userid, username, name, surname, passwd, roleid) "
            f"VALUES ({userid}, {sql_str(USERNAME)}, 'OpenCode', 'ReadOnly', {sql_str(locked_pw)}, {roleid});"
        )

    linked = psql(
        f"SELECT id FROM users_groups WHERE usrgrpid = {usrgrpid} AND userid = {userid};"
    )
    if not linked:
        ug_id = next_id("users_groups", "id")
        psql(
            f"INSERT INTO users_groups (id, usrgrpid, userid) VALUES ({ug_id}, {usrgrpid}, {userid});"
        )
    return userid


def ensure_ugset_permissions(userid: int, usrgrpid: int) -> int:
    """Populate Zabbix 8.0 precomputed ugset/permission tables for read-only access."""
    ugset_hash = hashlib.sha256(str(usrgrpid).encode("utf-8")).hexdigest()
    existing = psql(f"SELECT ugsetid FROM ugset WHERE hash = {sql_str(ugset_hash)};")
    if existing:
        ugsetid = int(existing)
    else:
        ugsetid = next_id("ugset", "ugsetid")
        psql(f"INSERT INTO ugset (ugsetid, hash) VALUES ({ugsetid}, {sql_str(ugset_hash)});")
        psql(f"INSERT INTO ugset_group (ugsetid, usrgrpid) VALUES ({ugsetid}, {usrgrpid});")

    psql(
        f"INSERT INTO user_ugset (userid, ugsetid) VALUES ({userid}, {ugsetid}) "
        f"ON CONFLICT (userid) DO UPDATE SET ugsetid = EXCLUDED.ugsetid;"
    )

    psql(f"DELETE FROM permission WHERE ugsetid = {ugsetid};")
    psql(
        f"INSERT INTO permission (ugsetid, hgsetid, permission) "
        f"SELECT DISTINCT {ugsetid}, hg.hgsetid, 2 "
        f"FROM hgset_group hg "
        f"JOIN rights r ON r.id = hg.groupid AND r.groupid = {usrgrpid} AND r.permission = 2;"
    )
    return ugsetid


def rotate_token(userid: int) -> str:
    plaintext = secrets.token_hex(32)
    digest = hashlib.sha512(plaintext.encode("utf-8")).hexdigest()
    now_ts = int(time.time())
    existing = psql(
        f"SELECT tokenid FROM token WHERE userid = {userid} AND name = {sql_str(TOKEN_NAME)};"
    )
    if existing:
        psql(
            f"UPDATE token SET token = {sql_str(digest)}, status = 0, expires_at = 0 "
            f"WHERE tokenid = {int(existing)};"
        )
    else:
        tokenid = next_id("token", "tokenid")
        psql(
            f"INSERT INTO token (tokenid, name, description, userid, token, lastaccess, status, expires_at, created_at, creator_userid, auth_scheme) "
            f"VALUES ({tokenid}, {sql_str(TOKEN_NAME)}, 'Read-only API token for OpenCode daily health digest', "
            f"{userid}, {sql_str(digest)}, 0, 0, 0, {now_ts}, 1, 0);"
        )
    return plaintext


def update_env_file(env_path: Path, token: str) -> None:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    if env_path.is_file():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    else:
        example = env_path.parent / ".env.example"
        lines = example.read_text(encoding="utf-8").splitlines() if example.is_file() else []

    out_lines: list[str] = []
    replaced = False
    for line in lines:
        if line.startswith("ZABBIX_API_TOKEN="):
            out_lines.append(f"ZABBIX_API_TOKEN={token}")
            replaced = True
        else:
            out_lines.append(line)
    if not replaced:
        out_lines.append(f"ZABBIX_API_TOKEN={token}")

    env_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    os.chmod(env_path, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description="Provision read-only Zabbix API user & token.")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path("/home/infra-admin/dev/home-infra/stacks/opencode/jobs/.env"),
    )
    args = parser.parse_args()

    roleid = ensure_role()
    usrgrpid = ensure_usrgrp()
    userid = ensure_user(roleid, usrgrpid)
    ugsetid = ensure_ugset_permissions(userid, usrgrpid)
    token = rotate_token(userid)
    update_env_file(args.env_file, token)
    print(
        f"OK: roleid={roleid} usrgrpid={usrgrpid} userid={userid} ugsetid={ugsetid} "
        f"token_written={args.env_file} (chmod 600)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
