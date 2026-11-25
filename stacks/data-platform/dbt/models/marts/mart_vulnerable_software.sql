WITH latest_date AS (
    SELECT MAX(snapshot_date::date) AS d
    FROM {{ source("raw", "fleet_vulnerable_software") }}
)
SELECT
    s.snapshot_date::date AS snapshot_date,
    s.software_id::bigint AS software_id,
    s.name AS software_name,
    s.version AS software_version,
    s.source AS package_source,
    s.hosts_count::int AS hosts_count,
    s.vuln_count::int AS vuln_count,
    s.top_cve
FROM {{ source("raw", "fleet_vulnerable_software") }} s
INNER JOIN latest_date l ON s.snapshot_date::date = l.d
ORDER BY s.hosts_count DESC, s.vuln_count DESC, s.name
