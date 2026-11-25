WITH workflows AS (
    SELECT
        w.workflow_id,
        w.workflow_name,
        COALESCE(a.workflow_label, w.workflow_name) AS workflow_label,
        COALESCE(a.category, 'general') AS category,
        COALESCE(w.active, FALSE) AS is_active,
        COALESCE(a.estimated_manual_minutes, 5)::numeric AS estimated_manual_minutes_per_run
    FROM raw.n8n_workflows w
    LEFT JOIN {{ ref('workflow_roi_assumptions') }} a ON w.workflow_id = a.workflow_id
),
agg AS (
    SELECT
        workflow_id,
        COUNT(*) AS executions_total,
        COUNT(*) FILTER (WHERE is_success) AS executions_success,
        COUNT(*) FILTER (WHERE NOT is_success) AS executions_failed,
        ROUND(AVG(duration_sec)::numeric, 2) AS avg_duration_sec,
        ROUND(SUM(saved_minutes)::numeric, 1) AS total_minutes_saved,
        MAX(started_at) AS last_executed_at
    FROM {{ ref('stg_n8n_executions') }}
    GROUP BY workflow_id
)
SELECT
    w.workflow_id,
    w.workflow_label,
    w.workflow_name,
    w.category,
    w.is_active,
    COALESCE(a.executions_total, 0)::bigint AS executions_total,
    COALESCE(a.executions_success, 0)::bigint AS executions_success,
    COALESCE(a.executions_failed, 0)::bigint AS executions_failed,
    CASE
        WHEN COALESCE(a.executions_total, 0) = 0 THEN 0.0
        ELSE ROUND((a.executions_success::numeric / a.executions_total::numeric) * 100.0, 1)
    END AS success_rate_pct,
    COALESCE(a.avg_duration_sec, 0.0) AS avg_duration_sec,
    w.estimated_manual_minutes_per_run,
    COALESCE(a.total_minutes_saved, 0.0) AS total_minutes_saved,
    ROUND((COALESCE(a.total_minutes_saved, 0.0) / 60.0)::numeric, 2) AS total_hours_saved,
    a.last_executed_at
FROM workflows w
LEFT JOIN agg a ON w.workflow_id = a.workflow_id
ORDER BY total_minutes_saved DESC, executions_total DESC, w.workflow_id
