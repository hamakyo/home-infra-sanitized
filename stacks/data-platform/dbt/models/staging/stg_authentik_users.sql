WITH src AS (
    SELECT
        snapshot_date::date AS snapshot_date,
        user_pk::bigint AS user_pk,
        username,
        name,
        user_type,
        is_active::boolean AS is_active,
        is_superuser::boolean AS is_superuser,
        NULLIF((last_login)::text, '')::timestamptz AS last_login,
        NULLIF((date_joined)::text, '')::timestamptz AS date_joined,
        COALESCE(groups_csv, '') AS groups_csv,
        (user_type = 'internal' AND username <> 'AnonymousUser') AS is_human_account,
        ingested_at::timestamptz AS ingested_at,
        MAX(snapshot_date::date) OVER () AS latest_snapshot_date
    FROM {{ source("raw", "authentik_users") }}
)
SELECT
    *,
    (snapshot_date = latest_snapshot_date) AS is_latest
FROM src
