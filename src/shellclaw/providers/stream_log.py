"""Append provider HTTP request/response bodies to a log file (dev mode)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

_write_lock = asyncio.Lock()


async def log_provider_json_response(
    log_path: Path,
    *,
    provider_tag: str,
    request_payload: dict,
    response: httpx.Response,
) -> None:
    """Append request + full non-streaming JSON body to the provider I/O log."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    body = response.text
    header = (
        f"\n{'=' * 72}\n"
        f"{datetime.now(timezone.utc).isoformat()} [{provider_tag}]\n"
        "--- REQUEST ---\n"
        f"{json.dumps(request_payload, indent=2, ensure_ascii=False)}\n"
        "--- RESPONSE (JSON body) ---\n"
        f"{body}\n"
    )
    async with _write_lock:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(header)
            f.flush()
