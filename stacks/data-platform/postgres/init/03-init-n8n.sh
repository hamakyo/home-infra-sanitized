#!/usr/bin/env bash
set -euo pipefail

# Idempotently create dedicated n8n_app DB/role and least-privilege n8n_ro audit role
# on infra-dwh-postgres. n8n_app has ZERO access to infra_dwh or grafana_app.

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  -v n8n_pw="$N8N_DB_PASSWORD" \
  -v n8n_ro_pw="$N8N_RO_PASSWORD" <<'EOSQL'
SELECT format('CREATE ROLE n8n_app LOGIN PASSWORD %L', :'n8n_pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'n8n_app') \gexec

SELECT format('ALTER ROLE n8n_app WITH LOGIN PASSWORD %L', :'n8n_pw') \gexec

SELECT format('CREATE ROLE n8n_ro LOGIN PASSWORD %L', :'n8n_ro_pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'n8n_ro') \gexec

SELECT format('ALTER ROLE n8n_ro WITH LOGIN PASSWORD %L', :'n8n_ro_pw') \gexec

SELECT 'CREATE DATABASE n8n_app OWNER n8n_app'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'n8n_app') \gexec

GRANT CONNECT ON DATABASE infra_dwh TO etl_writer, bi_reader, rag_reader;
REVOKE CONNECT ON DATABASE infra_dwh FROM PUBLIC, n8n_app, n8n_ro;
GRANT CONNECT ON DATABASE grafana_app TO grafana_app;
REVOKE CONNECT ON DATABASE grafana_app FROM PUBLIC, n8n_app, n8n_ro, etl_writer, bi_reader, rag_reader;
REVOKE ALL ON DATABASE n8n_app FROM PUBLIC;
GRANT ALL ON DATABASE n8n_app TO n8n_app;
GRANT CONNECT ON DATABASE n8n_app TO n8n_ro;
EOSQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname n8n_app <<'EOSQL'
GRANT USAGE ON SCHEMA public TO n8n_ro;
REVOKE CREATE ON SCHEMA public FROM n8n_ro;
ALTER ROLE n8n_ro IN DATABASE n8n_app SET default_transaction_read_only = on;

-- Re-run this script once after n8n runs initial migrations so least-privilege
-- table grants apply to workflow_entity / execution_entity only (never credentials_entity / execution_data).
DO $$
BEGIN
  IF to_regclass('public.workflow_entity') IS NOT NULL THEN
    GRANT SELECT ON TABLE public.workflow_entity TO n8n_ro;
  END IF;
  IF to_regclass('public.execution_entity') IS NOT NULL THEN
    GRANT SELECT ON TABLE public.execution_entity TO n8n_ro;
  END IF;
  IF to_regclass('public.credentials_entity') IS NOT NULL THEN
    REVOKE ALL ON TABLE public.credentials_entity FROM n8n_ro;
  END IF;
  IF to_regclass('public.execution_data') IS NOT NULL THEN
    REVOKE ALL ON TABLE public.execution_data FROM n8n_ro;
  END IF;
END
$$;
EOSQL
