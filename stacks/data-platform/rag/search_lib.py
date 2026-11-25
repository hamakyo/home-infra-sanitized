"""Shared Sakura AI Engine embedding client and pgvector+keyword RRF hybrid search."""

from __future__ import annotations

import os
import re
import time
from typing import Any

import psycopg2
import psycopg2.extras
import requests

DEFAULT_BASE_URL = "https://api.ai.sakura.ad.jp/v1"
DEFAULT_MODEL = "multilingual-e5-large"
TIMEOUT = 25


def embed_texts(
    texts: list[str],
    *,
    prefix: str = "passage: ",
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> list[list[float]]:
    """Embed texts via Sakura AI Engine (free-tier domestic ZDR, OpenAI-compatible)."""
    if not texts:
        return []
    key = (api_key or os.environ.get("SAKURA_AI_API_KEY", "")).strip()
    if not key:
        raise RuntimeError("Missing SAKURA_AI_API_KEY")
    url = (base_url or os.environ.get("SAKURA_AI_BASE_URL", DEFAULT_BASE_URL)).rstrip("/") + "/embeddings"
    mdl = (model or os.environ.get("RAG_EMBEDDING_MODEL", DEFAULT_MODEL)).strip()

    # multilingual-e5-large enforces a strict 512-token context limit.
    max_embed_chars = 650
    prefixed = [f"{prefix}{t[:max_embed_chars]}" for t in texts]
    last_err: Exception | None = None
    for attempt in range(4):
        try:
            resp = requests.post(
                url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": mdl, "input": prefixed},
                timeout=TIMEOUT,
            )
            if resp.status_code == 429 or resp.status_code >= 500:
                last_err = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                time.sleep(2 ** attempt)
                continue
            if resp.status_code >= 400:
                raise ValueError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            data = resp.json().get("data") or []
            data.sort(key=lambda x: x["index"])
            return [row["embedding"] for row in data]
        except ValueError:
            raise
        except Exception as exc:
            last_err = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Embedding request failed after retries: {last_err}")


def vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"


def extract_keywords(query: str) -> list[str]:
    """Extract lightweight alphanumeric & CJK keyword tokens for hybrid RRF boost."""
    tokens = re.findall(r"[A-Za-z0-9_.-]{2,}|[一-龥ァ-ヴー]{2,}", query)
    dedup: list[str] = []
    for tok in tokens:
        low = tok.lower()
        if low not in dedup:
            dedup.append(low)
    return dedup[:8]


def hybrid_search(
    conn: Any,
    query: str,
    *,
    top_k: int = 5,
    source_type: str | None = None,
    api_key: str | None = None,
) -> list[dict[str, Any]]:
    """Hybrid search combining pgvector cosine similarity + keyword match via RRF."""
    q_vec = vec_literal(embed_texts([query], prefix="query: ", api_key=api_key)[0])
    keywords = extract_keywords(query)
    top_k = max(1, min(int(top_k), 15))
    cand_limit = max(20, top_k * 4)

    filter_sql = ""
    params_vec: list[Any] = [q_vec]
    if source_type:
        filter_sql = " AND d.source_type = %s "
        params_vec.append(source_type)
    params_vec.extend([q_vec, cand_limit])

    vec_sql = f"""
        SELECT
            c.chunk_id,
            c.doc_id,
            d.source_type,
            d.source_path,
            d.title,
            d.doc_status,
            c.section_heading,
            c.content,
            1.0 - (c.embedding <=> %s::vector) AS cosine_sim
        FROM rag.chunks c
        INNER JOIN rag.documents d ON c.doc_id = d.doc_id
        WHERE TRUE {filter_sql}
        ORDER BY c.embedding <=> %s::vector ASC
        LIMIT %s
    """

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(vec_sql, params_vec)
        vec_rows = list(cur.fetchall())

        kw_rows: list[dict[str, Any]] = []
        if keywords:
            score_parts = []
            kw_params: list[Any] = []
            for kw in keywords:
                like = f"%{kw}%"
                score_parts.append(
                    "(CASE WHEN LOWER(c.content) LIKE %s OR LOWER(c.section_heading) LIKE %s OR LOWER(d.title) LIKE %s THEN 1 ELSE 0 END)"
                )
                kw_params.extend([like, like, like])
            score_expr = " + ".join(score_parts)
            kw_filter = ""
            if source_type:
                kw_filter = " AND d.source_type = %s "
                kw_params.append(source_type)
            kw_params.append(cand_limit)
            kw_sql = f"""
                SELECT * FROM (
                    SELECT
                        c.chunk_id,
                        c.doc_id,
                        d.source_type,
                        d.source_path,
                        d.title,
                        d.doc_status,
                        c.section_heading,
                        c.content,
                        ({score_expr}) AS kw_hits
                    FROM rag.chunks c
                    INNER JOIN rag.documents d ON c.doc_id = d.doc_id
                    WHERE TRUE {kw_filter}
                ) sub
                WHERE kw_hits > 0
                ORDER BY kw_hits DESC, chunk_id ASC
                LIMIT %s
            """
            cur.execute(kw_sql, kw_params)
            kw_rows = list(cur.fetchall())

    # Reciprocal Rank Fusion (k = 60), weighted slightly toward dense multilingual-e5-large
    rrf_k = 60.0
    combined: dict[str, dict[str, Any]] = {}
    for rank, row in enumerate(vec_rows, start=1):
        cid = row["chunk_id"]
        entry = dict(row)
        entry["rrf_score"] = 1.2 / (rrf_k + rank)
        entry["cosine_sim"] = round(float(row["cosine_sim"]), 4)
        entry["kw_hits"] = 0
        combined[cid] = entry

    for rank, row in enumerate(kw_rows, start=1):
        cid = row["chunk_id"]
        if cid not in combined:
            entry = dict(row)
            entry["rrf_score"] = 0.0
            entry["cosine_sim"] = None
            combined[cid] = entry
        combined[cid]["rrf_score"] += 0.8 / (rrf_k + rank)
        combined[cid]["kw_hits"] = int(row["kw_hits"])

    ranked = sorted(combined.values(), key=lambda x: x["rrf_score"], reverse=True)[:top_k]
    for item in ranked:
        item["score"] = round(float(item.pop("rrf_score")), 5)
    return ranked
