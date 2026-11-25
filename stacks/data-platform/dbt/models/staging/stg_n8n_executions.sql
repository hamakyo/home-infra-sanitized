SELECT
    e.execution_id,
    e.workflow_id,
    COALESCE(a.workflow_label, w.workflow_name, e.workflow_id) AS workflow_label,
    COALESCE(w.workflow_name, e.workflow_id) AS workflow_name,
    COALESCE(a.category, 'general') AS category,
    COALESCE(w.active, FALSE) AS workflow_active,
    e.mode,
    e.status,
    COALESCE(e.finished, FALSE) AS finished,
    (e.status = 'success') AS is_success,
    NULLIF((e.started_at)::text, '')::timestamptz AS started_at,
    NULLIF((e.stopped_at)::text, '')::timestamptz AS stopped_at,
    (NULLIF((e.started_at)::text, '')::timestamptz AT TIME ZONE 'Asia/Tokyo')::date AS exec_date_jst,
    CASE
        WHEN NULLIF((e.started_at)::text, '') IS NOT NULL
         AND NULLIF((e.stopped_at)::text, '') IS NOT NULL
        THEN GREATEST(
            0.0,
            EXTRACT(EPOCH FROM (
                NULLIF((e.stopped_at)::text, '')::timestamptz - NULLIF((e.started_at)::text, '')::timestamptz
            ))
        )
        ELSE 0.0
    END AS duration_sec,
    COALESCE(a.estimated_manual_minutes, 5)::numeric AS estimated_manual_minutes,
    CASE
        WHEN e.status = 'success' THEN COALESCE(a.estimated_manual_minutes, 5)::numeric
        ELSE 0::numeric
    END AS saved_minutes
FROM raw.n8n_executions e
LEFT JOIN raw.n8n_workflows w ON e.workflow_id = w.workflow_id
LEFT JOIN {{ ref('workflow_roi_assumptions') }} a ON e.workflow_id = a.workflow_id
