#!/usr/bin/env python3
"""Shared host-side guardrails for OpenCode scheduled read-only batch jobs."""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

JST = dt.timezone(dt.timedelta(hours=9))
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_FILE = SCRIPT_DIR / ".env"


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


def http_get_json(url: str, token: str = "", timeout: int = 15) -> Any:
    headers = {
        "Accept": "application/json",
        "User-Agent": "home-infra-opencode-jobs/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


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
    ok = rc == 0 and bool(summary)
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
            "User-Agent": "home-infra-opencode-jobs/1.0",
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


def prune_old_logs(log_dir: Path, prefix: str, keep_days: int = 30) -> None:
    cutoff = time.time() - keep_days * 86400
    for path in log_dir.glob(f"{prefix}-*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def run_guarded_job(
    *,
    job_slug: str,
    header_title: str,
    env_file: Path,
    dry_run: bool,
    force: bool,
    collect_fn: Callable[[dict[str, str], dt.datetime], dict[str, Any]],
    prompt_fn: Callable[[dict[str, Any]], str],
    failure_detail_fn: Callable[[dict[str, Any]], str],
) -> int:
    cfg = load_env_file(env_file)
    log_dir = Path(cfg.get("OPENCODE_LOG_DIR") or os.path.expanduser("~/.local/state/opencode-jobs"))
    log_dir.mkdir(parents=True, exist_ok=True)

    lock_path = log_dir / f"{job_slug}.lock"
    with lock_path.open("w", encoding="utf-8") as lock_fh:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"Another {job_slug} run is already in progress.", file=sys.stderr)
            return 2

        now = dt.datetime.now(tz=JST)
        today = now.strftime("%Y-%m-%d")
        max_runs = int(cfg.get("OPENCODE_MAX_DAILY_RUNS") or "5")
        count_file = log_dir / f"count-{job_slug}-{today}.txt"
        current_count = 0
        if count_file.is_file():
            try:
                current_count = int(count_file.read_text(encoding="utf-8").strip())
            except ValueError:
                current_count = 0

        if not force and current_count >= max_runs:
            print(
                f"Daily cap reached for {job_slug} ({current_count}/{max_runs}). Use --force to override.",
                file=sys.stderr,
            )
            return 3

        count_file.write_text(str(current_count + 1), encoding="utf-8")

        snapshot = collect_fn(cfg, now)

        container = cfg.get("OPENCODE_CONTAINER") or "opencode"
        primary_model = cfg.get("OPENCODE_PRIMARY_MODEL") or "sakura/preview/DeepSeek-V4.1-Flash-1017"
        fallback_model = cfg.get("OPENCODE_FALLBACK_MODEL") or "opencode/deepseek-v4.1-flash-free"
        timeout_sec = int(cfg.get("OPENCODE_TIMEOUT_SEC") or "120")

        prompt = prompt_fn(snapshot)
        attempts: list[dict[str, Any]] = []
        result = invoke_opencode(container, primary_model, prompt, timeout_sec)
        attempts.append(result)

        if not result["ok"] and fallback_model and fallback_model != primary_model:
            result = invoke_opencode(container, fallback_model, prompt, timeout_sec)
            attempts.append(result)

        header = f"{header_title} ({now.strftime('%Y-%m-%d %H:%M JST')})"
        if result["ok"]:
            footer = (
                f"\n\n_model: `{result['model']}` / {result['duration_sec']}s / cost: {result.get('cost')}_"
            )
            discord_message = f"{header}\n\n{result['summary']}{footer}"
        else:
            discord_message = (
                f"🔴 **{header_title} の生成に失敗** ({now.strftime('%Y-%m-%d %H:%M JST')})\n"
                f"- OpenCode 終了コード: `{result['exit_code']}`（試行: {len(attempts)} 回）\n"
                f"- {failure_detail_fn(snapshot)}\n"
                f"- ログ: `{log_dir}`"
            )

        webhook_url = cfg.get("DISCORD_WEBHOOK_URL", "")
        if dry_run:
            discord_status = "dry_run"
        elif not webhook_url:
            discord_status = "skipped_no_webhook"
        else:
            sent_ok, sent_detail = post_discord(webhook_url, discord_message)
            discord_status = f"sent:{sent_detail}" if sent_ok else f"error:{sent_detail}"

        audit = {
            "job": job_slug,
            "timestamp_jst": now.isoformat(timespec="seconds"),
            "ok": result["ok"],
            "model_used": result["model"],
            "duration_sec": result["duration_sec"],
            "tokens": result["tokens"],
            "cost": result["cost"],
            "discord_status": discord_status,
            "attempts": [{k: v for k, v in a.items() if k != "summary"} for a in attempts],
            "summary": result["summary"],
            "snapshot": snapshot,
        }
        log_path = log_dir / f"{job_slug}-{now.strftime('%Y%m%d-%H%M%S')}.json"
        log_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(log_path, 0o600)
        prune_old_logs(log_dir, job_slug, keep_days=30)

        print(discord_message)
        print(f"\n[audit] log={log_path} discord={discord_status}", file=sys.stderr)
        return 0 if result["ok"] else 1
