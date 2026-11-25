{{ config(severity = "warn") }}
-- Governance rule: every host observed in Fleet or Zabbix must be registered in NetBox (SSoT).
SELECT
    canonical_hostname,
    in_fleet,
    in_zabbix
FROM {{ ref("mart_asset_reconciliation") }}
WHERE reconciliation_status = 'shadow_asset'
