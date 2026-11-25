WITH src AS (
    SELECT
        snapshot_date::date AS snapshot_date,
        hostid,
        host,
        LOWER(TRIM(host)) AS canonical_hostname,
        display_name,
        status::int AS status,
        active_available::int AS active_available,
        interface_ip,
        interface_available::int AS interface_available,
        (LOWER(TRIM(host)) = 'zabbix server') AS is_zabbix_internal,
        ingested_at::timestamptz AS ingested_at,
        MAX(snapshot_date::date) OVER () AS latest_snapshot_date
    FROM {{ source("raw", "zabbix_hosts") }}
)
SELECT
    *,
    (snapshot_date = latest_snapshot_date) AS is_latest
FROM src
