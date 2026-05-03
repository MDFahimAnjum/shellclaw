"""Strip trailing API usage blobs (e.g. ``total_tokens``) from model text."""

from __future__ import annotations

import re

_TOTAL_TOKENS_RE = re.compile(
    r'["\']?total_tokens["\']?\s*:\s*(\d+)',
    re.IGNORECASE,
)


def format_tokens_compact(n: int) -> str:
    """Format token counts as compact strings (e.g. ``3k``, ``2.5M``)."""
    if n < 0:
        n = 0
    if n >= 1_000_000:
        x = n / 1_000_000
        s = f"{x:.1f}".rstrip("0").rstrip(".")
        return f"{s}M"
    if n >= 1_000:
        x = n / 1_000
        s = f"{x:.1f}".rstrip("0").rstrip(".")
        return f"{s}k"
    return str(n)


def split_trailing_token_usage(text: str) -> tuple[str, int | None]:
    """Return ``(body, total_tokens)`` with trailing JSON usage removed when detected.

    If the tail from the last ``{`` to end of string is a single brace-block that
    mentions ``total_tokens``, that block is stripped from ``body``. Otherwise
    ``body`` is unchanged; ``total_tokens`` is still set when a match exists anywhere.
    """
    raw = text
    stripped = text.rstrip()
    m = _TOTAL_TOKENS_RE.search(stripped)
    if m is None:
        return raw, None
    total = int(m.group(1))
    brace = stripped.rfind("{")
    if brace == -1:
        return raw, total
    tail = stripped[brace:]
    if tail.endswith("}") and "total_tokens" in tail.lower():
        return stripped[:brace].rstrip(), total
    return raw, total
