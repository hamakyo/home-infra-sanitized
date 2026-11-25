#!/usr/bin/env python3
"""Read-only 5-source Infra ingestion into PostgreSQL `raw` schema via dlt."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

import dlt
import psycopg2
import requests

JST = dt.timezone(dt.timedelta(hours=9))
NOW_UTC = dt.datetime.now(dt.timezone.utc)
SNAPSHOT_DATE = NOW_UTC.astimezone(JST).date().isoformat()
INGESTED_AT = NOW_UTC.isoformat()
TIMEOUT = 15


def env_req(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return val


def get_json(url: str, headers: dict[str, str], params: dict[str, Any] | None = None) -> Any:
    resp = requests.get(url, headers=headers, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def post_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> Any:
    resp = requests.post(url, json=payload, headers=headers, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def ensure_empty_raw_tables(dsn: str) -> None:
    """Guarantee tables that may legitimately have 0 rows (e.g. MFA devices, problems) exist."""
    ddl = """
    CREATE TABLE IF NOT EXISTS raw.authentik_authenticators (
        snapshot_date TEXT NOT NULL,
        device_pk TEXT NOT NULL,
        user_pk BIGINT,
        device_name TEXT,
        verbose_name TEXT,
        confirmed BOOLEAN,
        ingested_at TIMESTAMPTZ
    );
    CREATE TABLE IF NOT EXISTS raw.zabbix_problems (
        snapshot_date TEXT NOT NULL,
        eventid TEXT NOT NULL,
        objectid TEXT,
        clock_epoch BIGINT,
        problem_name TEXT,
        severity INTEGER,
        acknowledged INTEGER,
        ingested_at TIMESTAMPTZ
    );
    CREATE TABLE IF NOT EXISTS raw.n8n_workflows (
        workflow_id TEXT NOT NULL,
        workflow_name TEXT,
        active BOOLEAN,
        created_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ,
        ingested_at TIMESTAMPTZ
    );
    CREATE TABLE IF NOT EXISTS raw.n8n_executions (
        execution_id BIGINT NOT NULL,
        workflow_id TEXT,
        mode TEXT,
        status TEXT,
        finished BOOLEAN,
        started_at TIMESTAMPTZ,
        stopped_at TIMESTAMPTZ,
        ingested_at TIMESTAMPTZ
    );
    """
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    with conn:
        with conn.cursor() as cur:
            cur.execute(ddl)


@dlt.resource(name="authentik_users", write_disposition="merge", primary_key=["snapshot_date", "user_pk"])
def authentik_users():
    base = env_req("AUTHENTIK_API_URL").rstrip("/")
    headers = {"Authorization": f"Bearer {env_req('AUTHENTIK_API_TOKEN')}"}
    data = get_json(f"{base}/core/users/", headers, params={"page_size": 100})
    for u in data.get("results") or []:
        groups = [g.get("name", "") for g in (u.get("groups_obj") or [])]
        yield {
            "snapshot_date": SNAPSHOT_DATE,
            "user_pk": int(u["pk"]),
            "username": u.get("username"),
            "name": u.get("name"),
            "user_type": u.get("type"),
            "is_active": bool(u.get("is_active", False)),
            "is_superuser": bool(u.get("is_superuser", False)),
            "last_login": u.get("last_login"),
            "date_joined": u.get("date_joined"),
            "groups_csv": ",".join(sorted(g for g in groups if g)),
            "ingested_at": INGESTED_AT,
        }


@dlt.resource(name="authentik_authenticators", write_disposition="merge", primary_key=["snapshot_date", "device_pk"])
def authentik_authenticators():
    base = env_req("AUTHENTIK_API_URL").rstrip("/")
    headers = {"Authorization": f"Bearer {env_req('AUTHENTIK_API_TOKEN')}"}
    users = get_json(f"{base}/core/users/", headers, params={"page_size": 100}).get("results") or []
    for u in users:
        user_pk = int(u["pk"])
        items = get_json(f"{base}/authenticators/admin/all/", headers, params={"user": user_pk})
        if isinstance(items, dict):
            items = items.get("results") or []
        for d in items:
            model_name = d.get("meta_model_name") or d.get("type") or "device"
            yield {
                "snapshot_date": SNAPSHOT_DATE,
                "device_pk": f"{model_name}:{d.get('pk')}",
                "user_pk": user_pk,
                "device_name": d.get("name"),
                "verbose_name": d.get("verbose_name"),
                "confirmed": bool(d.get("confirmed", False)),
                "ingested_at": INGESTED_AT,
            }


@dlt.resource(name="authentik_events", write_disposition="merge", primary_key="event_pk")
def authentik_events():
    base = env_req("AUTHENTIK_API_URL").rstrip("/")
    headers = {"Authorization": f"Bearer {env_req('AUTHENTIK_API_TOKEN')}"}
    data = get_json(f"{base}/events/events/", headers, params={"page_size": 200})
    for e in data.get("results") or []:
        user_obj = e.get("user") or {}
        yield {
            "event_pk": str(e["pk"]),
            "action": e.get("action"),
            "created": e.get("created"),
            "username": user_obj.get("username"),
            "client_ip": e.get("client_ip"),
            "app": e.get("app"),
            "ingested_at": INGESTED_AT,
        }


@dlt.resource(name="netbox_devices", write_disposition="merge", primary_key=["snapshot_date", "device_id"])
def netbox_devices():
    base = env_req("NETBOX_API_URL").rstrip("/")
    headers = {"Authorization": f"Token {env_req('NETBOX_API_TOKEN')}"}
    data = get_json(f"{base}/dcim/devices/", headers, params={"limit": 200})
    for d in data.get("results") or []:
        yield {
            "snapshot_date": SNAPSHOT_DATE,
            "device_id": int(d["id"]),
            "device_name": d.get("name"),
            "role_name": (d.get("role") or {}).get("name"),
            "device_type": (d.get("device_type") or {}).get("display"),
            "status": (d.get("status") or {}).get("value"),
            "primary_ip": (d.get("primary_ip") or {}).get("address"),
            "site_name": (d.get("site") or {}).get("name"),
            "ingested_at": INGESTED_AT,
        }


@dlt.resource(name="fleet_hosts", write_disposition="merge", primary_key=["snapshot_date", "host_id"])
def fleet_hosts():
    base = env_req("FLEET_API_URL").rstrip("/")
    headers = {"Authorization": f"Bearer {env_req('FLEET_API_TOKEN')}"}
    data = get_json(f"{base}/hosts", headers)
    for h in data.get("hosts") or []:
        issues = h.get("issues") or {}
        yield {
            "snapshot_date": SNAPSHOT_DATE,
            "host_id": int(h["id"]),
            "hostname": h.get("hostname"),
            "display_name": h.get("display_name"),
            "platform": h.get("platform"),
            "os_version": h.get("os_version"),
            "osquery_version": h.get("osquery_version"),
            "status": h.get("status"),
            "seen_time": h.get("seen_time"),
            "disk_free_gb": float(h.get("gigs_disk_space_available") or 0.0),
            "disk_free_pct": float(h.get("percent_disk_space_available") or 0.0),
            "failing_policies_count": int(issues.get("failing_policies_count") or 0),
            "critical_vulnerabilities_count": int(issues.get("critical_vulnerabilities_count") or 0),
            "total_issues_count": int(issues.get("total_issues_count") or 0),
            "ingested_at": INGESTED_AT,
        }


@dlt.resource(name="fleet_vulnerabilities", write_disposition="merge", primary_key=["snapshot_date", "cve"], columns={"cvss_score": {"data_type": "double", "nullable": True}, "epss_probability": {"data_type": "double", "nullable": True}, "cisa_known_exploit": {"data_type": "bool", "nullable": True}})
def fleet_vulnerabilities():
    base = env_req("FLEET_API_URL").rstrip("/")
    headers = {"Authorization": f"Bearer {env_req('FLEET_API_TOKEN')}"}
    data = get_json(
        f"{base}/vulnerabilities",
        headers,
        params={"per_page": 200, "order_key": "hosts_count", "order_direction": "desc"},
    )
    for v in data.get("vulnerabilities") or []:
        yield {
            "snapshot_date": SNAPSHOT_DATE,
            "cve": v.get("cve"),
            "hosts_count": int(v.get("hosts_count") or 0),
            "cvss_score": float(v["cvss_score"]) if v.get("cvss_score") is not None else None,
            "epss_probability": float(v["epss_probability"]) if v.get("epss_probability") is not None else None,
            "cisa_known_exploit": bool(v.get("cisa_known_exploit", False)),
            "details_link": v.get("details_link"),
            "created_at": v.get("created_at"),
            "hosts_count_updated_at": v.get("hosts_count_updated_at"),
            "ingested_at": INGESTED_AT,
        }


@dlt.resource(name="fleet_vulnerable_software", write_disposition="merge", primary_key=["snapshot_date", "software_id"])
def fleet_vulnerable_software():
    base = env_req("FLEET_API_URL").rstrip("/")
    headers = {"Authorization": f"Bearer {env_req('FLEET_API_TOKEN')}"}
    data = get_json(
        f"{base}/software",
        headers,
        params={"vulnerable": "true", "per_page": 100, "order_key": "hosts_count", "order_direction": "desc"},
    )
    for s in data.get("software") or []:
        vulns = s.get("vulnerabilities") or []
        top_cve = vulns[0].get("cve") if vulns else None
        yield {
            "snapshot_date": SNAPSHOT_DATE,
            "software_id": int(s["id"]),
            "name": s.get("name"),
            "version": s.get("version"),
            "source": s.get("source"),
            "hosts_count": int(s.get("hosts_count") or 0),
            "vuln_count": len(vulns),
            "top_cve": top_cve,
            "ingested_at": INGESTED_AT,
        }


@dlt.resource(name="zabbix_hosts", write_disposition="merge", primary_key=["snapshot_date", "hostid"])
def zabbix_hosts():
    url = env_req("ZABBIX_API_URL")
    headers = {"Authorization": f"Bearer {env_req('ZABBIX_API_TOKEN')}"}
    res = post_json(
        url,
        {
            "jsonrpc": "2.0",
            "method": "host.get",
            "params": {
                "output": ["hostid", "host", "name", "status", "active_available"],
                "selectInterfaces": ["ip", "available"],
            },
            "id": 1,
        },
        headers,
    )
    for h in res.get("result") or []:
        ifaces = h.get("interfaces") or []
        ip = ifaces[0].get("ip") if ifaces else None
        avail = int(ifaces[0].get("available") or 0) if ifaces else 0
        yield {
            "snapshot_date": SNAPSHOT_DATE,
            "hostid": str(h["hostid"]),
            "host": h.get("host"),
            "display_name": h.get("name"),
            "status": int(h.get("status") or 0),
            "active_available": int(h.get("active_available") or 0),
            "interface_ip": ip,
            "interface_available": avail,
            "ingested_at": INGESTED_AT,
        }


@dlt.resource(name="zabbix_problems", write_disposition="merge", primary_key=["snapshot_date", "eventid"])
def zabbix_problems():
    url = env_req("ZABBIX_API_URL")
    headers = {"Authorization": f"Bearer {env_req('ZABBIX_API_TOKEN')}"}
    res = post_json(
        url,
        {
            "jsonrpc": "2.0",
            "method": "problem.get",
            "params": {
                "output": ["eventid", "objectid", "clock", "name", "severity", "acknowledged"],
                "recent": True,
            },
            "id": 2,
        },
        headers,
    )
    for p in res.get("result") or []:
        yield {
            "snapshot_date": SNAPSHOT_DATE,
            "eventid": str(p["eventid"]),
            "objectid": str(p.get("objectid") or ""),
            "clock_epoch": int(p.get("clock") or 0),
            "problem_name": p.get("name"),
            "severity": int(p.get("severity") or 0),
            "acknowledged": int(p.get("acknowledged") or 0),
            "ingested_at": INGESTED_AT,
        }


@dlt.resource(name="backup_runs", write_disposition="merge", primary_key="run_file", columns={"finished_at_jst": {"data_type": "text", "nullable": True}, "error_message": {"data_type": "text", "nullable": True}, "snapshot_id": {"data_type": "text", "nullable": True}})
def backup_runs():
    runs_dir = Path(os.environ.get("BACKUP_RUNS_DIR", "/backup-runs"))
    if not runs_dir.exists():
        return
    for p in sorted(runs_dir.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        manifest = d.get("manifest") or {}
        dbs = manifest.get("databases") or {}
        total_bytes = sum(int((v or {}).get("bytes") or 0) for v in dbs.values())
        restic_info = d.get("restic") or {}
        yield {
            "run_file": p.name,
            "started_at_jst": d.get("started_at_jst"),
            "finished_at_jst": d.get("finished_at_jst"),
            "mode": d.get("mode"),
            "status": d.get("status"),
            "duration_sec": float(d.get("duration_sec") or 0.0),
            "snapshot_id": restic_info.get("snapshot_id") or d.get("snapshot_id"),
            "total_dump_bytes": total_bytes,
            "db_count": len(dbs),
            "zabbix_bytes": int((dbs.get("zabbix") or {}).get("bytes") or 0),
            "netbox_bytes": int((dbs.get("netbox") or {}).get("bytes") or 0),
            "authentik_bytes": int((dbs.get("authentik") or {}).get("bytes") or 0),
            "fleet_bytes": int((dbs.get("fleet") or {}).get("bytes") or 0),
            "infra_dwh_bytes": int((dbs.get("infra_dwh") or {}).get("bytes") or 0),
            "error_message": d.get("error"),
            "ingested_at": INGESTED_AT,
        }



def _n8n_ro_conn():
    pw = os.environ.get("N8N_RO_PASSWORD", "").strip()
    if not pw:
        return None
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "infra-dwh-postgres"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname="n8n_app",
        user="n8n_ro",
        password=pw,
        options="-c default_transaction_read_only=on",
    )


@dlt.resource(name="n8n_workflows", write_disposition="merge", primary_key="workflow_id")
def n8n_workflows():
    conn = _n8n_ro_conn()
    if conn is None:
        return
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, name, active, "createdAt", "updatedAt"
                    FROM public.workflow_entity
                    WHERE COALESCE("isArchived", false) = false
                    ORDER BY id
                    """
                )
                for wid, wname, active, created_at, updated_at in cur.fetchall():
                    yield {
                        "workflow_id": str(wid),
                        "workflow_name": wname,
                        "active": bool(active),
                        "created_at": created_at.isoformat() if created_at else None,
                        "updated_at": updated_at.isoformat() if updated_at else None,
                        "ingested_at": INGESTED_AT,
                    }
    finally:
        conn.close()


@dlt.resource(
    name="n8n_executions",
    write_disposition="merge",
    primary_key="execution_id",
    columns={"started_at": {"data_type": "timestamp", "nullable": True}, "stopped_at": {"data_type": "timestamp", "nullable": True}},
)
def n8n_executions():
    conn = _n8n_ro_conn()
    if conn is None:
        return
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, "workflowId", mode, status, finished, "startedAt", "stoppedAt"
                    FROM public.execution_entity
                    WHERE "deletedAt" IS NULL
                    ORDER BY id DESC
                    LIMIT 2000
                    """
                )
                for eid, wid, mode, status, finished, started_at, stopped_at in cur.fetchall():
                    yield {
                        "execution_id": int(eid),
                        "workflow_id": str(wid) if wid else None,
                        "mode": mode,
                        "status": status,
                        "finished": bool(finished) if finished is not None else False,
                        "started_at": started_at.isoformat() if started_at else None,
                        "stopped_at": stopped_at.isoformat() if stopped_at else None,
                        "ingested_at": INGESTED_AT,
                    }
    finally:
        conn.close()


def main() -> None:
    dsn = (
        f"postgresql://{env_req('PGUSER')}:{env_req('PGPASSWORD')}"
        f"@{os.environ.get('PGHOST', 'infra-dwh-postgres')}:{os.environ.get('PGPORT', '5432')}"
        f"/{os.environ.get('PGDATABASE', 'infra_dwh')}"
    )
    pipeline = dlt.pipeline(
        pipeline_name="infra_dwh",
        destination=dlt.destinations.postgres(credentials=dsn),
        dataset_name="raw",
        pipelines_dir="/tmp/dlt",
    )
    info = pipeline.run(
        [
            authentik_users(),
            authentik_authenticators(),
            authentik_events(),
            netbox_devices(),
            fleet_hosts(),
            fleet_vulnerabilities(),
            fleet_vulnerable_software(),
            zabbix_hosts(),
            zabbix_problems(),
            backup_runs(),
            n8n_workflows(),
            n8n_executions(),
        ]
    )
    info.raise_on_failed_jobs()
    ensure_empty_raw_tables(dsn)

    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'raw'
                  AND table_name NOT LIKE '_dlt_%'
                ORDER BY table_name
                """
            )
            tables = [r[0] for r in cur.fetchall()]
            counts = {}
            for t in tables:
                cur.execute(f"SELECT COUNT(*) FROM raw.{t}")
                counts[t] = cur.fetchone()[0]

    print("INGEST_SUMMARY_JSON=" + json.dumps({"snapshot_date": SNAPSHOT_DATE, "row_counts": counts}))


if __name__ == "__main__":
    main()
