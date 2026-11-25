#!/usr/bin/env python3
"""Host-side thin wrapper for the daily ubuntu-desktop health digest.

Security & guardrail boundaries:
- Runs on the host (non-root user in the docker group) and collects ONLY
  read-only host/Docker/journalctl/Zabbix metrics deterministically.
- Never mounts host files, docker.sock, Zabbix credentials, or Discord Webhook
  URLs into the isolated `opencode` container.
- Invokes `opencode run --agent batch --format json` with a strict timeout,
  daily invocation cap, overlap lock, and primary->fallback model switch.
- Saves structured JSON audit logs locally (30-day retention) and posts the
  summary to Discord via Webhook from the host side.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

JST = dt.timezone(dt.timedelta(hours=9))
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_FILE = SCRIPT_DIR / ".env"

ZABBIX_SEVERITY = {
    "0": "未分類",
    "1": "情報",
    "2": "警告",
    "3": "軽度",
    "4": "重度",
    "5": "致命的",
}

SECRET_REDACT_RE = re.compile(
    r"(?i)(authorization:\s*bearer\s+\S+|bearer\s+[A-Za-z0-9._-]{16,}|"
    r"(?:password|passwd|token|api[_-]?key|secret)\s*[=:]\s*\S+)"
)


def load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        env[key] = val
    return env


def run_cmd(cmd: list[str], timeout: int = 20) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd[0]}"


def collect_host_metrics(now: dt.datetime) -> dict[str, Any]:
    meminfo: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].endswith(":"):
                meminfo[parts[0][:-1]] = int(parts[1]) * 1024
    except OSError:
        pass

    mem_total = meminfo.get("MemTotal", 0)
    mem_avail = meminfo.get("MemAvailable", 0)
    mem_used = max(mem_total - mem_avail, 0)

    disk = shutil.disk_usage("/")
    load1, load5, load15 = os.getloadavg()

    uptime_sec = 0.0
    try:
        uptime_sec = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        pass

    return {
        "hostname": socket.gethostname(),
        "timestamp_jst": now.isoformat(timespec="seconds"),
        "uptime_hours": round(uptime_sec / 3600.0, 1),
        "load_average": {"1m": round(load1, 2), "5m": round(load5, 2), "15m": round(load15, 2)},
        "memory_gib": {
            "total": round(mem_total / (1024**3), 2),
            "used": round(mem_used / (1024**3), 2),
            "available": round(mem_avail / (1024**3), 2),
            "used_pct": round((mem_used / mem_total) * 100.0, 1) if mem_total else None,
        },
        "root_disk_gib": {
            "total": round(disk.total / (1024**3), 1),
            "used": round(disk.used / (1024**3), 1),
            "free": round(disk.free / (1024**3), 1),
            "used_pct": round((disk.used / disk.total) * 100.0, 1) if disk.total else None,
        },
    }


def collect_docker_metrics() -> dict[str, Any]:
    rc, ps_out, ps_err = run_cmd(["docker", "ps", "-a", "--format", "{{json .}}"], timeout=15)
    if rc != 0:
        return {"error": ps_err.strip() or f"docker ps failed ({rc})"}

    names: list[str] = []
    ps_map: dict[str, dict[str, Any]] = {}
    for line in ps_out.splitlines():
        line = line.strip()
        if not line:
            continue
        item = json.loads(line)
        name = item.get("Names", "")
        names.append(name)
        ps_map[name] = item

    inspect_map: dict[str, dict[str, Any]] = {}
    if names:
        rc_i, ins_out, _ = run_cmd(["docker", "inspect", *names], timeout=15)
        if rc_i == 0 and ins_out.strip():
            for obj in json.loads(ins_out):
                cname = obj.get("Name", "").lstrip("/")
                state = obj.get("State", {})
                health = state.get("Health", {})
                inspect_map[cname] = {
                    "state": state.get("Status"),
                    "health": health.get("Status") if health else "none",
                    "restart_count": obj.get("RestartCount", 0),
                    "oom_killed": state.get("OOMKilled", False),
                    "exit_code": state.get("ExitCode", 0),
                    "restart_policy": obj.get("HostConfig", {}).get("RestartPolicy", {}).get("Name", ""),
                }

    stats_map: dict[str, dict[str, str]] = {}
    rc_s, st_out, _ = run_cmd(["docker", "stats", "--no-stream", "--format", "{{json .}}"], timeout=20)
    if rc_s == 0:
        for line in st_out.splitlines():
            line = line.strip()
            if not line:
                continue
            st = json.loads(line)
            stats_map[st.get("Name", "")] = {
                "cpu_pct": st.get("CPUPerc", ""),
                "mem_usage": st.get("MemUsage", ""),
                "mem_pct": st.get("MemPerc", ""),
            }

    containers: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    running_count = 0
    for name in sorted(names):
        ins = inspect_map.get(name, {})
        st = stats_map.get(name, {})
        state = ins.get("state") or ps_map[name].get("State", "")
        health = ins.get("health", "none")
        restarts = ins.get("restart_count", 0)
        oom = ins.get("oom_killed", False)
        exit_code = ins.get("exit_code", 0)
        policy = ins.get("restart_policy", "")

        if state == "running":
            running_count += 1

        entry = {
            "name": name,
            "state": state,
            "status": ps_map[name].get("Status", ""),
            "health": health,
            "restart_count": restarts,
            "oom_killed": oom,
            "cpu_pct": st.get("cpu_pct", ""),
            "mem_usage": st.get("mem_usage", ""),
        }
        containers.append(entry)

        is_oneshot_ok = (state == "exited" and exit_code == 0 and policy in ("", "no"))
        if (state != "running" and not is_oneshot_ok) or health == "unhealthy" or oom or restarts > 0:
            anomalies.append(entry)

    return {
        "total_containers": len(containers),
        "running_containers": running_count,
        "anomalies": anomalies,
        "containers": containers,
    }


def collect_journal_errors() -> dict[str, Any]:
    rc, out, err = run_cmd(
        [
            "journalctl",
            "-p",
            "err",
            "-S",
            "24 hours ago",
            "--no-pager",
            "-n",
            "30",
            "--output=short-iso",
        ],
        timeout=15,
    )
    if rc != 0:
        return {"error": err.strip() or f"journalctl failed ({rc})", "lines": []}
    lines = []
    for raw in out.splitlines():
        line = raw.strip()
        if not line or line == "-- No entries --":
            continue
        lines.append(SECRET_REDACT_RE.sub("[REDACTED]", line)[:240])
    return {"count_shown": len(lines), "lines": lines}


def zabbix_rpc(url: str, token: str, method: str, params: dict[str, Any]) -> Any:
    payload = json.dumps(
        {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json-rpc",
            "Authorization": f"Bearer {token}",
            "User-Agent": "home-infra-daily-health/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if "error" in data:
        raise RuntimeError(f"Zabbix RPC {method} error: {data['error']}")
    return data.get("result")


def collect_zabbix_metrics(url: str, token: str) -> dict[str, Any]:
    if not url or not token:
        return {"status": "not_configured"}
    try:
        hosts_raw = zabbix_rpc(
            url,
            token,
            "host.get",
            {
                "output": ["hostid", "host", "name", "status", "active_available"],
                "selectHostGroups": ["name"],
                "selectInterfaces": ["type", "available", "error"],
                "filter": {"status": "0"},
            },
        )
        problems_raw = zabbix_rpc(
            url,
            token,
            "problem.get",
            {
                "output": ["eventid", "objectid", "name", "severity", "clock", "acknowledged"],
                "recent": False,
                "sortfield": ["eventid"],
                "sortorder": "DESC",
                "limit": 30,
            },
        )

        trigger_ids = list({p["objectid"] for p in problems_raw if p.get("objectid")})
        trigger_host_map: dict[str, list[dict[str, Any]]] = {}
        if trigger_ids:
            triggers_raw = zabbix_rpc(
                url,
                token,
                "trigger.get",
                {
                    "triggerids": trigger_ids,
                    "output": ["triggerid"],
                    "selectHosts": ["host", "name"],
                    "selectHostGroups": ["name"],
                },
            )
            for t in triggers_raw:
                groups = [g.get("name", "") for g in t.get("hostgroups", [])]
                hosts = [h.get("name") or h.get("host", "") for h in t.get("hosts", [])]
                trigger_host_map[t["triggerid"]] = [{"hosts": hosts, "groups": groups}]

        avail_map = {"0": "unknown_or_unused", "1": "available", "2": "unavailable"}
        hosts = []
        for h in hosts_raw:
            groups = [g.get("name", "") for g in h.get("hostgroups", [])]
            ifaces = [
                {
                    "type": i.get("type"),
                    "available": avail_map.get(str(i.get("available", "0")), str(i.get("available"))),
                    "error": (i.get("error") or "")[:120],
                }
                for i in h.get("interfaces", [])
            ]
            hosts.append(
                {
                    "name": h.get("name") or h.get("host"),
                    "groups": groups,
                    "active_available": avail_map.get(
                        str(h.get("active_available", "0")), str(h.get("active_available"))
                    ),
                    "interfaces": ifaces,
                }
            )

        problems = []
        for p in problems_raw:
            ts = int(p.get("clock", "0"))
            ts_jst = dt.datetime.fromtimestamp(ts, tz=JST).isoformat(timespec="seconds") if ts else ""
            meta = trigger_host_map.get(p.get("objectid", ""), [{}])[0]
            problems.append(
                {
                    "severity": ZABBIX_SEVERITY.get(str(p.get("severity", "0")), str(p.get("severity"))),
                    "name": p.get("name", ""),
                    "since_jst": ts_jst,
                    "acknowledged": p.get("acknowledged") == "1",
                    "hosts": meta.get("hosts", []),
                    "groups": meta.get("groups", []),
                }
            )

        return {
            "status": "ok",
            "monitored_hosts": hosts,
            "active_problems_count": len(problems),
            "active_problems": problems,
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": str(exc)[:240]}


def build_prompt(snapshot: dict[str, Any]) -> str:
    snapshot_json = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    return (
        "あなたは自宅インフラ（ubuntu-desktop）の日次ヘルス要約担当です。"
        "以下の読み取り専用スナップショット JSON だけを根拠に、日本語で簡潔な日次レポートを作成してください。\n"
        "制約:\n"
        "- JSON にない数値・事実を推測や補完しないこと\n"
        "- ツール実行やファイル読み書きは一切行わず、与えられた JSON だけで回答すること\n"
        "- Discord 投稿向けに全体 1200 文字以内の見やすい Markdown にすること\n"
        "- Zabbix で `Home/PC` グループ（macOS ノート PC / workstation-01）の疎通断・Ping 障害が出ている場合は、"
        "スリープ・電源断による想定ノイズとして重大障害と分けて扱うこと\n"
        "- 出力構成:\n"
        "  1. 先頭行に総合判定（`🟢 正常` / `🟡 要確認` / `🔴 要対応`）と 1 行要約\n"
        "  2. **リソース・Docker**：メモリ空き・ディスク残量・稼働コンテナ数・異常/再起動コンテナの有無\n"
        "  3. **Zabbix 監視**：未解決障害の件数と内容（`Home/PC` 由来ノイズは区別して記載）\n"
        "  4. **直近 24h エラーログ**：注目すべき `journalctl -p err` のみ要約（問題なければ「特記事項なし」）\n"
        "  5. **次の対応**：正常時は「対応不要」、注意点がある場合のみ 1〜3 項目\n\n"
        f"スナップショット JSON:\n```json\n{snapshot_json}\n```"
    )


def invoke_opencode(container: str, model: str, prompt: str, timeout_sec: int) -> dict[str, Any]:
    started = time.monotonic()
    cmd = [
        "docker",
        "exec",
        container,
        "opencode",
        "run",
        "--agent",
        "batch",
        "--model",
        model,
        "--format",
        "json",
        prompt,
    ]
    rc, stdout, stderr = run_cmd(cmd, timeout=timeout_sec)
    duration = round(time.monotonic() - started, 2)

    text_chunks: list[str] = []
    tokens: dict[str, Any] = {}
    cost: float | None = None
    parse_errors = 0

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            parse_errors += 1
            continue
        ev_type = ev.get("type")
        part = ev.get("part") or {}
        if ev_type == "text" and isinstance(part.get("text"), str):
            text_chunks.append(part["text"])
        elif ev_type == "step_finish":
            if isinstance(part.get("tokens"), dict):
                tokens = part["tokens"]
            if "cost" in part:
                cost = part["cost"]

    summary = "".join(text_chunks).strip()
    ok = (rc == 0 and bool(summary))
    return {
        "ok": ok,
        "model": model,
        "exit_code": rc,
        "duration_sec": duration,
        "tokens": tokens,
        "cost": cost,
        "parse_errors": parse_errors,
        "summary": summary,
        "stderr_tail": stderr.strip()[-400:] if stderr else "",
    }


def post_discord(webhook_url: str, content: str) -> tuple[bool, str]:
    trimmed = content if len(content) <= 1900 else content[:1885] + "\n…(省略)"
    body = json.dumps({"content": trimmed}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "home-infra-daily-health/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return True, f"http_{resp.status}"
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        return False, f"http_{exc.code}: {detail}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:200]


def prune_old_logs(log_dir: Path, keep_days: int = 30) -> None:
    cutoff = time.time() - keep_days * 86400
    for path in log_dir.glob("daily-health-*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Run isolated OpenCode daily health digest.")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--dry-run", action="store_true", help="Skip Discord webhook posting.")
    parser.add_argument("--force", action="store_true", help="Bypass daily run cap.")
    args = parser.parse_args()

    cfg = load_env_file(args.env_file)
    log_dir = Path(cfg.get("OPENCODE_LOG_DIR") or os.path.expanduser("~/.local/state/opencode-jobs"))
    log_dir.mkdir(parents=True, exist_ok=True)

    lock_path = log_dir / "daily-health.lock"
    with lock_path.open("w", encoding="utf-8") as lock_fh:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another daily-health run is already in progress.", file=sys.stderr)
            return 2

        now = dt.datetime.now(tz=JST)
        today = now.strftime("%Y-%m-%d")
        max_runs = int(cfg.get("OPENCODE_MAX_DAILY_RUNS") or "5")
        count_file = log_dir / f"count-{today}.txt"
        current_count = 0
        if count_file.is_file():
            try:
                current_count = int(count_file.read_text(encoding="utf-8").strip())
            except ValueError:
                current_count = 0

        if not args.force and current_count >= max_runs:
            print(f"Daily cap reached ({current_count}/{max_runs}). Use --force to override.", file=sys.stderr)
            return 3

        count_file.write_text(str(current_count + 1), encoding="utf-8")

        snapshot = {
            "host": collect_host_metrics(now),
            "docker": collect_docker_metrics(),
            "journal_err_24h": collect_journal_errors(),
            "zabbix": collect_zabbix_metrics(
                cfg.get("ZABBIX_API_URL", ""),
                cfg.get("ZABBIX_API_TOKEN", ""),
            ),
        }

        container = cfg.get("OPENCODE_CONTAINER") or "opencode"
        primary_model = cfg.get("OPENCODE_PRIMARY_MODEL") or "sakura/preview/DeepSeek-V4.1-Flash-1017"
        fallback_model = cfg.get("OPENCODE_FALLBACK_MODEL") or "opencode/deepseek-v4.1-flash-free"
        timeout_sec = int(cfg.get("OPENCODE_TIMEOUT_SEC") or "120")

        prompt = build_prompt(snapshot)
        attempts: list[dict[str, Any]] = []
        result = invoke_opencode(container, primary_model, prompt, timeout_sec)
        attempts.append(result)

        if not result["ok"] and fallback_model and fallback_model != primary_model:
            result = invoke_opencode(container, fallback_model, prompt, timeout_sec)
            attempts.append(result)

        header = f"📊 **ubuntu-desktop 日次ヘルス要約** ({now.strftime('%Y-%m-%d %H:%M JST')})"
        if result["ok"]:
            footer = f"\n\n_model: `{result['model']}` / {result['duration_sec']}s / cost: {result.get('cost')}_"
            discord_message = f"{header}\n\n{result['summary']}{footer}"
        else:
            anomalies = snapshot["docker"].get("anomalies", [])
            zbx_count = snapshot["zabbix"].get("active_problems_count", "?")
            discord_message = (
                f"🔴 **ubuntu-desktop 日次ヘルス要約の生成に失敗** ({now.strftime('%Y-%m-%d %H:%M JST')})\n"
                f"- OpenCode 終了コード: `{result['exit_code']}`（試行: {len(attempts)} 回）\n"
                f"- Docker 異常コンテナ数: `{len(anomalies)}` / Zabbix 未解決障害: `{zbx_count}`\n"
                f"- ログ: `{log_dir}`"
            )

        webhook_url = cfg.get("DISCORD_WEBHOOK_URL", "")
        if args.dry_run:
            discord_status = "dry_run"
        elif not webhook_url:
            discord_status = "skipped_no_webhook"
        else:
            sent_ok, sent_detail = post_discord(webhook_url, discord_message)
            discord_status = f"sent:{sent_detail}" if sent_ok else f"error:{sent_detail}"

        audit = {
            "timestamp_jst": now.isoformat(timespec="seconds"),
            "ok": result["ok"],
            "model_used": result["model"],
            "duration_sec": result["duration_sec"],
            "tokens": result["tokens"],
            "cost": result["cost"],
            "discord_status": discord_status,
            "attempts": [
                {k: v for k, v in a.items() if k != "summary"} for a in attempts
            ],
            "summary": result["summary"],
            "snapshot": snapshot,
        }
        log_path = log_dir / f"daily-health-{now.strftime('%Y%m%d-%H%M%S')}.json"
        log_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(log_path, 0o600)
        prune_old_logs(log_dir, keep_days=30)

        print(discord_message)
        print(f"\n[audit] log={log_path} discord={discord_status}", file=sys.stderr)
        return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
