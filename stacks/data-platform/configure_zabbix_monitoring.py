#!/usr/bin/env python3
"""Idempotently provision Zabbix Trapper items and triggers for Infra DWH pipeline.

Run on ubuntu-desktop:
  python3 stacks/data-platform/configure_zabbix_monitoring.py
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
TEMP_TOKEN_NAME = "tmp-configure-infra-dwh-monitoring"

ITEMS = [
    {
        "name": "Infra DWH pipeline status",
        "key_": "infra.dwh.status",
        "type": 2,
        "value_type": 3,
        "units": "",
        "description": "0 = success, non-zero = failure",
    },
    {
        "name": "Infra DWH pipeline duration",
        "key_": "infra.dwh.duration_sec",
        "type": 2,
        "value_type": 0,
        "units": "s",
        "description": "Total runtime of the Infra DWH daily pipeline in seconds",
    },
    {
        "name": "Infra DWH raw rows loaded",
        "key_": "infra.dwh.raw_rows",
        "type": 2,
        "value_type": 3,
        "units": "rows",
        "description": "Total row count across raw.* tables after daily dlt ingestion",
    },
    {
        "name": "Infra DWH dbt governance warnings",
        "key_": "infra.dwh.dbt_warn",
        "type": 2,
        "value_type": 3,
        "units": "",
        "description": "Number of dbt governance tests returning WARN",
    },
    {
        "name": "Infra DWH dbt test failures",
        "key_": "infra.dwh.dbt_fail",
        "type": 2,
        "value_type": 3,
        "units": "",
        "description": "Number of dbt quality/governance tests returning FAIL or ERROR",
    },
    {
        "name": "Infra RAG sync & eval status",
        "key_": "infra.rag.status",
        "type": 2,
        "value_type": 3,
        "units": "",
        "description": "0 = RAG sync & eval passed, non-zero = failure",
    },
    {
        "name": "Infra RAG indexed chunks",
        "key_": "infra.rag.chunks",
        "type": 2,
        "value_type": 3,
        "units": "chunks",
        "description": "Total governed chunks stored in rag.chunks (pgvector)",
    },
    {
        "name": "Infra RAG retrieval Hit@3",
        "key_": "infra.rag.hit_at_3",
        "type": 2,
        "value_type": 0,
        "units": "%",
        "description": "Golden dataset retrieval Hit@3 accuracy (%)",
    },
    {
        "name": "Infra RAG leakage violations",
        "key_": "infra.rag.leakage",
        "type": 2,
        "value_type": 3,
        "units": "",
        "description": "Unredacted secret/PII leakage violations detected in rag.chunks (must be 0)",
    },
]

TRIGGERS = [
    {
        "description": "Infra DWH daily pipeline failed on ubuntu-desktop",
        "expression": f"last(/{TARGET_HOST}/infra.dwh.status)<>0",
        "priority": 3,
        "comments": "Daily Infra DWH pipeline (dlt + dbt run + dbt test) returned non-zero status.",
    },
    {
        "description": "Infra DWH daily pipeline has not reported in 28h on ubuntu-desktop",
        "expression": f"nodata(/{TARGET_HOST}/infra.dwh.status,28h)=1",
        "priority": 2,
        "comments": "No Infra DWH daily pipeline heartbeat received in the last 28 hours.",
    },
    {
        "description": "Infra RAG secret/PII leakage violation detected on ubuntu-desktop",
        "expression": f"last(/{TARGET_HOST}/infra.rag.leakage)>0",
        "priority": 4,
        "comments": "Unredacted secret or PII pattern was detected in rag.chunks.",
    },
    {
        "description": "Infra RAG evaluation failed or accuracy dropped on ubuntu-desktop",
        "expression": f"last(/{TARGET_HOST}/infra.rag.status)<>0",
        "priority": 3,
        "comments": "RAG retrieval evaluation failed (Hit@3 below threshold or sync error).",
    },
]


def psql(sql: str) -> str:
    proc = subprocess.run(
        [
            "docker", "exec", "-i", "zabbix-postgres",
            "psql", "-v", "ON_ERROR_STOP=1", "-U", "zabbix", "-d", "zabbix", "-tAc", sql,
        ],
        capture_output=True, text=True, check=True,
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
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode("utf-8")
    req = urllib.request.Request(
        ZABBIX_API_URL,
        data=body,
        headers={"Content-Type": "application/json-rpc", "Authorization": f"Bearer {token}"},
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
        hosts = zabbix_rpc(token, "host.get", {"filter": {"host": [TARGET_HOST]}, "output": ["hostid", "host"]})
        if not hosts:
            raise RuntimeError(f"Zabbix host not found: {TARGET_HOST}")
        hostid = hosts[0]["hostid"]

        existing_items = zabbix_rpc(token, "item.get", {"hostids": [hostid], "output": ["itemid", "key_"]})
        by_key = {it["key_"]: it for it in existing_items}
        item_results = []
        for spec in ITEMS:
            if spec["key_"] in by_key:
                itemid = by_key[spec["key_"]]["itemid"]
                zabbix_rpc(token, "item.update", {"itemid": itemid, **{k: v for k, v in spec.items() if k != "key_"}, "status": 0})
                item_results.append((spec["key_"], "updated", itemid))
            else:
                res = zabbix_rpc(token, "item.create", {"hostid": hostid, **spec})
                item_results.append((spec["key_"], "created", res["itemids"][0]))

        existing_triggers = zabbix_rpc(token, "trigger.get", {"hostids": [hostid], "output": ["triggerid", "description"]})
        by_desc = {tr["description"]: tr for tr in existing_triggers}
        trigger_results = []
        for tspec in TRIGGERS:
            if tspec["description"] in by_desc:
                tid = by_desc[tspec["description"]]["triggerid"]
                zabbix_rpc(token, "trigger.update", {"triggerid": tid, "expression": tspec["expression"], "priority": tspec["priority"], "comments": tspec["comments"], "status": 0})
                trigger_results.append((tspec["description"], "updated", tid))
            else:
                res = zabbix_rpc(token, "trigger.create", tspec)
                trigger_results.append((tspec["description"], "created", res["triggerids"][0]))

        print(json.dumps({"host": TARGET_HOST, "items": item_results, "triggers": trigger_results}, ensure_ascii=False, indent=2))
    finally:
        delete_temp_token(tokenid)


if __name__ == "__main__":
    main()
