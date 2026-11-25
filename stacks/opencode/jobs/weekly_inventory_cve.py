#!/usr/bin/env python3
"""Weekly read-only Fleet + NetBox inventory and CVE digest via isolated OpenCode.

Runs on the host (never inside the opencode container), fetches read-only data
from Fleet observer API and NetBox view-only API, summarizes with the isolated
OpenCode `batch` agent, saves an audit log, and posts to Discord.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

from common import DEFAULT_ENV_FILE, http_get_json, run_guarded_job


def collect_fleet(url: str, token: str) -> dict[str, Any]:
    if not url or not token:
        return {"status": "not_configured"}
    base = url.rstrip("/")
    try:
        hosts_raw = http_get_json(f"{base}/hosts", token)
        hosts = []
        for h in hosts_raw.get("hosts") or []:
            issues = h.get("issues") or {}
            hosts.append(
                {
                    "hostname": h.get("hostname") or h.get("display_name"),
                    "platform": h.get("platform"),
                    "os_version": h.get("os_version"),
                    "status": h.get("status"),
                    "seen_time": h.get("seen_time"),
                    "osquery_version": h.get("osquery_version"),
                    "disk_free_gb": h.get("gigs_disk_space_available"),
                    "disk_free_pct": h.get("percent_disk_space_available"),
                    "failing_policies": issues.get("failing_policies_count", 0),
                    "critical_vulns": issues.get("critical_vulnerabilities_count", 0),
                }
            )

        vulns_top_hosts = http_get_json(
            f"{base}/vulnerabilities?per_page=15&order_key=hosts_count&order_direction=desc",
            token,
        )
        vulns_recent = http_get_json(
            f"{base}/vulnerabilities?per_page=10&order_key=created_at&order_direction=desc",
            token,
        )
        sw_raw = http_get_json(
            f"{base}/software?vulnerable=true&per_page=20&order_key=hosts_count&order_direction=desc",
            token,
        )

        top_software = []
        for s in sw_raw.get("software") or []:
            vulns = s.get("vulnerabilities") or []
            top_software.append(
                {
                    "name": s.get("name"),
                    "version": s.get("version"),
                    "source": s.get("source"),
                    "hosts_count": s.get("hosts_count"),
                    "cve_count": len(vulns),
                    "sample_cves": [v.get("cve") for v in vulns[:5] if isinstance(v, dict)],
                }
            )

        return {
            "status": "ok",
            "hosts_count": len(hosts),
            "hosts": hosts,
            "total_cve_count": vulns_top_hosts.get("count"),
            "counts_updated_at": vulns_top_hosts.get("counts_updated_at"),
            "top_cves_by_hosts": [
                {"cve": v.get("cve"), "hosts_count": v.get("hosts_count")}
                for v in (vulns_top_hosts.get("vulnerabilities") or [])[:15]
            ],
            "newest_cves": [
                {
                    "cve": v.get("cve"),
                    "hosts_count": v.get("hosts_count"),
                    "created_at": v.get("created_at"),
                }
                for v in (vulns_recent.get("vulnerabilities") or [])[:10]
            ],
            "top_vulnerable_software": top_software,
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": str(exc)[:240]}


def collect_netbox(url: str, token: str) -> dict[str, Any]:
    if not url or not token:
        return {"status": "not_configured"}
    base = url.rstrip("/")
    try:
        dev_raw = http_get_json(f"{base}/dcim/devices/?limit=100", token)
        ip_raw = http_get_json(f"{base}/ipam/ip-addresses/?limit=100", token)
        pfx_raw = http_get_json(f"{base}/ipam/prefixes/?limit=50", token)

        devices = []
        for d in dev_raw.get("results") or []:
            role = (d.get("role") or {}).get("name") or (d.get("device_role") or {}).get("name")
            status = (d.get("status") or {}).get("value")
            primary_ip = (d.get("primary_ip") or {}).get("address")
            devices.append(
                {
                    "name": d.get("name"),
                    "role": role,
                    "status": status,
                    "primary_ip": primary_ip,
                }
            )

        return {
            "status": "ok",
            "devices_count": dev_raw.get("count", len(devices)),
            "devices": devices,
            "ip_addresses_count": ip_raw.get("count", 0),
            "prefixes": [p.get("prefix") for p in (pfx_raw.get("results") or [])],
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": str(exc)[:240]}


def collect_snapshot(cfg: dict[str, str], now: dt.datetime) -> dict[str, Any]:
    return {
        "timestamp_jst": now.isoformat(timespec="seconds"),
        "fleet": collect_fleet(cfg.get("FLEET_API_URL", ""), cfg.get("FLEET_API_TOKEN", "")),
        "netbox": collect_netbox(cfg.get("NETBOX_API_URL", ""), cfg.get("NETBOX_API_TOKEN", "")),
    }


def build_prompt(snapshot: dict[str, Any]) -> str:
    snapshot_json = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    return (
        "あなたは自宅インフラ（Fleet Free + NetBox）の週次棚卸し＆CVE 要約担当です。"
        "以下の読み取り専用スナップショット JSON だけを根拠に、日本語で簡潔な週次レポートを作成してください。\n"
        "制約:\n"
        "- JSON にない数値・CVE・事実を推測や補完しないこと\n"
        "- ツール実行やファイル読み書きは一切行わず、与えられた JSON だけで回答すること\n"
        "- Discord 投稿向けに全体 1200 文字以内の見やすい Markdown にすること\n"
        "- クライアント PC（macOS ノート PC / workstation-01）の `offline` はスリープ・電源断による想定内のため重大異常扱いしないこと\n"
        "- 出力構成:\n"
        "  1. 先頭行に総合判定（`🟢 正常` / `🟡 要確認` / `🔴 要対応`）と 1 行要約\n"
        "  2. **端末稼働（Fleet）**：3 台の状態・OS バージョン・空きディスク注意点\n"
        "  3. **脆弱性・CVE 動向**：総 CVE 数・複数端末に影響する主要パッケージと優先して更新を検討すべきソフト上位 3〜5 件\n"
        "  4. **台帳整合（NetBox）**：登録機器数・IP 数・Fleet 側との大きなズレがあれば指摘\n"
        "  5. **今週のおすすめアクション**：優先度順に 1〜3 項目（特になければ「対応不要」）\n\n"
        f"スナップショット JSON:\n```json\n{snapshot_json}\n```"
    )


def failure_detail(snapshot: dict[str, Any]) -> str:
    f_cnt = snapshot.get("fleet", {}).get("hosts_count", "?")
    c_cnt = snapshot.get("fleet", {}).get("total_cve_count", "?")
    n_cnt = snapshot.get("netbox", {}).get("devices_count", "?")
    return f"Fleet 端末: `{f_cnt}` / 総 CVE: `{c_cnt}` / NetBox 機器: `{n_cnt}`"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run weekly Fleet + NetBox inventory & CVE digest.")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--dry-run", action="store_true", help="Skip Discord webhook posting.")
    parser.add_argument("--force", action="store_true", help="Bypass daily run cap.")
    args = parser.parse_args()

    return run_guarded_job(
        job_slug="weekly-inventory-cve",
        header_title="🛡️ **週次 棚卸し＆CVE 要約 (Fleet / NetBox)**",
        env_file=args.env_file,
        dry_run=args.dry_run,
        force=args.force,
        collect_fn=collect_snapshot,
        prompt_fn=build_prompt,
        failure_detail_fn=failure_detail,
    )


if __name__ == "__main__":
    raise SystemExit(main())
