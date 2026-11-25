#!/usr/bin/env bash
set -euo pipefail

psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname infra_dwh \
  -v rag_pw="$RAG_READER_PASSWORD" <<'EOSQL'
  DO $$
  BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rag_reader') THEN
      CREATE ROLE rag_reader WITH LOGIN;
    END IF;
  END$$;

  ALTER ROLE rag_reader WITH LOGIN PASSWORD :'rag_pw';

  GRANT CONNECT ON DATABASE infra_dwh TO rag_reader;
  CREATE SCHEMA IF NOT EXISTS rag AUTHORIZATION etl_writer;
  GRANT ALL PRIVILEGES ON SCHEMA rag TO etl_writer;

  -- Strictly least privilege: rag_reader can SELECT on rag & mart ONLY (NO raw, NO write)
  REVOKE ALL ON SCHEMA raw, stg, public FROM rag_reader;
  GRANT USAGE ON SCHEMA rag, mart TO rag_reader;
  ALTER DEFAULT PRIVILEGES FOR ROLE etl_writer IN SCHEMA rag
    GRANT SELECT ON TABLES TO rag_reader;
  ALTER DEFAULT PRIVILEGES FOR ROLE etl_writer IN SCHEMA mart
    GRANT SELECT ON TABLES TO rag_reader;

  -- bi_reader can view RAG governance/eval metadata in Grafana (not raw secrets)
  GRANT USAGE ON SCHEMA rag TO bi_reader;

  CREATE TABLE IF NOT EXISTS rag.documents (
    doc_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_path TEXT NOT NULL,
    title TEXT NOT NULL,
    doc_status TEXT,
    classification TEXT NOT NULL DEFAULT 'internal',
    content_sha256 TEXT NOT NULL,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    redaction_count INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
  );
  ALTER TABLE rag.documents OWNER TO etl_writer;

  CREATE TABLE IF NOT EXISTS rag.chunks (
    chunk_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL REFERENCES rag.documents(doc_id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    section_heading TEXT NOT NULL,
    content TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    token_estimate INTEGER NOT NULL DEFAULT 0,
    embedding vector(1024) NOT NULL,
    redaction_hits JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
  );
  ALTER TABLE rag.chunks OWNER TO etl_writer;

  CREATE INDEX IF NOT EXISTS idx_rag_chunks_doc_id ON rag.chunks (doc_id);
  CREATE INDEX IF NOT EXISTS idx_rag_chunks_embedding_hnsw
    ON rag.chunks USING hnsw (embedding vector_cosine_ops);

  CREATE TABLE IF NOT EXISTS rag.eval_runs (
    run_id TEXT PRIMARY KEY,
    ran_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    embedding_model TEXT NOT NULL,
    query_count INTEGER NOT NULL,
    hit_at_1 NUMERIC(5,2) NOT NULL,
    hit_at_3 NUMERIC(5,2) NOT NULL,
    hit_at_5 NUMERIC(5,2) NOT NULL,
    mrr_at_5 NUMERIC(6,4) NOT NULL,
    avg_latency_ms NUMERIC(8,2) NOT NULL,
    leakage_violations INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL
  );
  ALTER TABLE rag.eval_runs OWNER TO etl_writer;

  CREATE TABLE IF NOT EXISTS rag.eval_results (
    run_id TEXT NOT NULL REFERENCES rag.eval_runs(run_id) ON DELETE CASCADE,
    query_id TEXT NOT NULL,
    query_text TEXT NOT NULL,
    expected_doc_ids TEXT NOT NULL,
    top1_doc_id TEXT,
    top3_doc_ids TEXT,
    hit_at_1 BOOLEAN NOT NULL,
    hit_at_3 BOOLEAN NOT NULL,
    hit_at_5 BOOLEAN NOT NULL,
    reciprocal_rank NUMERIC(6,4) NOT NULL,
    latency_ms NUMERIC(8,2) NOT NULL,
    PRIMARY KEY (run_id, query_id)
  );
  ALTER TABLE rag.eval_results OWNER TO etl_writer;

  GRANT SELECT ON ALL TABLES IN SCHEMA rag, mart TO rag_reader;
  GRANT SELECT ON rag.documents, rag.eval_runs, rag.eval_results TO bi_reader;
EOSQL
