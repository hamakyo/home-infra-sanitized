#!/usr/bin/env python3
"""Verify that no real Tailscale/LAN IPs, personal domains/emails, MACs, serials, audits, PNG metadata, or secrets remain."""

from __future__ import annotations

import base64
import os
import re
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SKIP_FILES = {"scripts/check_sanitized.py"}
REDACT_DEF_FILE = "stacks/data-platform/rag/redact.py"

# Strip ONLY embedded base64 font assets; inline SVG assets are decoded and scanned below
FONT_DATA_URI_RE = re.compile(r"data:font/woff2;base64,[A-Za-z0-9+/=]+")
SVG_DATA_URI_RE = re.compile(r"data:image/svg\+xml;base64,([A-Za-z0-9+/=]+)")
OTHER_IMG_DATA_URI_RE = re.compile(r"data:image/(?!svg\+xml)[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]+")

DISALLOWED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".heic", ".ico"}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_FORBIDDEN_CHUNKS = {b"tEXt", b"iTXt", b"zTXt", b"eXIf"}

RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "personal_email",
        re.compile(
            r"\b[A-Za-z0-9._%+-]+@(?!(?:[A-Za-z0-9-]+\.)*example\.com\b|users\.noreply\.github\.com\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
        ),
    ),
    (
        "non_reserved_home_domain",
        # Reject *.home.<non-example>.<tld> personal domains
        re.compile(r"(?i)\bhome\.(?!example\.com\b)[a-z0-9-]+\.(?:dev|com|net|org|jp|io|me|app|xyz|tech)\b"),
    ),
    (
        "non_placeholder_tailscale_ip",
        # Allow RFC 6598 placeholder range 100.64.0.{0,10,20,30,40,50} only
        re.compile(r"\b100\.(?!64\.0\.(?:0|10|20|30|40|50)\b)\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
    ),
    (
        "non_placeholder_lan_ip",
        # Allow sanitized sample subnets 192.168.50.* and 192.168.60.* only
        re.compile(r"\b192\.168\.(?!(?:50|60)\.)\d{1,3}\.\d{1,3}\b"),
    ),
    (
        "mac_address",
        re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b"),
    ),
    (
        "serial_number_literal",
        re.compile(r"(?i)\b(?:S/N|serial(?:\s*number)?)\s*[:=]\s*[A-Za-z0-9_-]{6,}\b"),
    ),
    (
        "excluded_audit_refs",
        re.compile(r"docs/audits/"),
    ),
    (
        "jwt_token",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    ),
    (
        "discord_webhook",
        re.compile(r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]+"),
    ),
    (
        "secret_literal",
        re.compile(
            r"(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|\b00[0-9a-f]{23}\b|-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----)"
        ),
    ),
    (
        "inline_secret_assignment",
        re.compile(
            r"""(?i)^\s*["']?[A-Za-z0-9_]*(?:password|passwd|secret|api_key|api_token|access_token|webhook_url|encryption_key)[A-Za-z0-9_]*["']?\s*[:=]\s*["']?(?!\s*$|[\$\{\[<]|replace[_-]with|change[_-]me|example|dummy|none\b|null\b|true\b|false\b|env\.|os\.environ|cfg\.get)[A-Za-z0-9._~+/=-]{8,}["']?\s*$"""
        ),
    ),
    (
        "operational_date",
        re.compile(r"\b20\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])\b"),
    ),
]

REDACT_REGEX_RULES = {"secret_literal", "jwt_token", "discord_webhook", "inline_secret_assignment"}
DATED_ADR_RE = re.compile(r"^20\d{2}-\d{2}-\d{2}-")
MCP_PROTOCOL_FILE = "stacks/data-platform/rag_mcp/server.py"


def inspect_png(path: Path, rel: str, violations: list[str]) -> None:
    if not rel.startswith("docs/diagrams/"):
        violations.append(f"[unexpected_png] {rel}: PNG assets are only permitted under docs/diagrams/")
    data = path.read_bytes()
    if len(data) < 8 or data[:8] != PNG_SIGNATURE:
        violations.append(f"[invalid_png] {rel}: missing PNG header signature")
        return
    pos = 8
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        ctype = data[pos + 4 : pos + 8]
        if ctype in PNG_FORBIDDEN_CHUNKS:
            violations.append(f"[png_metadata] {rel}: forbidden chunk {ctype.decode('ascii', 'replace')} found")
        pos += 12 + length


def scan_line(
    rel: str,
    lineno: int,
    line: str,
    active_rules: list[tuple[str, re.Pattern[str]]],
    violations: list[str],
    context_label: str = "",
) -> None:
    suffix = f" ({context_label})" if context_label else ""
    for rule_name, pattern in active_rules:
        # Line-level allowlist only for regex pattern definitions inside redact.py
        if rel == REDACT_DEF_FILE and rule_name in REDACT_REGEX_RULES and "re.compile(" in line:
            continue
        # Line-level allowlist only for MCP specification protocol version constant
        if rel == MCP_PROTOCOL_FILE and rule_name == "operational_date" and "PROTOCOL_VERSION" in line:
            continue
        m = pattern.search(line)
        if m:
            violations.append(f"[{rule_name}] {rel}:{lineno}{suffix}: matched {m.group(0)!r}")


def main() -> int:
    violations: list[str] = []

    if (ROOT / "docs" / "audits").exists():
        violations.append("docs/audits/ directory must not exist in sanitized repository")

    decisions_dir = ROOT / "docs" / "decisions"
    if decisions_dir.exists():
        for adr in sorted(decisions_dir.glob("*.md")):
            if DATED_ADR_RE.match(adr.name):
                violations.append(f"[dated_adr_filename] docs/decisions/{adr.name} must use sequential NNNN-<slug>.md")

    extra_pat_str = os.environ.get("EXTRA_FORBIDDEN_REGEX", "").strip()
    active_rules = list(RULES)
    if extra_pat_str:
        active_rules.append(("extra_forbidden", re.compile(extra_pat_str)))

    for path in sorted(ROOT.rglob("*")):
        if ".git" in path.parts or "__pycache__" in path.parts or not path.is_file():
            continue
        suffix_lower = path.suffix.lower()
        if suffix_lower in {".pyc", ".pyo"}:
            continue
        rel = str(path.relative_to(ROOT))
        if path.name == ".env":
            violations.append(f"[env_file] {rel} must not be committed")
            continue
        if suffix_lower in DISALLOWED_IMAGE_SUFFIXES:
            violations.append(f"[disallowed_image] {rel}: unexpected raster/binary image format")
            continue
        if suffix_lower == ".png":
            inspect_png(path, rel, violations)
            continue
        if rel in SKIP_FILES:
            continue

        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, raw_line in enumerate(text.splitlines(), 1):
            # Decode and inspect inline base64 SVG XML before stripping base64 payloads
            for svg_match in SVG_DATA_URI_RE.finditer(raw_line):
                try:
                    decoded_svg = base64.b64decode(svg_match.group(1), validate=True).decode("utf-8", errors="ignore")
                except Exception:
                    violations.append(f"[invalid_inline_svg] {rel}:{lineno}: failed to decode base64 SVG payload")
                    continue
                for svg_line in decoded_svg.splitlines():
                    scan_line(rel, lineno, svg_line, active_rules, violations, context_label="inline-svg")

            line = FONT_DATA_URI_RE.sub("data:[EMBEDDED_FONT]", raw_line)
            line = SVG_DATA_URI_RE.sub("data:[EMBEDDED_SVG]", line)
            line = OTHER_IMG_DATA_URI_RE.sub("data:[EMBEDDED_IMG]", line)
            scan_line(rel, lineno, line, active_rules, violations)

    if violations:
        print(f"FAILED: {len(violations)} sanitization violation(s) found:", file=sys.stderr)
        for v in violations[:50]:
            print(f"  - {v}", file=sys.stderr)
        return 1

    print("OK: zero sanitization violations found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
