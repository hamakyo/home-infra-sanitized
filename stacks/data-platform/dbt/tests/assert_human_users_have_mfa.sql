{{ config(severity = "warn") }}
-- Governance rule: every active human Authentik account should have at least one confirmed MFA device.
SELECT
    username,
    display_name,
    is_superuser,
    confirmed_mfa_devices
FROM {{ ref("mart_identity_posture") }}
WHERE is_human_account
  AND is_active
  AND NOT mfa_enabled
