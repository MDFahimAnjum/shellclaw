"""Linux term glossary for the noun [?] feature.

find_terms(text) returns all glossary terms found in a block of text.
These terms are underlined in the TUI and trigger an explain popup when clicked.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

DATA_PATH = Path(__file__).parent / "data" / "glossary.json"


@lru_cache(maxsize=1)
def _load() -> dict[str, str]:
    if not DATA_PATH.exists():
        return {}
    return json.loads(DATA_PATH.read_text())


def lookup(term: str) -> str | None:
    """Return the plain-English definition of a Linux term, or None."""
    data = _load()
    return data.get(term.lower())


def find_terms(text: str) -> list[str]:
    """Return all glossary terms that appear (as whole words) in text."""
    data = _load()
    found: list[str] = []
    for term in data:
        pattern = re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)
        if pattern.search(text):
            found.append(term)
    return found


def find_terms_for_shell_command(cmd: str) -> list[str]:
    """Glossary hits for a command line (sudo/pipes normalized so real commands match)."""
    from .commands import text_for_glossary_search

    return find_terms(text_for_glossary_search(cmd))


def all_terms() -> dict[str, str]:
    """Return the complete glossary."""
    return dict(_load())
