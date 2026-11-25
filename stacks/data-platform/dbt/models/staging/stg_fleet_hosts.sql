WITH aliases AS (
    SELECT
        LOWER(TRIM(raw_hostname)) AS raw_hostname,
        LOWER(TRIM(canonical_hostname)) AS canonical_hostname
    FROM {{ ref("asset_hostname_aliases") }}
),
src AS (
    SELECT
        f.snapshot_date::date AS snapshot_date,
        f.host_id::bigint AS host_id,
        f.hostname,
        COALESCE(a.canonical_hostname, LOWER(TRIM(f.hostname))) AS canonical_hostname,
        f.display_name,
        f.platform,
        f.os_version,
        f.osquery_version,
        f.status,
        NULLIF((f.seen_time)::text, '')::timestamptz AS seen_time,
        f.disk_free_gb::double precision AS disk_free_gb,
        f.disk_free_pct::double precision AS disk_free_pct,
        f.failing_policies_count::int AS failing_policies_count,
        f.critical_vulnerabilities_count::int AS critical_vulnerabilities_count,
        f.total_issues_count::int AS total_issues_count,
        f.ingested_at::timestamptz AS ingested_at,
        MAX(f.snapshot_date::date) OVER () AS latest_snapshot_date
    FROM {{ source("raw", "fleet_hosts") }} f
    LEFT JOIN aliases a
        ON LOWER(TRIM(f.hostname)) = a.raw_hostname
)
SELECT
    *,
    (snapshot_date = latest_snapshot_date) AS is_latest
FROM src
