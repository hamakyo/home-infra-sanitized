WITH nb AS (
    SELECT * FROM {{ ref("stg_netbox_devices") }} WHERE is_latest
),
fl AS (
    SELECT * FROM {{ ref("stg_fleet_hosts") }} WHERE is_latest
),
zb AS (
    SELECT * FROM {{ ref("stg_zabbix_hosts") }} WHERE is_latest AND NOT is_zabbix_internal
),
all_keys AS (
    SELECT canonical_hostname FROM nb
    UNION
    SELECT canonical_hostname FROM fl
    UNION
    SELECT canonical_hostname FROM zb
)
SELECT
    k.canonical_hostname,
    COALESCE(nb.snapshot_date, fl.snapshot_date, zb.snapshot_date) AS snapshot_date,
    (nb.device_id IS NOT NULL) AS in_netbox,
    (fl.host_id IS NOT NULL) AS in_fleet,
    (zb.hostid IS NOT NULL) AS in_zabbix,
    nb.role_name AS netbox_role,
    nb.status AS netbox_status,
    nb.primary_ip AS netbox_primary_ip,
    fl.hostname AS fleet_hostname,
    fl.platform AS fleet_platform,
    fl.os_version AS fleet_os_version,
    fl.status AS fleet_status,
    fl.disk_free_gb AS fleet_disk_free_gb,
    fl.disk_free_pct AS fleet_disk_free_pct,
    fl.failing_policies_count,
    fl.critical_vulnerabilities_count,
    zb.display_name AS zabbix_display_name,
    zb.interface_ip AS zabbix_ip,
    zb.active_available AS zabbix_active_available,
    CASE
        WHEN nb.device_id IS NULL THEN 'shadow_asset'
        WHEN nb.role_name IN ('Workstation', 'Server') AND fl.host_id IS NULL THEN 'unmanaged_in_fleet'
        WHEN nb.role_name IN ('Workstation', 'Server') AND zb.hostid IS NULL THEN 'unmonitored_in_zabbix'
        ELSE 'reconciled'
    END AS reconciliation_status
FROM all_keys k
LEFT JOIN nb ON k.canonical_hostname = nb.canonical_hostname
LEFT JOIN fl ON k.canonical_hostname = fl.canonical_hostname
LEFT JOIN zb ON k.canonical_hostname = zb.canonical_hostname
ORDER BY k.canonical_hostname
