"""Read-only Governed RAG & Infra Governance MCP Server (Streamable HTTP JSON-RPC 2.0).

Security boundaries:
- Connects to PostgreSQL strictly as `rag_reader` (SELECT on `rag.*` and `mart.*` only)
- Mandatory Bearer token authentication (`RAG_MCP_TOKEN`) on `/mcp`
- Runs as non-root UID 10001, read-only rootfs, cap_drop ALL
"""

from __future__ import annotations

import hmac
import json
import os
from typing import Any

import psycopg2
import psycopg2.extras
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from search_lib import hybrid_search

PROTOCOL_VERSION = "2025-03-26"

TOOLS = [
    {
        "name": "search_home_infra_knowledge",
        "description": (
            "Search the governed Infra & home-infra knowledge base (architecture docs, ADRs, audits, "
            "and live Step 1 Infra DWH summaries) using hybrid pgvector + keyword RRF search. "
            "All returned passages are pre-redacted for secrets and PII."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language search query in Japanese or English",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Number of passages to return (1-10, default 5)",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 5,
                },
                "source_type": {
                    "type": "string",
                    "description": (
                        "Optional filter: home_infra_doc, home_infra_adr, home_infra_audit, "
                        "home_infra_overview, or infra_mart_snapshot"
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_infra_governance_snapshot",
        "description": (
            "Return the latest read-only Infra governance KPIs from PostgreSQL mart.* "
            "(asset 3-way reconciliation, Authentik MFA posture, backup compliance, and latest RAG eval metrics)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


def db_connect():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "infra-dwh-postgres"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "infra_dwh"),
        user=os.environ.get("PGUSER", "rag_reader"),
        password=os.environ["PGPASSWORD"],
        options="-c default_transaction_read_only=on -c statement_timeout=5000",
    )


def check_bearer(request: Request) -> bool:
    expected = os.environ.get("RAG_MCP_TOKEN", "").strip()
    if not expected:
        return False
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return False
    token = auth[7:].strip()
    return hmac.compare_digest(token, expected)


def tool_search(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    top_k = max(1, min(int(args.get("top_k") or 5), 10))
    source_type = (args.get("source_type") or "").strip() or None
    with db_connect() as conn:
        hits = hybrid_search(conn, query, top_k=top_k, source_type=source_type)
    return {"query": query, "count": len(hits), "results": hits}


def tool_snapshot() -> dict[str, Any]:
    with db_connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM mart.mart_infra_kpi_daily ORDER BY snapshot_date DESC LIMIT 1;")
            kpi = dict(cur.fetchone() or {})
            cur.execute(
                "SELECT canonical_hostname, reconciliation_status, in_netbox, in_fleet, in_zabbix, netbox_role FROM mart.mart_asset_reconciliation ORDER BY canonical_hostname;"
            )
            assets = [dict(r) for r in cur.fetchall()]
            cur.execute(
                "SELECT username, user_type, posture_status, confirmed_mfa_devices, mfa_enabled FROM mart.mart_identity_posture ORDER BY is_human_account DESC, username;"
            )
            identities = [dict(r) for r in cur.fetchall()]
            cur.execute(
                "SELECT run_id, ran_at, embedding_model, query_count, hit_at_1, hit_at_3, hit_at_5, mrr_at_5, avg_latency_ms, leakage_violations, status FROM rag.eval_runs ORDER BY ran_at DESC LIMIT 1;"
            )
            rag_eval = dict(cur.fetchone() or {})
    return {
        "kpi": kpi,
        "assets": assets,
        "identities": identities,
        "latest_rag_eval": rag_eval,
    }


async def health(request: Request) -> JSONResponse:
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM rag.chunks;")
                chunks = cur.fetchone()[0]
        return JSONResponse({"status": "ok", "indexed_chunks": chunks})
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=503)


async def mcp_endpoint(request: Request) -> Response:
    if not check_bearer(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    if request.method == "GET":
        return JSONResponse({"status": "ok", "protocol": "mcp-streamable-http"})

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            status_code=400,
        )

    req_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params") or {}

    # Notifications have no id and expect 202 Accepted
    if method.startswith("notifications/") or (req_id is None and method != "initialize"):
        return Response(status_code=202)

    if method == "initialize":
        client_ver = params.get("protocolVersion") or PROTOCOL_VERSION
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": client_ver,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "infra-rag-mcp", "version": "1.0.0"},
                },
            }
        )

    if method == "ping":
        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": {}})

    if method == "tools/list":
        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if name == "search_home_infra_knowledge":
                data = tool_search(args)
            elif name == "get_infra_governance_snapshot":
                data = tool_snapshot()
            else:
                return JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32601, "message": f"Unknown tool: {name}"},
                    }
                )
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, default=str)}],
                        "isError": False,
                    },
                }
            )
        except Exception as exc:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": f"Tool error: {exc}"}],
                        "isError": True,
                    },
                }
            )

    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    )


app = Starlette(
    routes=[
        Route("/health", health, methods=["GET"]),
        Route("/mcp", mcp_endpoint, methods=["GET", "POST"]),
    ]
)
