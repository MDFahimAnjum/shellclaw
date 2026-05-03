"""In-memory log of side-terminal user runs and agent tool executions.

Used to bundle manual terminal activity into one LLM user message per chat turn
and to serve terminal_history_* tools without duplicating MessageHistory tool rows.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from ..config import MAX_TERMINAL_USER_MESSAGE_CHARS

TerminalEntryKind = Literal["user", "tool"]

# Bundled block header for the user message sent to the LLM.
USER_TERMINAL_BUNDLE_HEADER = "User commands ran:"

# Max lines/entries in summary tool (newest first after header).
_SUMMARY_MAX_ENTRIES = 80

# Per-command cap inside a bundle (remainder of total budget shared).
_PER_ENTRY_CAP = min(8000, MAX_TERMINAL_USER_MESSAGE_CHARS // 2)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class TerminalHistoryEntry:
    id: int
    timestamp: str
    kind: TerminalEntryKind
    description: str
    output: str


_LOG_SEP = "=" * 80


def _format_log_entry(entry: "TerminalHistoryEntry") -> str:
    """Render one entry as a human-readable debug block."""
    lines = [
        _LOG_SEP,
        f"[{entry.timestamp}] #{entry.id} kind={entry.kind}",
        f"CMD: {entry.description}",
        "---",
        entry.output if entry.output.strip() else "(no output)",
        "",
    ]
    return "\n".join(lines)


@dataclass
class TerminalHistoryStore:
    """Monotonic-id log with bundling cursor for user-kind entries."""

    _entries: list[TerminalHistoryEntry] = field(default_factory=list)
    _next_id: int = 1
    _last_bundled_user_id: int = 0
    # Optional path for debug logging; set to non-None to enable file writes.
    log_path: Path | None = None

    def _write_log(self, entry: TerminalHistoryEntry) -> None:
        """Append *entry* to the debug log file if logging is enabled."""
        if self.log_path is None:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(_format_log_entry(entry))
        except OSError:
            pass  # never crash the app over a debug log write

    def append_user(self, command: str, output: str) -> int:
        cmd = (command or "").strip() or "(empty command)"
        out = output or ""
        entry = TerminalHistoryEntry(
            id=self._next_id,
            timestamp=_utc_now_iso(),
            kind="user",
            description=cmd,
            output=out,
        )
        self._entries.append(entry)
        self._next_id += 1
        self._write_log(entry)
        return entry.id

    def append_tool(
        self,
        tool_name: str,
        arguments: dict,
        command_label: str,
        output: str,
    ) -> int:
        params = json.dumps(arguments or {}, sort_keys=True, default=str)
        if len(params) > 400:
            params = params[:400] + "…"
        label = (command_label or "").strip() or tool_name
        description = f"{tool_name}: {label} | {params}"
        entry = TerminalHistoryEntry(
            id=self._next_id,
            timestamp=_utc_now_iso(),
            kind="tool",
            description=description,
            output=output or "",
        )
        self._entries.append(entry)
        self._next_id += 1
        self._write_log(entry)
        return entry.id

    def clear(self) -> None:
        self._entries.clear()
        self._next_id = 1
        self._last_bundled_user_id = 0

    def format_bundle_for_next_user_turn(self) -> str:
        """Build text for user-kind entries not yet bundled; advance bundle cursor."""
        pending = [e for e in self._entries if e.kind == "user" and e.id > self._last_bundled_user_id]
        if not pending:
            return ""

        chunks: list[str] = [USER_TERMINAL_BUNDLE_HEADER]
        budget = MAX_TERMINAL_USER_MESSAGE_CHARS - len(USER_TERMINAL_BUNDLE_HEADER) - 200
        max_included_id = self._last_bundled_user_id

        for entry in pending:
            body = (entry.output or "").strip() or "(no output)"
            chunk = f"\n\n$ {entry.description}\n\n{body}"
            if len(chunk) > _PER_ENTRY_CAP:
                chunk = chunk[: _PER_ENTRY_CAP - 40].rstrip() + "\n... [truncated]"
            if len(chunk) > budget:
                break
            chunks.append(chunk)
            budget -= len(chunk)
            max_included_id = entry.id

        # Could not fit a full entry: include one hard-truncated block so we still advance.
        if max_included_id == self._last_bundled_user_id and pending:
            entry = pending[0]
            body = (entry.output or "").strip() or "(no output)"
            chunk = f"\n\n$ {entry.description}\n\n{body}"
            avail = MAX_TERMINAL_USER_MESSAGE_CHARS - len(USER_TERMINAL_BUNDLE_HEADER) - 80
            if len(chunk) > avail:
                chunk = chunk[: max(60, avail - 30)].rstrip() + "\n... [truncated]"
            chunks.append(chunk)
            max_included_id = entry.id

        self._last_bundled_user_id = max_included_id
        return "".join(chunks).strip()

    def summary_text(self) -> str:
        """id, kind, timestamp, description — no output."""
        if not self._entries:
            return "(no terminal history yet)"
        lines: list[str] = []
        slice_entries = self._entries[-_SUMMARY_MAX_ENTRIES:]
        if len(self._entries) > _SUMMARY_MAX_ENTRIES:
            lines.append(f"(showing last {_SUMMARY_MAX_ENTRIES} of {len(self._entries)} entries)\n")
        for e in slice_entries:
            lines.append(
                f"id={e.id} kind={e.kind} time={e.timestamp}\n  {e.description}\n"
            )
        return "\n".join(lines).rstrip()

    def fetch_full(self, entry_id: int) -> str:
        for e in self._entries:
            if e.id == entry_id:
                out = e.output or "(no output)"
                return (
                    f"id={e.id}\nkind={e.kind}\ntime={e.timestamp}\n"
                    f"description:\n{e.description}\n\noutput:\n{out}"
                )
        return f"[error] No terminal history entry with id={entry_id}."

    def latest_full(self) -> str:
        if not self._entries:
            return "(no terminal history yet)"
        e = self._entries[-1]
        return self.fetch_full(e.id)
