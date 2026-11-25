"""Deterministic PII & secret redaction gate for Infra RAG corpus.

Runs BEFORE hashing, embedding, and storing any text in PostgreSQL `rag.chunks`.
Also provides a post-storage leakage scanner used by automated RAG evaluation.
"""

from __future__ import annotations

import re

# Patterns that must NEVER enter the vector index or be returned to an LLM.
REDACTION_RULES: list[tuple[str, re.Pattern[str], str]] = [
    (
        "private_key_block",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
        ),
        "[REDACTED:PRIVATE_KEY]",
    ),
    (
        "github_pat",
        re.compile(r"\b(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|gho_[A-Za-z0-9]{20,})\b"),
        "[REDACTED:GITHUB_TOKEN]",
    ),
    (
        "api_secret_key",
        re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16})\b"),
        "[REDACTED:API_KEY]",
    ),
    (
        "jwt_token",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
        "[REDACTED:JWT]",
    ),
    (
        "discord_webhook",
        re.compile(r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]+"),
        "[REDACTED:WEBHOOK_URL]",
    ),
    (
        "bearer_token_literal",
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}\b"),
        "Bearer [REDACTED:TOKEN]",
    ),
    (
        "inline_secret_assignment",
        re.compile(
            r"""(?im)^(\s*["']?[A-Za-z0-9_]*(?:password|passwd|secret|api_key|api_token|access_token|webhook_url|encryption_key)[A-Za-z0-9_]*["']?\s*[:=]\s*["']?)(?![\s$\{\[\]<]|replace[_-]with|$)([^\s"']{8,})(["']?)"""
        ),
        r"\1[REDACTED:SECRET_VALUE]\3",
    ),
    (
        "external_email",
        # Mask external/personal emails while keeping documented internal/example system labels
        re.compile(
            r"\b[A-Za-z0-9._%+-]+@(?!(?:[A-Za-z0-9-]+\.)*example\.com\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
        ),
        "[REDACTED:EMAIL]",
    ),
    (
        "jp_phone_number",
        re.compile(
            r"(?:\+81[-\s]?(?:[789]0|[1-9]\d{0,3})[-\s]?\d{2,4}[-\s]?\d{4}|\b0[789]0-?\d{4}-?\d{4}\b|\b0\d{1,4}-\d{1,4}-\d{4}\b)"
        ),
        "[REDACTED:PHONE]",
    ),
]


def redact_text(text: str) -> tuple[str, list[str]]:
    """Return (redacted_text, list_of_triggered_rule_names)."""
    out = text
    hits: list[str] = []
    for name, pattern, repl in REDACTION_RULES:
        new_out, count = pattern.subn(repl, out)
        if count > 0:
            out = new_out
            hits.extend([name] * count)
    return out, hits


def scan_leakage(text: str) -> list[str]:
    """Return list of rule names if any unredacted secret/PII pattern remains."""
    found: list[str] = []
    for name, pattern, _ in REDACTION_RULES:
        if pattern.search(text):
            found.append(name)
    return found
