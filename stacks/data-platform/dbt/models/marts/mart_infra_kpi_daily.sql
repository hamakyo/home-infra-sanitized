WITH snap AS (
    SELECT DISTINCT snapshot_date FROM {{ ref("stg_netbox_devices") }}
),
recon AS (
    SELECT
        snapshot_date,
        COUNT(*) AS total_assets,
        COUNT(*) FILTER (WHERE in_netbox) AS netbox_devices,
        COUNT(*) FILTER (WHERE in_fleet) AS fleet_hosts,
        COUNT(*) FILTER (WHERE in_zabbix) AS zabbix_hosts,
        COUNT(*) FILTER (WHERE reconciliation_status <> 'reconciled') AS reconciliation_mismatches
    FROM {{ ref("mart_asset_reconciliation") }}
    GROUP BY snapshot_date
),
ident AS (
    SELECT
        snapshot_date,
        COUNT(*) FILTER (WHERE is_human_account AND is_active) AS active_human_users,
        COUNT(*) FILTER (WHERE is_human_account AND is_active AND mfa_enabled) AS mfa_enabled_human_users,
        SUM(login_failed_30d) AS failed_logins_30d
    FROM {{ ref("mart_identity_posture") }}
    GROUP BY snapshot_date
),
vulns AS (
    SELECT
        snapshot_date,
        COUNT(*) AS tracked_cves,
        COUNT(*) FILTER (WHERE hosts_count >= 2) AS multi_host_cves,
        COUNT(*) FILTER (WHERE priority_tier IN ('kev', 'critical')) AS critical_or_kev_cves
    FROM {{ ref("mart_vulnerability_posture") }}
    GROUP BY snapshot_date
),
zprobs AS (
    SELECT
        snapshot_date::date AS snapshot_date,
        COUNT(*) AS active_zabbix_problems
    FROM {{ source("raw", "zabbix_problems") }}
    GROUP BY snapshot_date::date
),
bkp AS (
    SELECT
        MIN(hours_since_run) FILTER (WHERE mode = 'backup' AND status = 'ok') AS hours_since_last_backup_ok,
        MAX(total_dump_mib) FILTER (WHERE is_latest_ok_backup) AS latest_backup_dump_mib
    FROM {{ ref("mart_backup_compliance") }}
)
SELECT
    s.snapshot_date,
    COALESCE(r.total_assets, 0)::int AS total_assets,
    COALESCE(r.netbox_devices, 0)::int AS netbox_devices,
    COALESCE(r.fleet_hosts, 0)::int AS fleet_hosts,
    COALESCE(r.zabbix_hosts, 0)::int AS zabbix_hosts,
    COALESCE(r.reconciliation_mismatches, 0)::int AS reconciliation_mismatches,
    COALESCE(i.active_human_users, 0)::int AS active_human_users,
    COALESCE(i.mfa_enabled_human_users, 0)::int AS mfa_enabled_human_users,
    CASE
        WHEN COALESCE(i.active_human_users, 0) > 0
        THEN ROUND((i.mfa_enabled_human_users::numeric / i.active_human_users::numeric) * 100.0, 1)
        ELSE 100.0
    END AS mfa_coverage_pct,
    COALESCE(i.failed_logins_30d, 0)::int AS failed_logins_30d,
    COALESCE(v.tracked_cves, 0)::int AS tracked_cves,
    COALESCE(v.multi_host_cves, 0)::int AS multi_host_cves,
    COALESCE(v.critical_or_kev_cves, 0)::int AS critical_or_kev_cves,
    COALESCE(z.active_zabbix_problems, 0)::int AS active_zabbix_problems,
    b.hours_since_last_backup_ok,
    b.latest_backup_dump_mib
FROM snap s
LEFT JOIN recon r ON s.snapshot_date = r.snapshot_date
LEFT JOIN ident i ON s.snapshot_date = i.snapshot_date
LEFT JOIN vulns v ON s.snapshot_date = v.snapshot_date
LEFT JOIN zprobs z ON s.snapshot_date = z.snapshot_date
CROSS JOIN bkp b
ORDER BY s.snapshot_date DESC
