SELECT
    run_file,
    started_at,
    finished_at,
    mode,
    status,
    duration_sec,
    snapshot_id,
    total_dump_bytes,
    total_dump_mib,
    db_count,
    zabbix_mib,
    netbox_mib,
    authentik_mib,
    fleet_mib,
    infra_dwh_mib,
    error_message,
    ROUND(EXTRACT(EPOCH FROM (NOW() - started_at)) / 3600.0, 2) AS hours_since_run,
    (started_at = MAX(started_at) FILTER (WHERE mode = 'backup' AND status = 'ok') OVER ()) AS is_latest_ok_backup
FROM {{ ref("stg_backup_runs") }}
ORDER BY started_at DESC
