"""Block web search queries that may leak secrets, PII, or local system details."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

# Paths, env vars, and host-specific strings we never send to the public web.
SYSTEM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"/etc/\w+"),
    re.compile(r"/proc/\w+"),
    re.compile(r"/sys/\w+"),
    re.compile(r"(?i)hostname\s*[:=]\s*\S+"),
    re.compile(r"(?i)\buname\b|\bkernel\s+version\b"),
    re.compile(r"\$(?:HOME|USER|PATH|SHELL|SUDO_USER)\b"),
    re.compile(r"(?i)root:\w+"),
    re.compile(r"ssh-rsa\s+\S+"),
)

# When Presidio is not installed, catch obvious PII with simple patterns.
_FALLBACK_PII_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
    re.compile(r"\b(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
)


def _matches_system_info(text: str) -> bool:
    return any(p.search(text) for p in SYSTEM_PATTERNS)


def _fallback_pii(text: str) -> bool:
    return any(p.search(text) for p in _FALLBACK_PII_PATTERNS)


def _detect_secrets_hits(text: str) -> bool:
    from detect_secrets import SecretsCollection

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".txt",
        delete=False,
        encoding="utf-8",
    ) as tmp:
        tmp.write(text)
        path = tmp.name
    try:
        collection = SecretsCollection()
        collection.scan_file(path)
        # SecretsCollection defines __bool__ but not __len__ (detect-secrets 1.x).
        return bool(collection)
    finally:
        Path(path).unlink(missing_ok=True)


def _presidio_pii_hits(text: str) -> bool:
    try:
        from presidio_analyzer import AnalyzerEngine
    except ImportError:
        return _fallback_pii(text)

    try:
        engine = AnalyzerEngine()
        results = engine.analyze(text=text, language="en")
        return len(results) > 0
    except Exception:
        return _fallback_pii(text)


MAX_QUERY_LEN = 500


def validate_web_search_query(query: str) -> tuple[bool, str]:
    """Return (allowed, error_message). error_message is empty when allowed."""
    q = (query or "").strip()
    if not q:
        return False, "Search query is empty."
    if len(q) > MAX_QUERY_LEN:
        return False, f"Search query is too long (max {MAX_QUERY_LEN} characters)."

    if _matches_system_info(q):
        return (
            False,
            "This query looks like local system information (paths, env vars, or "
            "host details). Remove that and search for a general topic instead.",
        )

    if _detect_secrets_hits(q):
        return (
            False,
            "This query may contain secrets (keys, tokens, or passwords). "
            "Do not search the web with credentials or private tokens.",
        )

    if _presidio_pii_hits(q):
        return (
            False,
            "This query may contain personal information (names, emails, or phone "
            "numbers). Use a generic search phrase without private details.",
        )

    return True, ""
