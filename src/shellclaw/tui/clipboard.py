"""Shared clipboard utilities for the TUI."""

from __future__ import annotations

import asyncio
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from textual.app import App


async def copy_to_clipboard(text: str) -> bool:
    """Copy *text* to the system clipboard; returns True on success.

    Tries ``wl-copy`` (Wayland), ``xclip``, and ``xsel`` in order.
    """
    candidates = [
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
    ]
    for cmd in candidates:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            await proc.communicate(text.encode("utf-8", errors="replace"))
            if proc.returncode == 0:
                return True
        except (FileNotFoundError, OSError):
            continue
    return False


async def copy_and_notify(text: str, app: "App") -> None:
    """Copy *text* to the clipboard and show a Textual toast on *app*."""
    ok = await copy_to_clipboard(text)
    preview = text[:40].replace("\n", " ")
    if len(text) > 40:
        preview += "…"
    if ok:
        app.notify(f'Copied: "{preview}"', title="Clipboard", timeout=3)
    else:
        app.notify(
            "No clipboard tool found (install wl-copy, xclip, or xsel)",
            title="Copy failed",
            severity="warning",
            timeout=4,
        )
