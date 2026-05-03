"""Tests for the undo log and backup system."""

import json
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest


@pytest.fixture
def tmp_dirs(tmp_path, monkeypatch):
    """Redirect all undo log paths to a temporary directory."""
    data_dir = tmp_path / "shellclaw"
    backup_dir = data_dir / "backups"
    data_dir.mkdir()
    backup_dir.mkdir()

    import shellclaw.safety.undo as undo_module
    monkeypatch.setattr(undo_module, "DATA_DIR", data_dir)
    monkeypatch.setattr(undo_module, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(undo_module, "UNDO_LOG_PATH", data_dir / "undo_log.json")

    return data_dir, backup_dir


class TestUndoLog:
    def test_empty_log_no_reversible(self, tmp_dirs):
        from shellclaw.safety.undo import UndoLog
        log = UndoLog()
        assert log.last_reversible() is None

    def test_record_file_write_creates_backup(self, tmp_dirs, tmp_path):
        data_dir, backup_dir = tmp_dirs
        from shellclaw.safety.undo import UndoLog, ActionKind

        # Create a source file to back up
        source_file = tmp_path / "test_config.txt"
        source_file.write_text("original content")

        log = UndoLog()
        backup_path = log.record_file_write(str(source_file))

        assert backup_path is not None
        assert Path(backup_path).exists()
        assert Path(backup_path).read_text() == "original content"

    def test_record_file_write_nonexistent_returns_none(self, tmp_dirs):
        from shellclaw.safety.undo import UndoLog
        log = UndoLog()
        result = log.record_file_write("/nonexistent/path/file.txt")
        assert result is None

    def test_file_write_entry_is_reversible(self, tmp_dirs, tmp_path):
        from shellclaw.safety.undo import UndoLog
        source_file = tmp_path / "config.txt"
        source_file.write_text("data")

        log = UndoLog()
        log.record_file_write(str(source_file))

        entry = log.last_reversible()
        assert entry is not None
        assert entry.reversible is True

    def test_undo_last_restores_file(self, tmp_dirs, tmp_path):
        from shellclaw.safety.undo import UndoLog
        source_file = tmp_path / "config.txt"
        source_file.write_text("original")

        log = UndoLog()
        log.record_file_write(str(source_file))

        # Now modify the file
        source_file.write_text("modified")
        assert source_file.read_text() == "modified"

        result = log.undo_last()
        assert "Restored" in result
        assert source_file.read_text() == "original"

    def test_record_file_delete_is_irreversible(self, tmp_dirs):
        from shellclaw.safety.undo import UndoLog
        log = UndoLog()
        log.record_file_delete("/some/deleted/file.txt")
        assert log.last_reversible() is None

    def test_record_command_is_irreversible(self, tmp_dirs):
        from shellclaw.safety.undo import UndoLog
        log = UndoLog()
        log.record_command("journalctl --vacuum-size=500M")
        assert log.last_reversible() is None

    def test_prune_old_backups(self, tmp_dirs, tmp_path):
        from shellclaw.safety.undo import UndoLog, UndoEntry, ActionKind
        data_dir, backup_dir = tmp_dirs

        # Create a fake backup file
        old_backup = backup_dir / "old_backup.txt"
        old_backup.write_text("old")

        log = UndoLog()
        old_entry = UndoEntry(
            kind=ActionKind.FILE_WRITE,
            timestamp=(datetime.now() - timedelta(days=60)).isoformat(),
            description="Old backup",
            backup_path=str(old_backup),
            original_path="/etc/old.conf",
            reversible=True,
        )
        log.entries.append(old_entry)
        log.save()

        removed = log.prune_old_backups(retention_days=30)
        assert removed == 1
        assert not old_backup.exists()
        assert len(log.entries) == 0

    def test_save_and_load_roundtrip(self, tmp_dirs, tmp_path):
        from shellclaw.safety.undo import UndoLog
        source_file = tmp_path / "file.txt"
        source_file.write_text("data")

        log = UndoLog()
        log.record_file_write(str(source_file))
        log.record_command("apt clean")
        log.save()

        loaded = UndoLog.load()
        assert len(loaded.entries) == 2

    def test_undo_nothing_returns_message(self, tmp_dirs):
        from shellclaw.safety.undo import UndoLog
        log = UndoLog()
        result = log.undo_last()
        assert "Nothing" in result or "nothing" in result.lower()
