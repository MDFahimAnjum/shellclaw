"""Flag parser — extract flags from a command string and describe them.

Uses the bundled tldr dataset as the source of flag descriptions.
Falls back to a generic description if the flag is not found.
"""

from __future__ import annotations

import re

from .commands import first_executable_from_segment, split_pipeline_segments
from .tldr import lookup

# Matches --long-flag or -s (short flags)
_FLAG_PATTERN = re.compile(r"(--[\w-]+|-[a-zA-Z])")


def extract_flags(cmd: str) -> list[str]:
    """Return all flags found in a command string."""
    return _FLAG_PATTERN.findall(cmd)


def describe_flags(cmd: str) -> list[dict]:
    """Return {flag, description} for flags in each pipeline segment (sudo stripped)."""
    segments = split_pipeline_segments(cmd)
    if not segments:
        segments = [cmd] if cmd.strip() else []

    multi = len(segments) > 1
    results: list[dict] = []
    for stage, seg in enumerate(segments, start=1):
        name = first_executable_from_segment(seg)
        if not name:
            continue
        flags = extract_flags(seg)
        if not flags:
            continue
        entry = lookup(name.lower())
        examples = entry.get("examples", []) if entry else []
        for flag in flags:
            description = _find_flag_description(flag, examples)
            results.append(
                {
                    "flag": flag,
                    "command": name,
                    "description": description,
                    "stage": stage,
                    "multi_segment": multi,
                }
            )
    return results


def _find_flag_description(flag: str, examples: list[dict]) -> str:
    """Search tldr examples for a plain-English description of a flag."""
    for ex in examples:
        command_text = ex.get("command", "")
        desc_text = ex.get("description", "")
        if flag in command_text and desc_text:
            return desc_text
    return "No description available."
