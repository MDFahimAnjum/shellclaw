"""Risk level classification for solution steps. [NOT USED ANYWHERE NOW]

Classifies individual shell commands into safe / caution / danger.
This is used by the solution widget to colour-code steps and decide
whether a CONFIRM prompt is needed before execution.
"""

from __future__ import annotations

import re
from enum import Enum


class RiskLevel(str, Enum):
    SAFE = "safe"
    CAUTION = "caution"
    DANGER = "danger"


# Patterns that indicate a destructive, hard-to-reverse operation.
_DANGER_PATTERNS: list[re.Pattern] = [
    re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f\b"),    # rm -rf
    re.compile(r"\brm\s+-[a-zA-Z]*f[a-zA-Z]*r\b"),    # rm -fr
    re.compile(r"\bdd\b"),                              # dd (disk copy/wipe)
    re.compile(r"\bmkfs\b"),                            # format filesystem
    re.compile(r"\bshred\b"),                           # secure delete
    re.compile(r"\bwipefs\b"),                          # wipe filesystem signatures
    re.compile(r"\bparted\b.*\brm\b"),                  # parted rm (delete partition)
    re.compile(r">\s*/dev/[a-z]+"),                     # redirect to block device
    re.compile(r"\bfdisk\b.*\bw\b"),                    # fdisk write
]

# Patterns that require elevated care — involve sudo, deletion, or modification.
_CAUTION_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bsudo\b"),
    re.compile(r"\brm\b"),
    re.compile(r"\bchmod\b"),
    re.compile(r"\bchown\b"),
    re.compile(r"\bmv\b"),
    re.compile(r"\btruncate\b"),
    re.compile(r"\bkill\b"),
    re.compile(r"\bkillall\b"),
    re.compile(r"\bpkill\b"),
    re.compile(r"\bapt(-get)?\s+(remove|purge|autoremove)\b"),
    re.compile(r"\bdnf\s+(remove|erase)\b"),
    re.compile(r"\bpacman\s+-R"),
    re.compile(r"\bsystemctl\s+(stop|disable|mask)\b"),
    re.compile(r"\bjournalctl\s+--vacuum"),
    re.compile(r"\bapt\s+clean\b"),
    re.compile(r"\bapt-get\s+clean\b"),
]


def classify_risk(cmd: str) -> RiskLevel:
    """Return the risk level for a single shell command."""
    for pattern in _DANGER_PATTERNS:
        if pattern.search(cmd):
            return RiskLevel.DANGER

    for pattern in _CAUTION_PATTERNS:
        if pattern.search(cmd):
            return RiskLevel.CAUTION

    return RiskLevel.SAFE


def classify_steps(steps: list[dict]) -> RiskLevel:
    """Return the highest risk level across all steps."""
    levels = [classify_risk(step.get("command", "")) for step in steps]
    if RiskLevel.DANGER in levels:
        return RiskLevel.DANGER
    if RiskLevel.CAUTION in levels:
        return RiskLevel.CAUTION
    return RiskLevel.SAFE
