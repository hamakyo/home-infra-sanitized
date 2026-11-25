WITH src AS (
    SELECT
        snapshot_date::date AS snapshot_date,
        device_id::bigint AS device_id,
        device_name,
        LOWER(TRIM(device_name)) AS canonical_hostname,
        role_name,
        device_type,
        status,
        primary_ip,
        site_name,
        ingested_at::timestamptz AS ingested_at,
        MAX(snapshot_date::date) OVER () AS latest_snapshot_date
    FROM {{ source("raw", "netbox_devices") }}
)
SELECT
    *,
    (snapshot_date = latest_snapshot_date) AS is_latest
FROM src
