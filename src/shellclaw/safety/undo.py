"""Undo log with automatic file backups.

Before any file is modified, a backup is taken automatically.
Before any service state is changed, the previous state is recorded.
Deletions are logged as irreversible — no false promises.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path

from ..config import BACKUP_DIR, DATA_DIR

UNDO_LOG_PATH = DATA_DIR / "undo_log.json"


class ActionKind(str, Enum):
    FILE_WRITE = "file_write"
    FILE_DELETE = "file_delete"
    SERVICE_CHANGE = "service_change"
    COMMAND = "command"


@dataclass
class UndoEntry:
    kind: str
    timestamp: str
    description: str
    undo_command: str = ""
    backup_path: str = ""
    original_path: str = ""
    reversible: bool = True


@dataclass
class UndoLog:
    entries: list[UndoEntry] = field(default_factory=list)

    @classmethod
    def load(cls) -> "UndoLog":
        if not UNDO_LOG_PATH.exists():
            return cls()
        try:
            data = json.loads(UNDO_LOG_PATH.read_text())
            entries = [UndoEntry(**e) for e in data.get("entries", [])]
            return cls(entries=entries)
        except (json.JSONDecodeError, TypeError):
            return cls()

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        UNDO_LOG_PATH.write_text(
            json.dumps({"entries": [asdict(e) for e in self.entries]}, indent=2)
        )

    def record_file_write(self, path: str) -> str | None:
        """Back up a file before writing it. Returns backup path or None."""
        p = Path(path)
        if not p.exists():
            return None

        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"{timestamp}-{p.name}"
        backup_path = BACKUP_DIR / backup_name
        shutil.copy2(p, backup_path)

        entry = UndoEntry(
            kind=ActionKind.FILE_WRITE,
            timestamp=datetime.now().isoformat(),
            description=f"Modified {path}",
            backup_path=str(backup_path),
            original_path=str(path),
            reversible=True,
        )
        self.entries.append(entry)
        self.save()
        return str(backup_path)

    def record_file_delete(self, path: str) -> None:
        entry = UndoEntry(
            kind=ActionKind.FILE_DELETE,
            timestamp=datetime.now().isoformat(),
            description=f"Deleted {path}",
            original_path=str(path),
            reversible=False,
        )
        self.entries.append(entry)
        self.save()

    def record_service_change(self, unit: str, prev_enabled: str, prev_active: str) -> None:
        undo_cmd = _service_undo_command(unit, prev_enabled, prev_active)
        entry = UndoEntry(
            kind=ActionKind.SERVICE_CHANGE,
            timestamp=datetime.now().isoformat(),
            description=f"Changed service state: {unit}",
            undo_command=undo_cmd,
            reversible=bool(undo_cmd),
        )
        self.entries.append(entry)
        self.save()

    def record_command(self, command: str) -> None:
        """Log an executed write command (no automatic undo available)."""
        entry = UndoEntry(
            kind=ActionKind.COMMAND,
            timestamp=datetime.now().isoformat(),
            description=f"Ran: {command}",
            reversible=False,
        )
        self.entries.append(entry)
        self.save()

    def last_reversible(self) -> UndoEntry | None:
        for entry in reversed(self.entries):
            if entry.reversible:
                return entry
        return None

    def undo_last(self) -> str:
        """Reverse the last reversible action. Returns a status message."""
        entry = self.last_reversible()
        if entry is None:
            return "Nothing reversible to undo."

        if entry.kind == ActionKind.FILE_WRITE and entry.backup_path:
            backup = Path(entry.backup_path)
            if not backup.exists():
                return f"Backup file not found: {entry.backup_path}"
            shutil.copy2(backup, entry.original_path)
            entry.reversible = False
            self.save()
            return f"Restored {entry.original_path} from backup."

        if entry.kind == ActionKind.SERVICE_CHANGE and entry.undo_command:
            result = subprocess.run(
                entry.undo_command.split(),
                capture_output=True,
                text=True,
            )
            entry.reversible = False
            self.save()
            if result.returncode == 0:
                return f"Undone: {entry.undo_command}"
            return f"Undo command failed: {result.stderr.strip()}"

        return "Cannot undo this action."

    def prune_old_backups(self, retention_days: int = 30) -> int:
        """Remove backups older than retention_days. Returns count removed."""
        cutoff = datetime.now() - timedelta(days=retention_days)
        removed = 0
        kept: list[UndoEntry] = []

        for entry in self.entries:
            try:
                entry_time = datetime.fromisoformat(entry.timestamp)
            except ValueError:
                kept.append(entry)
                continue

            if entry_time < cutoff:
                if entry.backup_path:
                    Path(entry.backup_path).unlink(missing_ok=True)
                removed += 1
            else:
                kept.append(entry)

        self.entries = kept
        self.save()
        return removed


def _service_undo_command(unit: str, prev_enabled: str, prev_active: str) -> str:
    """Return the systemctl command that restores a service to its previous state."""
    if "disabled" in prev_enabled:
        return f"systemctl disable {unit}"
    if "enabled" in prev_enabled:
        return f"systemctl enable {unit}"
    return ""


def get_service_state(unit: str) -> tuple[str, str]:
    """Return (enabled_state, active_state) for a systemd unit."""
    enabled = subprocess.run(
        ["systemctl", "is-enabled", unit],
        capture_output=True, text=True,
    ).stdout.strip()
    active = subprocess.run(
        ["systemctl", "is-active", unit],
        capture_output=True, text=True,
    ).stdout.strip()
    return enabled, active
