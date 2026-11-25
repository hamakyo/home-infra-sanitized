{{ config(severity = "error") }}
-- Governance rule: at least one restic DB backup must have succeeded within the last 36 hours.
WITH latest_ok AS (
    SELECT MAX(started_at) AS last_ok_at
    FROM {{ ref("mart_backup_compliance") }}
    WHERE mode = 'backup'
      AND status = 'ok'
)
SELECT last_ok_at
FROM latest_ok
WHERE last_ok_at IS NULL
   OR last_ok_at < NOW() - INTERVAL '36 hours'
