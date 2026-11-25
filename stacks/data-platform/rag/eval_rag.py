#!/usr/bin/env python3
"""Automated RAG Retrieval Quality & Governance Leakage Evaluator."""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import psycopg2
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from redact import scan_leakage
from search_lib import DEFAULT_MODEL, hybrid_search

DATASET_PATH = Path(__file__).resolve().parent / "eval_dataset.yaml"
MIN_HIT_AT_3_PCT = 80.0


def main() -> int:
    dsn = (
        f"postgresql://{os.environ['PGUSER']}:{os.environ['PGPASSWORD']}"
        f"@{os.environ.get('PGHOST', 'infra-dwh-postgres')}:{os.environ.get('PGPORT', '5432')}"
        f"/{os.environ.get('PGDATABASE', 'infra_dwh')}"
    )
    dataset = yaml.safe_load(DATASET_PATH.read_text(encoding="utf-8"))
    queries = dataset["queries"]
    run_id = dt.datetime.now(dt.timezone.utc).strftime("eval-%Y%m%dT%H%M%SZ")
    model = os.environ.get("RAG_EMBEDDING_MODEL", DEFAULT_MODEL)

    with psycopg2.connect(dsn) as conn:
        # 1. Governance leakage assertion over ALL stored chunks
        leakage_violations = 0
        with conn.cursor() as cur:
            cur.execute("SELECT chunk_id, content FROM rag.chunks;")
            for cid, content in cur.fetchall():
                if scan_leakage(content):
                    leakage_violations += 1

        # 2. Evaluate retrieval quality on golden queries
        results = []
        hit1_cnt = hit3_cnt = hit5_cnt = 0
        rr_sum = 0.0
        lat_sum = 0.0

        for item in queries:
            t0 = time.monotonic()
            hits = hybrid_search(conn, item["query"], top_k=5)
            lat_ms = round((time.monotonic() - t0) * 1000.0, 2)
            lat_sum += lat_ms

            expected = set(item["expected_doc_ids"])
            ranked_docs = []
            for h in hits:
                if h["doc_id"] not in ranked_docs:
                    ranked_docs.append(h["doc_id"])

            h1 = bool(ranked_docs[:1] and ranked_docs[0] in expected)
            h3 = any(d in expected for d in ranked_docs[:3])
            h5 = any(d in expected for d in ranked_docs[:5])
            rr = 0.0
            for idx, d in enumerate(ranked_docs[:5], start=1):
                if d in expected:
                    rr = round(1.0 / idx, 4)
                    break

            hit1_cnt += int(h1)
            hit3_cnt += int(h3)
            hit5_cnt += int(h5)
            rr_sum += rr

            results.append(
                {
                    "query_id": item["id"],
                    "query_text": item["query"],
                    "expected_doc_ids": ",".join(item["expected_doc_ids"]),
                    "top1_doc_id": ranked_docs[0] if ranked_docs else None,
                    "top3_doc_ids": ",".join(ranked_docs[:3]),
                    "hit_at_1": h1,
                    "hit_at_3": h3,
                    "hit_at_5": h5,
                    "reciprocal_rank": rr,
                    "latency_ms": lat_ms,
                }
            )

        n = len(queries)
        hit1_pct = round(hit1_cnt / n * 100.0, 2)
        hit3_pct = round(hit3_cnt / n * 100.0, 2)
        hit5_pct = round(hit5_cnt / n * 100.0, 2)
        mrr5 = round(rr_sum / n, 4)
        avg_lat = round(lat_sum / n, 2)
        status = "ok" if (leakage_violations == 0 and hit3_pct >= MIN_HIT_AT_3_PCT) else "failed"

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO rag.eval_runs
                    (run_id, ran_at, embedding_model, query_count, hit_at_1, hit_at_3, hit_at_5, mrr_at_5, avg_latency_ms, leakage_violations, status)
                VALUES (%s, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (run_id, model, n, hit1_pct, hit3_pct, hit5_pct, mrr5, avg_lat, leakage_violations, status),
            )
            for r in results:
                cur.execute(
                    """
                    INSERT INTO rag.eval_results
                        (run_id, query_id, query_text, expected_doc_ids, top1_doc_id, top3_doc_ids, hit_at_1, hit_at_3, hit_at_5, reciprocal_rank, latency_ms)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        r["query_id"],
                        r["query_text"],
                        r["expected_doc_ids"],
                        r["top1_doc_id"],
                        r["top3_doc_ids"],
                        r["hit_at_1"],
                        r["hit_at_3"],
                        r["hit_at_5"],
                        r["reciprocal_rank"],
                        r["latency_ms"],
                    ),
                )
        conn.commit()

    summary = {
        "run_id": run_id,
        "status": status,
        "query_count": n,
        "hit_at_1_pct": hit1_pct,
        "hit_at_3_pct": hit3_pct,
        "hit_at_5_pct": hit5_pct,
        "mrr_at_5": mrr5,
        "avg_latency_ms": avg_lat,
        "leakage_violations": leakage_violations,
        "misses_top3": [r["query_id"] for r in results if not r["hit_at_3"]],
    }
    print("RAG_EVAL_SUMMARY_JSON=" + json.dumps(summary, ensure_ascii=False))
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
