#!/usr/bin/env python3
"""Weekly read-only GitHub example-user/home-infra-sanitized patrol digest via isolated OpenCode.

Runs on the host (never inside the opencode container), reads open Issues/PRs,
recent 7-day commits, and `状態：` lines from `docs/` without modifying anything,
summarizes with the isolated OpenCode `batch` agent, saves an audit log, and
posts to Discord.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

from common import DEFAULT_ENV_FILE, run_cmd, run_guarded_job


def collect_gh_json(args: list[str]) -> Any:
    rc, out, err = run_cmd(["gh", *args], timeout=20)
    if rc != 0:
        return {"error": (err.strip() or f"gh failed ({rc})")[:200]}
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        return {"error": f"json decode error: {exc}"}


def collect_doc_statuses(repo_dir: Path) -> list[dict[str, str]]:
    statuses: list[dict[str, str]] = []
    docs_dir = repo_dir / "docs"
    if not docs_dir.is_dir():
        return statuses
    for md_path in sorted(docs_dir.rglob("*.md")):
        rel = md_path.relative_to(repo_dir).as_posix()
        title = ""
        status_line = ""
        try:
            for line in md_path.read_text(encoding="utf-8").splitlines()[:10]:
                s = line.strip()
                if not title and s.startswith("# "):
                    title = s[2:].strip()
                if s.startswith("状態：") or s.startswith("実施日："):
                    status_line = s
                    break
        except OSError:
            continue
        statuses.append({"file": rel, "title": title, "status": status_line})
    return statuses


def collect_snapshot(cfg: dict[str, str], now: dt.datetime) -> dict[str, Any]:
    repo = cfg.get("GITHUB_REPO") or "example-user/home-infra-sanitized"
    repo_dir = Path(cfg.get("HOME_INFRA_DIR") or "/home/infra-admin/dev/home-infra")

    issues = collect_gh_json(
        [
            "issue",
            "list",
            "-R",
            repo,
            "--state",
            "open",
            "--limit",
            "30",
            "--json",
            "number,title,labels,createdAt,updatedAt",
        ]
    )
    prs = collect_gh_json(
        [
            "pr",
            "list",
            "-R",
            repo,
            "--state",
            "open",
            "--limit",
            "20",
            "--json",
            "number,title,isDraft,createdAt,updatedAt",
        ]
    )

    rc_log, log_out, _ = run_cmd(
        ["git", "-C", str(repo_dir), "log", "--since=7 days ago", "--pretty=format:%h %ad %s", "--date=short", "-n", "30"],
        timeout=15,
    )
    recent_commits = [l for l in log_out.splitlines() if l.strip()] if rc_log == 0 else []

    rc_st, st_out, _ = run_cmd(["git", "-C", str(repo_dir), "status", "-sb"], timeout=10)
    git_status = [l for l in st_out.splitlines() if l.strip()][:20] if rc_st == 0 else []

    return {
        "timestamp_jst": now.isoformat(timespec="seconds"),
        "repo": repo,
        "open_issues": issues,
        "open_prs": prs,
        "recent_commits_7d_count": len(recent_commits),
        "recent_commits_7d": recent_commits[:20],
        "local_git_status": git_status,
        "doc_statuses": collect_doc_statuses(repo_dir),
    }


def build_prompt(snapshot: dict[str, Any]) -> str:
    snapshot_json = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    return (
        "あなたは自宅インフラ管理リポジトリ（example-user/home-infra-sanitized）の週次巡回担当です。"
        "以下の読み取り専用スナップショット JSON だけを根拠に、日本語で簡潔な週次レポートを作成してください。\n"
        "制約:\n"
        "- JSON にない Issue 番号・事実を推測や補完しないこと\n"
        "- ツール実行やファイル読み書きは一切行わず、与えられた JSON だけで回答すること\n"
        "- Discord 投稿向けに全体 1200 文字以内の見やすい Markdown にすること\n"
        "- 出力構成:\n"
        "  1. 先頭行に総合ステータス（`🟢 順調` / `🟡 整理候補あり`）と 1 行要約\n"
        "  2. **今週の動き（直近 7 日コミット）**：主な進捗を 2〜4 行で整理\n"
        "  3. **未クローズ Issue / PR**：残っている Issue・PR の一覧と、すでに構築済みでクローズ候補になりそうなものがあれば指摘\n"
        "  4. **設計書とローカル同期状態**：`docs/` の `状態：` や `ubuntu-desktop` ローカルの未コミット差分・未同期があれば簡潔に記載\n"
        "  5. **来週のおすすめ次アクション**：1〜3 項目\n\n"
        f"スナップショット JSON:\n```json\n{snapshot_json}\n```"
    )


def failure_detail(snapshot: dict[str, Any]) -> str:
    issues = snapshot.get("open_issues")
    i_cnt = len(issues) if isinstance(issues, list) else "err"
    c_cnt = snapshot.get("recent_commits_7d_count", "?")
    return f"Open Issue: `{i_cnt}` / 直近7日コミット: `{c_cnt}`"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run weekly GitHub home-infra patrol digest.")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--dry-run", action="store_true", help="Skip Discord webhook posting.")
    parser.add_argument("--force", action="store_true", help="Bypass daily run cap.")
    args = parser.parse_args()

    return run_guarded_job(
        job_slug="weekly-github-patrol",
        header_title="🗂️ **週次 GitHub 巡回レポート (example-user/home-infra-sanitized)**",
        env_file=args.env_file,
        dry_run=args.dry_run,
        force=args.force,
        collect_fn=collect_snapshot,
        prompt_fn=build_prompt,
        failure_detail_fn=failure_detail,
    )


if __name__ == "__main__":
    raise SystemExit(main())
