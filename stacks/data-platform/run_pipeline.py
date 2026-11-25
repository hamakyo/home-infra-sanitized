#!/usr/bin/env python3
"""Host-side wrapper for Infra DWH + Governed RAG daily pipeline (flock, timeout, audit log, Zabbix Trapper)."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

JST = dt.timezone(dt.timedelta(hours=9))
STACK_DIR = Path("/home/infra-admin/dev/home-infra/stacks/data-platform")
ENV_FILE = STACK_DIR / ".env"
STATE_DIR = Path("/home/infra-admin/.local/state/infra-dwh")
RUNS_DIR = STATE_DIR / "runs"
LOCK_FILE = STATE_DIR / "pipeline.lock"
TIMEOUT_SEC = 360
RETENTION_DAYS = 30


def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def send_zabbix_metrics(cfg: dict[str, str], metrics: dict[str, object]) -> dict[str, object]:
    host = cfg.get("ZABBIX_TRAPPER_HOST", "ubuntu-desktop")
    container = cfg.get("ZABBIX_SERVER_CONTAINER", "zabbix-server")
    lines = []
    for key, val in metrics.items():
        if isinstance(val, str):
            escaped = val.replace("\n", " ").replace("\r", " ").replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'"{host}" {key} "{escaped}"')
        else:
            lines.append(f'"{host}" {key} {val}')
    proc = subprocess.run(
        ["docker", "exec", "-i", container, "zabbix_sender", "-z", "127.0.0.1", "-i", "-"],
        input="\n".join(lines) + "\n",
        capture_output=True,
        text=True,
    )
    return {"returncode": proc.returncode, "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip()}


def main() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    RUNS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    cfg = load_env(ENV_FILE)

    with LOCK_FILE.open("w") as lf:
        try:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another Infra DWH pipeline run is already in progress.", file=sys.stderr)
            return 1

        started = dt.datetime.now(JST)
        t0 = time.monotonic()
        record: dict[str, object] = {
            "started_at_jst": started.isoformat(timespec="seconds"),
            "status": "error",
            "returncode": 1,
            "duration_sec": 0.0,
            "ingest_summary": None,
            "dbt_test_summary": None,
            "rag_sync_summary": None,
            "rag_eval_summary": None,
        }

        try:
            proc = subprocess.run(
                ["docker", "compose", "--profile", "job", "run", "--rm", "infra-dwh-pipeline"],
                cwd=str(STACK_DIR),
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SEC,
            )
            dur = round(time.monotonic() - t0, 2)
            record["duration_sec"] = dur
            record["returncode"] = proc.returncode
            for line in (proc.stdout + "\n" + proc.stderr).splitlines():
                if line.startswith("INGEST_SUMMARY_JSON="):
                    record["ingest_summary"] = json.loads(line.split("=", 1)[1])
                elif line.startswith("DBT_TEST_SUMMARY_JSON="):
                    record["dbt_test_summary"] = json.loads(line.split("=", 1)[1])
                elif line.startswith("RAG_SYNC_SUMMARY_JSON="):
                    record["rag_sync_summary"] = json.loads(line.split("=", 1)[1])
                elif line.startswith("RAG_EVAL_SUMMARY_JSON="):
                    record["rag_eval_summary"] = json.loads(line.split("=", 1)[1])
            record["status"] = "ok" if proc.returncode == 0 else "failed"
            if proc.returncode != 0:
                record["stderr_tail"] = (proc.stderr or proc.stdout)[-1500:]
        except subprocess.TimeoutExpired:
            record["duration_sec"] = round(time.monotonic() - t0, 2)
            record["status"] = "timeout"
            record["returncode"] = 124
        except Exception as exc:
            record["duration_sec"] = round(time.monotonic() - t0, 2)
            record["status"] = "exception"
            record["error"] = str(exc)

        record["finished_at_jst"] = dt.datetime.now(JST).isoformat(timespec="seconds")

        dbt_sum = record.get("dbt_test_summary") or {}
        ingest_sum = record.get("ingest_summary") or {}
        rag_sync = record.get("rag_sync_summary") or {}
        rag_eval = record.get("rag_eval_summary") or {}
        total_rows = sum((ingest_sum.get("row_counts") or {}).values()) if isinstance(ingest_sum, dict) else 0
        metrics = {
            "infra.dwh.status": 0 if record["status"] == "ok" else 1,
            "infra.dwh.duration_sec": record["duration_sec"],
            "infra.dwh.raw_rows": total_rows,
            "infra.dwh.dbt_warn": int(dbt_sum.get("warn", 0)) if isinstance(dbt_sum, dict) else 0,
            "infra.dwh.dbt_fail": int(dbt_sum.get("fail", 0)) + int(dbt_sum.get("error", 0)) if isinstance(dbt_sum, dict) else 0,
            "infra.rag.status": 0 if (isinstance(rag_eval, dict) and rag_eval.get("status") == "ok") else 1,
            "infra.rag.chunks": int(rag_sync.get("chunks", 0)) if isinstance(rag_sync, dict) else 0,
            "infra.rag.hit_at_3": float(rag_eval.get("hit_at_3_pct", 0.0)) if isinstance(rag_eval, dict) else 0.0,
            "infra.rag.leakage": int(rag_eval.get("leakage_violations", 0)) if isinstance(rag_eval, dict) else 0,
        }
        record["zabbix"] = send_zabbix_metrics(cfg, metrics)

        log_file = RUNS_DIR / f"{started.strftime('%Y-%m-%d_%H%M%S')}.json"
        log_file.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(log_file, 0o600)

        cutoff = time.time() - RETENTION_DAYS * 86400
        for old in RUNS_DIR.glob("*.json"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass

        print(
            json.dumps(
                {
                    k: record[k]
                    for k in [
                        "started_at_jst",
                        "status",
                        "duration_sec",
                        "ingest_summary",
                        "dbt_test_summary",
                        "rag_sync_summary",
                        "rag_eval_summary",
                        "zabbix",
                    ]
                },
                ensure_ascii=False,
            )
        )
        return 0 if record["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
