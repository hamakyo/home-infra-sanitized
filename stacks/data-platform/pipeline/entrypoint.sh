#!/usr/bin/env bash
set -euo pipefail

mkdir -p /tmp/dlt /tmp/dbt_target /tmp/dbt_logs

echo "[1/6] Ingesting 5 Infra sources via dlt..."
python3 /app/pipeline/ingest.py

echo "[2/6] Loading dbt seeds..."
dbt seed --project-dir /app/dbt --profiles-dir /app/dbt --target-path /tmp/dbt_target --log-path /tmp/dbt_logs

echo "[3/6] Building dbt staging & mart models..."
dbt run --project-dir /app/dbt --profiles-dir /app/dbt --target-path /tmp/dbt_target --log-path /tmp/dbt_logs

echo "[4/6] Running dbt data quality & IT governance tests..."
set +e
dbt test --project-dir /app/dbt --profiles-dir /app/dbt --target-path /tmp/dbt_target --log-path /tmp/dbt_logs
DBT_TEST_RC=$?
set -e
python3 /app/pipeline/summarize_run.py /tmp/dbt_target/run_results.json "${DBT_TEST_RC}"

echo "[5/6] Syncing governed RAG index (pgvector + redaction gate)..."
python3 /app/rag/sync_rag.py

echo "[6/6] Running automated RAG retrieval & leakage evaluation..."
set +e
python3 /app/rag/eval_rag.py
RAG_EVAL_RC=$?
set -e

if [ "${DBT_TEST_RC}" -ne 0 ] || [ "${RAG_EVAL_RC}" -ne 0 ]; then
  exit 1
fi
