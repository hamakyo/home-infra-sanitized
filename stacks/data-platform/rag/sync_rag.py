#!/usr/bin/env python3
"""Governed incremental RAG indexer into PostgreSQL pgvector (`rag.*`).

Sources (explicit allowlist only):
  1. example-user/home-infra-sanitized README.md, docs/*.md, docs/decisions/*.md
  2. Live Step 1 Infra DWH summary cards synthesized from `mart.*`
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent))
from redact import redact_text, scan_leakage
from search_lib import DEFAULT_MODEL, embed_texts, vec_literal

REPO_DIR = Path(os.environ.get("HOME_INFRA_REPO_DIR", "/repo-home-infra"))
MAX_CHUNK_CHARS = 500
EMBED_BATCH_SIZE = 16


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_title_and_status(md_text: str, fallback_title: str) -> tuple[str, str]:
    title = fallback_title
    status = "unknown"
    for line in md_text.splitlines()[:15]:
        s = line.strip()
        if s.startswith("# ") and title == fallback_title:
            title = s[2:].strip()
        elif s.startswith("状態：") or s.startswith("状態:"):
            status = s.split("：", 1)[-1].split(":", 1)[-1].strip()
    return title, status


def split_markdown_sections(md_text: str, title: str) -> list[tuple[str, str]]:
    """Split markdown into section-aware chunks preserving heading breadcrumbs."""
    lines = md_text.splitlines()
    sections: list[tuple[str, list[str]]] = []
    cur_h2 = "概要"
    cur_h3 = ""
    buf: list[str] = []

    def flush():
        nonlocal buf
        body = "\n".join(buf).strip()
        if body:
            heading = f"{cur_h2} / {cur_h3}" if cur_h3 else cur_h2
            sections.append((heading, [body]))
        buf = []

    for line in lines:
        if line.startswith("## "):
            flush()
            cur_h2 = line[3:].strip()
            cur_h3 = ""
        elif line.startswith("### "):
            flush()
            cur_h3 = line[4:].strip()
        else:
            buf.append(line)
    flush()

    chunks: list[tuple[str, str]] = []
    for heading, bodies in sections:
        text = bodies[0]
        # Clean markdown image base64 or overly verbose alt links if any
        text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text).strip()
        if not text:
            continue
        header = f"[{title} > {heading}]\n"
        budget = max(200, MAX_CHUNK_CHARS - min(len(header), 120))
        if len(text) <= budget:
            chunks.append((heading, f"{header}{text}"))
        else:
            # Split cleanly by paragraphs, then lines, then character slices to stay well below 512 tokens
            units: list[str] = []
            for p in re.split(r"\n{2,}", text):
                p = p.strip()
                if not p:
                    continue
                if len(p) <= budget:
                    units.append(p)
                else:
                    line_acc = ""
                    for ln in p.splitlines():
                        if len(ln) > budget:
                            if line_acc:
                                units.append(line_acc)
                                line_acc = ""
                            for i in range(0, len(ln), budget):
                                units.append(ln[i : i + budget])
                        elif line_acc and len(line_acc) + len(ln) + 1 > budget:
                            units.append(line_acc)
                            line_acc = ln
                        else:
                            line_acc = f"{line_acc}\n{ln}" if line_acc else ln
                    if line_acc:
                        units.append(line_acc)

            acc = ""
            part = 1
            for u in units:
                if acc and len(acc) + len(u) + 2 > budget:
                    chunks.append((f"{heading} ({part})", f"{header}{acc}"))
                    acc = u
                    part += 1
                else:
                    acc = f"{acc}\n\n{u}" if acc else u
            if acc:
                lbl = f"{heading} ({part})" if part > 1 else heading
                chunks.append((lbl, f"{header}{acc}"))
    return chunks


def collect_repo_documents() -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    allow_patterns = [
        ("home_infra_overview", [REPO_DIR / "README.md"]),
        ("home_infra_doc", sorted((REPO_DIR / "docs").glob("*.md"))),
        ("home_infra_adr", sorted((REPO_DIR / "docs/decisions").glob("*.md"))),
        ("home_infra_audit", sorted((REPO_DIR / "docs/audits").glob("*.md"))),
    ]
    for source_type, paths in allow_patterns:
        for p in paths:
            if not p.is_file():
                continue
            rel = str(p.relative_to(REPO_DIR))
            raw_text = p.read_text(encoding="utf-8")
            title, status = extract_title_and_status(raw_text, p.stem)
            docs.append(
                {
                    "doc_id": f"home-infra/{rel}",
                    "source_type": source_type,
                    "source_path": f"example-user/home-infra-sanitized/{rel}",
                    "title": title,
                    "doc_status": status,
                    "raw_text": raw_text,
                }
            )
    return docs


def collect_mart_documents(conn: Any) -> list[dict[str, Any]]:
    """Synthesize governed live operational summaries from Step 1 DWH marts."""
    docs: list[dict[str, Any]] = []
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM mart.mart_infra_kpi_daily ORDER BY snapshot_date DESC LIMIT 1;")
        kpi = cur.fetchone()

        cur.execute(
            "SELECT canonical_hostname, reconciliation_status, in_netbox, in_fleet, in_zabbix, netbox_role, netbox_primary_ip, fleet_platform, fleet_status, ROUND(fleet_disk_free_gb::numeric, 1) AS disk_free_gb FROM mart.mart_asset_reconciliation ORDER BY canonical_hostname;"
        )
        assets = cur.fetchall()

        cur.execute(
            "SELECT username, user_type, posture_status, confirmed_mfa_devices, mfa_enabled, is_superuser, login_success_30d, login_failed_30d FROM mart.mart_identity_posture ORDER BY is_human_account DESC, username;"
        )
        identities = cur.fetchall()

        cur.execute(
            "SELECT started_at, mode, status, duration_sec, total_dump_mib, db_count, snapshot_id FROM mart.mart_backup_compliance ORDER BY started_at DESC LIMIT 5;"
        )
        backups = cur.fetchall()

        cur.execute(
            "SELECT software_name, software_version, package_source, hosts_count, vuln_count, top_cve FROM mart.mart_vulnerable_software ORDER BY hosts_count DESC, vuln_count DESC LIMIT 10;"
        )
        sw = cur.fetchall()

    snap = str(kpi["snapshot_date"]) if kpi else "unknown"

    # 1. Asset Reconciliation Mart Card
    asset_lines = [
        f"# ライブ運用サマリ：資産 3 点突合（NetBox × Fleet × Zabbix）",
        f"状態： ライブ集計（{snap} 時点）",
        "",
        "## 全体サマリ",
        f"- 登録資産数（NetBox）：{kpi['netbox_devices']} 台 / Fleet 管理：{kpi['fleet_hosts']} 台 / Zabbix 監視：{kpi['zabbix_hosts']} 台 / 不整合件数：{kpi['reconciliation_mismatches']} 件" if kpi else "- KPI 未取得",
        "",
        "## 資産別ステータス",
    ]
    for a in assets:
        asset_lines.append(
            f"- {a['canonical_hostname']}（役割: {a['netbox_role']}, IP: {a['netbox_primary_ip']}）: 突合={a['reconciliation_status']}, NetBox={a['in_netbox']}, Fleet={a['in_fleet']} ({a['fleet_platform']}/{a['fleet_status']}, 空き {a['disk_free_gb']} GB), Zabbix={a['in_zabbix']}"
        )
    docs.append(
        {
            "doc_id": "infra-mart/asset-reconciliation",
            "source_type": "infra_mart_snapshot",
            "source_path": "infra_dwh.mart.mart_asset_reconciliation",
            "title": "ライブ運用サマリ：資産 3 点突合（NetBox × Fleet × Zabbix）",
            "doc_status": f"live ({snap})",
            "raw_text": "\n".join(asset_lines),
        }
    )

    # 2. Identity & MFA Posture Mart Card
    id_lines = [
        "# ライブ運用サマリ：ID・MFA 統制状況（Authentik）",
        f"状態： ライブ集計（{snap} 時点）",
        "",
        "## MFA カバレッジ",
        f"- 有効な人間アカウント：{kpi['active_human_users']} 名 / MFA 設定済み：{kpi['mfa_enabled_human_users']} 名 / MFA 適用率：{kpi['mfa_coverage_pct']}% / 過去30日ログイン失敗：{kpi['failed_logins_30d']} 件" if kpi else "- KPI 未取得",
        "",
        "## アカウント一覧",
    ]
    for u in identities:
        id_lines.append(
            f"- {u['username']}（種別: {u['user_type']}）: ステータス={u['posture_status']}, MFA有効={u['mfa_enabled']}（登録デバイス数={u['confirmed_mfa_devices']}）, 管理者={u['is_superuser']}, 30日ログイン成功={u['login_success_30d']} / 失敗={u['login_failed_30d']}"
        )
    docs.append(
        {
            "doc_id": "infra-mart/identity-posture",
            "source_type": "infra_mart_snapshot",
            "source_path": "infra_dwh.mart.mart_identity_posture",
            "title": "ライブ運用サマリ：ID・MFA 統制状況（Authentik）",
            "doc_status": f"live ({snap})",
            "raw_text": "\n".join(id_lines),
        }
    )

    # 3. Backup & Vulnerability Operational Card
    ops_lines = [
        "# ライブ運用サマリ：バックアップ鮮度・脆弱性状況（restic / Fleet）",
        f"状態： ライブ集計（{snap} 時点）",
        "",
        "## バックアップ実績（restic → Backblaze B2）",
        f"- 最終成功からの経過時間：{kpi['hours_since_last_backup_ok']} 時間 / 最新ダンプ合計：{kpi['latest_backup_dump_mib']} MiB / Zabbix アクティブ障害：{kpi['active_zabbix_problems']} 件" if kpi else "- KPI 未取得",
    ]
    for b in backups:
        ops_lines.append(
            f"- {b['started_at']} mode={b['mode']} status={b['status']} duration={b['duration_sec']}s dump={b['total_dump_mib']}MiB dbs={b['db_count']} snapshot={b['snapshot_id']}"
        )
    ops_lines.extend(
        [
            "",
            "## 脆弱性サマリ（Fleet CVE）",
            f"- 追跡 CVE 上位取得数：{kpi['tracked_cves']} 件 / 複数端末該当 CVE：{kpi['multi_host_cves']} 件 / 重大・KEV：{kpi['critical_or_kev_cves']} 件" if kpi else "",
            "### 影響の大きい脆弱ソフトウェア上位",
        ]
    )
    for s in sw:
        ops_lines.append(
            f"- {s['software_name']} {s['software_version']} ({s['package_source']}): 該当端末={s['hosts_count']}, CVE数={s['vuln_count']}, 代表CVE={s['top_cve']}"
        )
    docs.append(
        {
            "doc_id": "infra-mart/backup-and-vulnerabilities",
            "source_type": "infra_mart_snapshot",
            "source_path": "infra_dwh.mart.mart_backup_compliance",
            "title": "ライブ運用サマリ：バックアップ鮮度・脆弱性状況（restic / Fleet）",
            "doc_status": f"live ({snap})",
            "raw_text": "\n".join(ops_lines),
        }
    )
    return docs


def main() -> None:
    dsn = (
        f"postgresql://{os.environ['PGUSER']}:{os.environ['PGPASSWORD']}"
        f"@{os.environ.get('PGHOST', 'infra-dwh-postgres')}:{os.environ.get('PGPORT', '5432')}"
        f"/{os.environ.get('PGDATABASE', 'infra_dwh')}"
    )
    with psycopg2.connect(dsn) as conn:
        docs = collect_repo_documents() + collect_mart_documents(conn)

        with conn.cursor() as cur:
            cur.execute("SELECT chunk_id, content_sha256 FROM rag.chunks;")
            existing_chunks = {r[0]: r[1] for r in cur.fetchall()}

        all_doc_ids: set[str] = set()
        all_chunk_ids: set[str] = set()
        chunks_to_embed: list[dict[str, Any]] = []
        skipped_cached = 0
        total_redactions = 0

        for doc in docs:
            doc_id = doc["doc_id"]
            all_doc_ids.add(doc_id)
            redacted_full, doc_hits = redact_text(doc["raw_text"])
            if scan_leakage(redacted_full):
                raise RuntimeError(f"Post-redaction leakage detected in {doc_id}")
            doc_sha = sha256_hex(redacted_full)
            raw_chunks = split_markdown_sections(redacted_full, doc["title"])
            total_redactions += len(doc_hits)

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO rag.documents
                        (doc_id, source_type, source_path, title, doc_status, classification, content_sha256, chunk_count, redaction_count, updated_at)
                    VALUES (%s, %s, %s, %s, %s, 'internal', %s, %s, %s, NOW())
                    ON CONFLICT (doc_id) DO UPDATE SET
                        source_type = EXCLUDED.source_type,
                        source_path = EXCLUDED.source_path,
                        title = EXCLUDED.title,
                        doc_status = EXCLUDED.doc_status,
                        content_sha256 = EXCLUDED.content_sha256,
                        chunk_count = EXCLUDED.chunk_count,
                        redaction_count = EXCLUDED.redaction_count,
                        updated_at = NOW()
                    """,
                    (
                        doc_id,
                        doc["source_type"],
                        doc["source_path"],
                        doc["title"],
                        doc["doc_status"],
                        doc_sha,
                        len(raw_chunks),
                        len(doc_hits),
                    ),
                )

            for idx, (heading, content) in enumerate(raw_chunks):
                chunk_id = f"{doc_id}::chunk-{idx:03d}"
                all_chunk_ids.add(chunk_id)
                c_sha = sha256_hex(content)
                if existing_chunks.get(chunk_id) == c_sha:
                    skipped_cached += 1
                    continue
                _, c_hits = redact_text(content)
                chunks_to_embed.append(
                    {
                        "chunk_id": chunk_id,
                        "doc_id": doc_id,
                        "chunk_index": idx,
                        "section_heading": heading,
                        "content": content,
                        "content_sha256": c_sha,
                        "token_estimate": max(1, len(content) // 2),
                        "redaction_hits": c_hits,
                    }
                )

        # Batch-embed ONLY new or changed chunks
        api_calls = 0
        for i in range(0, len(chunks_to_embed), EMBED_BATCH_SIZE):
            batch = chunks_to_embed[i : i + EMBED_BATCH_SIZE]
            vecs = embed_texts([b["content"] for b in batch], prefix="passage: ")
            api_calls += 1
            with conn.cursor() as cur:
                for b, vec in zip(batch, vecs):
                    cur.execute(
                        """
                        INSERT INTO rag.chunks
                            (chunk_id, doc_id, chunk_index, section_heading, content, content_sha256, token_estimate, embedding, redaction_hits, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector, %s::jsonb, NOW())
                        ON CONFLICT (chunk_id) DO UPDATE SET
                            chunk_index = EXCLUDED.chunk_index,
                            section_heading = EXCLUDED.section_heading,
                            content = EXCLUDED.content,
                            content_sha256 = EXCLUDED.content_sha256,
                            token_estimate = EXCLUDED.token_estimate,
                            embedding = EXCLUDED.embedding,
                            redaction_hits = EXCLUDED.redaction_hits,
                            updated_at = NOW()
                        """,
                        (
                            b["chunk_id"],
                            b["doc_id"],
                            b["chunk_index"],
                            b["section_heading"],
                            b["content"],
                            b["content_sha256"],
                            b["token_estimate"],
                            vec_literal(vec),
                            json.dumps(b["redaction_hits"]),
                        ),
                    )

        # Prune deleted chunks & documents
        with conn.cursor() as cur:
            if all_chunk_ids:
                cur.execute("DELETE FROM rag.chunks WHERE NOT (chunk_id = ANY(%s));", (list(all_chunk_ids),))
            if all_doc_ids:
                cur.execute("DELETE FROM rag.documents WHERE NOT (doc_id = ANY(%s));", (list(all_doc_ids),))

            cur.execute("SELECT COUNT(*) FROM rag.documents;")
            doc_cnt = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM rag.chunks;")
            chunk_cnt = cur.fetchone()[0]
        conn.commit()

    print(
        "RAG_SYNC_SUMMARY_JSON="
        + json.dumps(
            {
                "embedding_model": os.environ.get("RAG_EMBEDDING_MODEL", DEFAULT_MODEL),
                "documents": doc_cnt,
                "chunks": chunk_cnt,
                "embedded_new": len(chunks_to_embed),
                "skipped_cached": skipped_cached,
                "embedding_api_calls": api_calls,
                "redactions_applied": total_redactions,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
