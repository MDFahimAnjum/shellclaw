"""tldr-pages lookup from the bundled JSON dataset.

The dataset lives in wiki/data/tldr.json and is populated by
scripts/fetch_tldr.py.  Lookups are instant — no network call.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DATA_PATH = Path(__file__).parent / "data" / "tldr.json"


@lru_cache(maxsize=1)
def _load() -> dict:
    if not DATA_PATH.exists():
        return {}
    return json.loads(DATA_PATH.read_text())


@lru_cache(maxsize=1)
def all_tldr_command_names() -> frozenset[str]:
    """Return every command name present in the bundled tldr dataset."""
    return frozenset(_load().keys())


def lookup(command_name: str) -> dict | None:
    """Return the tldr entry for a command, or None if not found.

    Returns:
        {
          "description": "...",
          "examples": [{"description": "...", "command": "..."}, ...]
        }
    """
    data = _load()
    return data.get(command_name.lower())


def format_for_llm(command_name: str) -> str:
    """Return a plain-text tldr summary suitable for passing to the LLM."""
    entry = lookup(command_name)
    if entry is None:
        return f"No tldr page found for '{command_name}'."

    lines = [f"# {command_name}", entry.get("description", ""), ""]
    for ex in entry.get("examples", []):
        lines.append(f"- {ex['description']}")
        lines.append(f"  `{ex['command']}`")
    return "\n".join(lines)


def format_for_shell_line(cmd_line: str) -> str:
    """tldr summaries for each distinct executable in a full shell line (pipes, sudo)."""
    from .commands import command_names_in_shell_line

    names = command_names_in_shell_line(cmd_line)
    if not names:
        return f"No commands parsed from: {cmd_line!r}"

    blocks: list[str] = []
    for name in names:
        blocks.append(format_for_llm(name))
    return "\n\n---\n\n".join(blocks)
