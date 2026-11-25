#!/usr/bin/env python3
"""Idempotently register Phase 1 DB and fileserver backup Zabbix Trapper items and triggers.

Uses an ephemeral Zabbix Admin API token created directly in `zabbix-postgres`,
calls Zabbix 8.0 JSON-RPC API to ensure Trapper items and triggers exist on
host `ubuntu-desktop` (group `Home/Server`, which triggers Discord alerts for
Warning+), and deletes the temporary token in a finally block.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import subprocess
import time
import urllib.request

ZABBIX_API_URL = "https://zabbix.home.example.com/api_jsonrpc.php"
TARGET_HOST = "ubuntu-desktop"
TEMP_TOKEN_NAME = "tmp-configure-backup-monitoring"

ITEMS = [
    {
        "name": "Phase 1 DB backup status",
        "key_": "backup.phase1_db.status",
        "type": 2,  # Zabbix trapper
        "value_type": 3,  # Numeric (unsigned)
        "units": "",
        "description": "0 = success, non-zero = failure",
    },
    {
        "name": "Phase 1 DB backup last success",
        "key_": "backup.phase1_db.last_success",
        "type": 2,
        "value_type": 3,
        "units": "unixtime",
        "description": "Unix epoch timestamp of the last successful Phase 1 DB backup",
    },
    {
        "name": "Phase 1 DB backup duration",
        "key_": "backup.phase1_db.duration_sec",
        "type": 2,
        "value_type": 3,
        "units": "s",
        "description": "Total runtime of the Phase 1 DB backup job in seconds",
    },
    {
        "name": "Phase 1 DB backup dump size",
        "key_": "backup.phase1_db.dump_bytes",
        "type": 2,
        "value_type": 3,
        "units": "B",
        "description": "Uncompressed staging size of the Phase 1 DB dumps + NetBox media",
    },
    {
        "name": "Phase 1 DB backup message",
        "key_": "backup.phase1_db.message",
        "type": 2,
        "value_type": 4,  # Text
        "units": "",
        "description": "Summary message or error text from the latest Phase 1 DB backup run",
    },
    {
        "name": "Fileserver important backup status",
        "key_": "backup.fileserver.status",
        "type": 2,
        "value_type": 3,
        "units": "",
        "description": "0 = success, non-zero = failure for /mnt/files/important -> B2 backup",
    },
    {
        "name": "Fileserver important backup last success",
        "key_": "backup.fileserver.last_success",
        "type": 2,
        "value_type": 3,
        "units": "unixtime",
        "description": "Unix epoch timestamp of the last successful /mnt/files/important backup",
    },
    {
        "name": "Fileserver important backup duration",
        "key_": "backup.fileserver.duration_sec",
        "type": 2,
        "value_type": 3,
        "units": "s",
        "description": "Total runtime of the /mnt/files/important backup job in seconds",
    },
    {
        "name": "Fileserver important backup source size",
        "key_": "backup.fileserver.source_bytes",
        "type": 2,
        "value_type": 3,
        "units": "B",
        "description": "Total scanned size of /mnt/files/important",
    },
    {
        "name": "Fileserver important backup message",
        "key_": "backup.fileserver.message",
        "type": 2,
        "value_type": 4,
        "units": "",
        "description": "Summary message or error text from the latest /mnt/files/important backup run",
    },
    {
        "name": "Repolica Git replication status",
        "key_": "repolica.sync.status",
        "type": 2,
        "value_type": 3,
        "units": "",
        "description": "0 = healthy sync, non-zero = replication failure or unhealthy repo",
    },
    {
        "name": "Repolica Git replication last success",
        "key_": "repolica.sync.last_success",
        "type": 2,
        "value_type": 3,
        "units": "unixtime",
        "description": "Unix epoch timestamp of the last fully healthy Repolica sync",
    },
    {
        "name": "Repolica Git replication duration",
        "key_": "repolica.sync.duration_sec",
        "type": 2,
        "value_type": 3,
        "units": "s",
        "description": "Total runtime of the Repolica sync + status verification job in seconds",
    },
    {
        "name": "Repolica Git repositories total",
        "key_": "repolica.repos.total",
        "type": 2,
        "value_type": 3,
        "units": "",
        "description": "Total GitHub -> GitLab repository/target pairs managed by Repolica",
    },
    {
        "name": "Repolica Git repositories healthy",
        "key_": "repolica.repos.healthy",
        "type": 2,
        "value_type": 3,
        "units": "",
        "description": "Number of healthy verified Repolica repository/target pairs",
    },
    {
        "name": "Repolica Git repositories unhealthy",
        "key_": "repolica.repos.unhealthy",
        "type": 2,
        "value_type": 3,
        "units": "",
        "description": "Number of behind/diverged/error Repolica repository/target pairs",
    },
    {
        "name": "Repolica Git replication message",
        "key_": "repolica.sync.message",
        "type": 2,
        "value_type": 4,
        "units": "",
        "description": "Summary message or error text from the latest Repolica replication run",
    },
]

TRIGGERS = [
    {
        "description": "Phase 1 DB backup failed on ubuntu-desktop",
        "expression": f"last(/{TARGET_HOST}/backup.phase1_db.status)<>0",
        "priority": 3,  # Average (triggers Discord alert)
        "comments": "Daily Phase 1 DB backup (pg_dump/mysqldump -> restic -> Backblaze B2) returned non-zero status.",
    },
    {
        "description": "Phase 1 DB backup has not succeeded in 28h on ubuntu-desktop",
        "expression": f"nodata(/{TARGET_HOST}/backup.phase1_db.last_success,28h)=1",
        "priority": 2,  # Warning (triggers Discord alert)
        "comments": "No successful Phase 1 DB backup heartbeat received in the last 28 hours.",
    },
    {
        "description": "Fileserver important backup failed on ubuntu-desktop",
        "expression": f"last(/{TARGET_HOST}/backup.fileserver.status)<>0",
        "priority": 3,  # Average (triggers Discord alert)
        "comments": "Daily /mnt/files/important -> restic -> Backblaze B2 backup returned non-zero status (including unmounted SSD guard).",
    },
    {
        "description": "Fileserver important backup has not succeeded in 28h on ubuntu-desktop",
        "expression": f"nodata(/{TARGET_HOST}/backup.fileserver.last_success,28h)=1",
        "priority": 2,  # Warning (triggers Discord alert)
        "comments": "No successful /mnt/files/important backup heartbeat received in the last 28 hours.",
    },
    {
        "description": "Repolica Git replication failed on ubuntu-desktop",
        "expression": f"last(/{TARGET_HOST}/repolica.sync.status)<>0",
        "priority": 3,
        "comments": "Daily Repolica GitHub -> GitLab.com replication returned non-zero status or reported unhealthy repositories.",
    },
    {
        "description": "Repolica Git replication has not succeeded in 28h on ubuntu-desktop",
        "expression": f"nodata(/{TARGET_HOST}/repolica.sync.last_success,28h)=1",
        "priority": 2,
        "comments": "No healthy Repolica Git replication heartbeat received in the last 28 hours.",
    },
]


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


def sql_str(val: str) -> str:
    return "'" + val.replace("'", "''") + "'"


def create_temp_token() -> tuple[int, str]:
    psql(f"DELETE FROM token WHERE name = {sql_str(TEMP_TOKEN_NAME)};")
    tokenid = int(psql("SELECT COALESCE(MAX(tokenid), 0) + 1 FROM token;"))
    admin_userid = int(psql("SELECT userid FROM users WHERE username = 'Admin';"))
    plaintext = secrets.token_hex(32)
    digest = hashlib.sha512(plaintext.encode("utf-8")).hexdigest()
    now = int(time.time())
    expires = now + 120
    psql(
        f"INSERT INTO token (tokenid, name, description, userid, token, lastaccess, status, expires_at, created_at, creator_userid) "
        f"VALUES ({tokenid}, {sql_str(TEMP_TOKEN_NAME)}, 'ephemeral setup token', {admin_userid}, "
        f"{sql_str(digest)}, 0, 0, {expires}, {now}, {admin_userid});"
    )
    return tokenid, plaintext


def delete_temp_token(tokenid: int) -> None:
    psql(f"DELETE FROM token WHERE tokenid = {tokenid};")


def zabbix_rpc(token: str, method: str, params: dict) -> object:
    body = json.dumps(
        {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    ).encode("utf-8")
    req = urllib.request.Request(
        ZABBIX_API_URL,
        data=body,
        headers={
            "Content-Type": "application/json-rpc",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if "error" in data:
        raise RuntimeError(f"Zabbix API error on {method}: {data['error']}")
    return data["result"]


def main() -> None:
    tokenid, token = create_temp_token()
    try:
        hosts = zabbix_rpc(
            token,
            "host.get",
            {"filter": {"host": [TARGET_HOST]}, "output": ["hostid", "host", "name"]},
        )
        if not hosts:
            raise RuntimeError(f"Zabbix host not found: {TARGET_HOST}")
        hostid = hosts[0]["hostid"]

        existing_items = zabbix_rpc(
            token,
            "item.get",
            {
                "hostids": [hostid],
                "output": ["itemid", "key_", "name"],
            },
        )
        by_key = {it["key_"]: it for it in existing_items}

        item_results = []
        for spec in ITEMS:
            if spec["key_"] in by_key:
                itemid = by_key[spec["key_"]]["itemid"]
                zabbix_rpc(
                    token,
                    "item.update",
                    {
                        "itemid": itemid,
                        "name": spec["name"],
                        "type": spec["type"],
                        "value_type": spec["value_type"],
                        "units": spec["units"],
                        "description": spec["description"],
                        "status": 0,
                    },
                )
                item_results.append((spec["key_"], "updated", itemid))
            else:
                res = zabbix_rpc(
                    token,
                    "item.create",
                    {
                        "hostid": hostid,
                        **spec,
                    },
                )
                item_results.append((spec["key_"], "created", res["itemids"][0]))

        existing_triggers = zabbix_rpc(
            token,
            "trigger.get",
            {
                "hostids": [hostid],
                "output": ["triggerid", "description", "priority", "status"],
            },
        )
        by_desc = {tr["description"]: tr for tr in existing_triggers}

        trigger_results = []
        for tspec in TRIGGERS:
            if tspec["description"] in by_desc:
                tid = by_desc[tspec["description"]]["triggerid"]
                zabbix_rpc(
                    token,
                    "trigger.update",
                    {
                        "triggerid": tid,
                        "expression": tspec["expression"],
                        "priority": tspec["priority"],
                        "comments": tspec["comments"],
                        "status": 0,
                    },
                )
                trigger_results.append((tspec["description"], "updated", tid))
            else:
                res = zabbix_rpc(token, "trigger.create", tspec)
                trigger_results.append((tspec["description"], "created", res["triggerids"][0]))

        print(
            json.dumps(
                {
                    "host": TARGET_HOST,
                    "hostid": hostid,
                    "items": item_results,
                    "triggers": trigger_results,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        delete_temp_token(tokenid)


if __name__ == "__main__":
    main()
