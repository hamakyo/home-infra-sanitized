SELECT
    run_file,
    NULLIF((started_at_jst)::text, '')::timestamptz AS started_at,
    NULLIF((finished_at_jst)::text, '')::timestamptz AS finished_at,
    mode,
    status,
    duration_sec::double precision AS duration_sec,
    snapshot_id,
    total_dump_bytes::bigint AS total_dump_bytes,
    ROUND((total_dump_bytes::numeric / 1048576.0), 2) AS total_dump_mib,
    db_count::int AS db_count,
    ROUND((zabbix_bytes::numeric / 1048576.0), 2) AS zabbix_mib,
    ROUND((netbox_bytes::numeric / 1048576.0), 2) AS netbox_mib,
    ROUND((authentik_bytes::numeric / 1048576.0), 2) AS authentik_mib,
    ROUND((fleet_bytes::numeric / 1048576.0), 2) AS fleet_mib,
    ROUND((infra_dwh_bytes::numeric / 1048576.0), 2) AS infra_dwh_mib,
    error_message,
    ingested_at::timestamptz AS ingested_at
FROM {{ source("raw", "backup_runs") }}
