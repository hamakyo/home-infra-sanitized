WITH users AS (
    SELECT * FROM {{ ref("stg_authentik_users") }} WHERE is_latest
),
mfa AS (
    SELECT
        user_pk::bigint AS user_pk,
        COUNT(*) FILTER (WHERE confirmed IS TRUE) AS confirmed_mfa_devices
    FROM {{ source("raw", "authentik_authenticators") }}
    WHERE snapshot_date::date = (SELECT MAX(snapshot_date) FROM users)
    GROUP BY user_pk
),
events_30d AS (
    SELECT
        username,
        COUNT(*) FILTER (WHERE action = 'login') AS login_success_30d,
        COUNT(*) FILTER (WHERE action = 'login_failed') AS login_failed_30d,
        MAX(NULLIF((created)::text, '')::timestamptz) AS last_event_at
    FROM {{ source("raw", "authentik_events") }}
    WHERE NULLIF((created)::text, '')::timestamptz >= NOW() - INTERVAL '30 days'
    GROUP BY username
)
SELECT
    u.snapshot_date,
    u.user_pk,
    u.username,
    u.name AS display_name,
    u.user_type,
    u.is_human_account,
    u.is_active,
    u.is_superuser,
    u.groups_csv,
    u.last_login,
    CASE
        WHEN u.last_login IS NOT NULL
        THEN ROUND(EXTRACT(EPOCH FROM (NOW() - u.last_login)) / 86400.0, 1)
        ELSE NULL
    END AS days_since_last_login,
    COALESCE(m.confirmed_mfa_devices, 0)::int AS confirmed_mfa_devices,
    (COALESCE(m.confirmed_mfa_devices, 0) > 0) AS mfa_enabled,
    COALESCE(e.login_success_30d, 0)::int AS login_success_30d,
    COALESCE(e.login_failed_30d, 0)::int AS login_failed_30d,
    CASE
        WHEN NOT u.is_active THEN 'inactive'
        WHEN NOT u.is_human_account THEN 'service_account'
        WHEN COALESCE(m.confirmed_mfa_devices, 0) = 0 THEN 'mfa_missing'
        ELSE 'ok'
    END AS posture_status
FROM users u
LEFT JOIN mfa m ON u.user_pk = m.user_pk
LEFT JOIN events_30d e ON u.username = e.username
ORDER BY u.is_human_account DESC, u.username
