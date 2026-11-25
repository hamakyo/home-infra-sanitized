SELECT
    exec_date_jst,
    COUNT(*)::bigint AS executions_total,
    COUNT(*) FILTER (WHERE is_success)::bigint AS executions_success,
    COUNT(*) FILTER (WHERE NOT is_success)::bigint AS executions_failed,
    ROUND(
        CASE WHEN COUNT(*) = 0 THEN 0.0
             ELSE (COUNT(*) FILTER (WHERE is_success)::numeric / COUNT(*)::numeric) * 100.0
        END, 1
    ) AS success_rate_pct,
    ROUND(AVG(duration_sec)::numeric, 2) AS avg_duration_sec,
    ROUND(SUM(saved_minutes)::numeric, 1) AS total_minutes_saved
FROM {{ ref('stg_n8n_executions') }}
WHERE exec_date_jst IS NOT NULL
GROUP BY exec_date_jst
ORDER BY exec_date_jst DESC
