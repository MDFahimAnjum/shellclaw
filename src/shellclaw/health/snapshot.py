"""Launch-time health snapshot.

Runs 4–5 fast read commands concurrently on startup and returns a list of
HealthItem objects describing the current machine state.  Target: < 2s.
Results are cached for the session.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from enum import Enum


class HealthStatus(str, Enum):
    OK = "ok"
    WARN = "warn"
    CRITICAL = "critical"


@dataclass
class HealthItem:
    label: str
    message: str
    status: HealthStatus
    diagnostic_prompt: str = ""  # Pre-fills the input when clicked


async def _run(cmd: list[str], timeout: float = 3.0) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode("utf-8", errors="replace")
    except (asyncio.TimeoutError, FileNotFoundError, OSError):
        return ""


async def _check_disk() -> HealthItem:
    output = await _run(["df", "-h", "--output=source,pcent,target"])
    worst_pct = 0
    worst_line = ""
    for line in output.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 3:
            pct_str = parts[1].rstrip("%")
            if pct_str.isdigit():
                pct = int(pct_str)
                if pct > worst_pct:
                    worst_pct = pct
                    worst_line = f"{parts[0]} is {pct}% full"

    if worst_pct >= 90:
        return HealthItem(
            label="Disk",
            message=worst_line,
            status=HealthStatus.CRITICAL,
            diagnostic_prompt=f"My disk is nearly full — {worst_line}. What should I do?",
        )
    if worst_pct >= 75:
        return HealthItem(
            label="Disk",
            message=worst_line,
            status=HealthStatus.WARN,
            diagnostic_prompt=f"My disk is getting full — {worst_line}. Can you help?",
        )
    return HealthItem(
        label="Disk",
        message="All drives have plenty of free space",
        status=HealthStatus.OK,
    )


async def _check_memory() -> HealthItem:
    output = await _run(["free", "-h"])
    for line in output.splitlines():
        if line.startswith("Mem:"):
            parts = line.split()
            if len(parts) >= 7:
                available = parts[6]
                total = parts[1]
                return HealthItem(
                    label="Memory",
                    message=f"{available} free of {total}",
                    status=HealthStatus.OK,
                )
    return HealthItem(label="Memory", message="Could not read memory info", status=HealthStatus.WARN)


async def _check_failed_services() -> HealthItem:
    output = await _run(["systemctl", "--failed", "--no-legend", "--no-pager"])
    lines = [l for l in output.strip().splitlines() if l.strip()]
    if not lines:
        return HealthItem(
            label="Services",
            message="Running normally",
            status=HealthStatus.OK,
        )
    # Extract first failing service name
    first = lines[0].split()[0] if lines[0].split() else "a service"
    count = len(lines)
    msg = (
        f"{first} has failed"
        if count == 1
        else f"{first} and {count - 1} other failed"
    )
    return HealthItem(
        label="Services",
        message=msg,
        status=HealthStatus.CRITICAL,
        diagnostic_prompt=f"{first} is failing. Can you help me fix it?",
    )


async def _check_updates() -> HealthItem:
    output = await _run(
        ["apt", "list", "--upgradable"],
        timeout=5.0,
    )
    lines = [l for l in output.splitlines() if "/" in l]
    if not lines:
        return HealthItem(
            label="Updates",
            message="All packages are up to date",
            status=HealthStatus.OK,
        )
    count = len(lines)
    return HealthItem(
        label="Updates",
        message=f"{count} package update{'s' if count != 1 else ''} available",
        status=HealthStatus.WARN,
        diagnostic_prompt=f"I have {count} package updates available. Should I install them?",
    )


async def run_health_snapshot() -> list[HealthItem]:
    """Run all health checks concurrently and return results."""
    results = await asyncio.gather(
        _check_disk(),
        _check_memory(),
        _check_failed_services(),
        _check_updates(),
        return_exceptions=True,
    )
    items: list[HealthItem] = []
    for r in results:
        if isinstance(r, HealthItem):
            items.append(r)
        else:
            items.append(HealthItem(
                label="Check",
                message="Could not complete this check",
                status=HealthStatus.WARN,
            ))
    return items
