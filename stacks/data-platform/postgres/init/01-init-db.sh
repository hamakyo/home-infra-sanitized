#!/usr/bin/env bash
set -euo pipefail

psql -v ON_ERROR_STOP=1   --username "$POSTGRES_USER"   --dbname postgres   -v etl_pw="$ETL_WRITER_PASSWORD"   -v bi_pw="$BI_READER_PASSWORD"   -v grafana_pw="$GRAFANA_DB_PASSWORD" <<'EOSQL'
  CREATE DATABASE infra_dwh;
  CREATE DATABASE grafana_app;

  CREATE ROLE etl_writer WITH LOGIN PASSWORD :'etl_pw';
  CREATE ROLE bi_reader WITH LOGIN PASSWORD :'bi_pw';
  CREATE ROLE grafana_app WITH LOGIN PASSWORD :'grafana_pw';

  ALTER DATABASE grafana_app OWNER TO grafana_app;
  REVOKE ALL ON DATABASE infra_dwh FROM PUBLIC;
  REVOKE ALL ON DATABASE grafana_app FROM PUBLIC;
  GRANT CONNECT ON DATABASE infra_dwh TO etl_writer, bi_reader;
  GRANT CREATE ON DATABASE infra_dwh TO etl_writer;
  GRANT CONNECT ON DATABASE grafana_app TO grafana_app;
EOSQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname infra_dwh <<'EOSQL'
  CREATE EXTENSION IF NOT EXISTS vector;

  CREATE SCHEMA IF NOT EXISTS raw AUTHORIZATION etl_writer;
  CREATE SCHEMA IF NOT EXISTS stg AUTHORIZATION etl_writer;
  CREATE SCHEMA IF NOT EXISTS mart AUTHORIZATION etl_writer;

  REVOKE CREATE ON SCHEMA public FROM PUBLIC;

  GRANT ALL PRIVILEGES ON SCHEMA raw, stg, mart TO etl_writer;

  REVOKE ALL ON SCHEMA raw, stg, mart, public FROM bi_reader;
  GRANT USAGE ON SCHEMA mart TO bi_reader;
  ALTER DEFAULT PRIVILEGES FOR ROLE etl_writer IN SCHEMA mart
    GRANT SELECT ON TABLES TO bi_reader;
EOSQL
