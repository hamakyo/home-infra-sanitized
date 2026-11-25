{{ config(severity = "warn") }}
-- Governance rule: every Workstation/Server in NetBox must be monitored in Zabbix and enrolled in Fleet.
SELECT
    canonical_hostname,
    netbox_role,
    reconciliation_status
FROM {{ ref("mart_asset_reconciliation") }}
WHERE reconciliation_status IN ('unmonitored_in_zabbix', 'unmanaged_in_fleet')
