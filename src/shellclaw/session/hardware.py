"""Hardware profile collection and caching.

Builds a human-readable hardware summary from standard Linux tools and
caches it in ~/.local/share/shellclaw/hardware.json.
The profile is refreshed automatically if it is older than 7 days.
It is passed as context in the system prompt for every session.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

from ..config import DATA_DIR

PROFILE_PATH = DATA_DIR / "hardware.json"
REFRESH_AFTER_DAYS = 7


async def _run(cmd: list[str], timeout: float = 5.0) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode("utf-8", errors="replace").strip()
    except (asyncio.TimeoutError, FileNotFoundError, OSError):
        return ""


def _extract_lscpu_field(output: str, field: str) -> str:
    for line in output.splitlines():
        if line.startswith(field):
            return line.split(":", 1)[1].strip()
    return "unknown"


def _extract_free_total(output: str) -> str:
    for line in output.splitlines():
        if line.startswith("Mem:"):
            parts = line.split()
            return parts[1] if len(parts) > 1 else "unknown"
    return "unknown"


def _extract_disk_info(lsblk_output: str) -> str:
    disks: list[str] = []
    for line in lsblk_output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("sd") or (
            len(parts) >= 2 and parts[0].startswith("nvme")
        ):
            disks.append(f"{parts[0]} ({parts[1]})")
    return ", ".join(disks) if disks else "unknown"


def _extract_gpu(lspci_output: str) -> str:
    for line in lspci_output.splitlines():
        lower = line.lower()
        if "vga" in lower or "3d controller" in lower or "display" in lower:
            if ":" in line:
                return line.split(":", 2)[-1].strip()
    return "unknown"


async def collect_profile() -> dict:
    """Run hardware queries concurrently and return a profile dict."""
    cpu_out, mem_out, lsblk_out, lspci_out, os_out = await asyncio.gather(
        _run(["lscpu"]),
        _run(["free", "-h"]),
        _run(["lsblk", "-d", "-o", "NAME,SIZE,TYPE"]),
        _run(["lspci"]),
        _run(["cat", "/etc/os-release"]),
        return_exceptions=False,
    )

    cpu_model = _extract_lscpu_field(cpu_out, "Model name")
    cpu_cores = _extract_lscpu_field(cpu_out, "CPU(s)")
    cpu_speed = _extract_lscpu_field(cpu_out, "CPU max MHz") or _extract_lscpu_field(cpu_out, "CPU MHz")
    ram = _extract_free_total(mem_out)
    disk = _extract_disk_info(lsblk_out)
    gpu = _extract_gpu(lspci_out)

    distro = "unknown"
    for line in os_out.splitlines():
        if line.startswith("PRETTY_NAME="):
            distro = line.split("=", 1)[1].strip().strip('"')
            break

    return {
        "CPU": f"{cpu_model} ({cpu_cores} cores, {cpu_speed} MHz)",
        "RAM": ram,
        "Disk": disk,
        "GPU": gpu,
        "Distro": distro,
        "_collected_at": datetime.now().isoformat(),
    }


def _is_stale(profile: dict) -> bool:
    collected_at = profile.get("_collected_at", "")
    if not collected_at:
        return True
    try:
        age = datetime.now() - datetime.fromisoformat(collected_at)
        return age > timedelta(days=REFRESH_AFTER_DAYS)
    except ValueError:
        return True


def load_profile() -> dict | None:
    """Load the cached hardware profile, or None if unavailable."""
    if not PROFILE_PATH.exists():
        return None
    try:
        return json.loads(PROFILE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def save_profile(profile: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(json.dumps(profile, indent=2))


async def get_or_refresh_profile() -> dict:
    """Return cached profile, refreshing it if stale or missing."""
    existing = load_profile()
    if existing and not _is_stale(existing):
        return existing
    profile = await collect_profile()
    save_profile(profile)
    return profile
