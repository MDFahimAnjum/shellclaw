"""Mutable command name set for terminal input highlighting and completion.

Layers: :data:`ALLOWED_COMMANDS` (aligns with ``run_safe``), bundled tldr keys,
and PATH executables merged asynchronously after mount.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterable

from ..safety.sandbox import ALLOWED_COMMANDS
from ..wiki.tldr import all_tldr_command_names


def scan_path_executables() -> set[str]:
    """Return basenames of regular executable files on ``PATH`` (no ``shutil.which`` per file)."""
    found: set[str] = set()
    raw = os.environ.get("PATH", "")
    for dirpath in raw.split(os.pathsep):
        if not dirpath or not os.path.isdir(dirpath):
            continue
        try:
            with os.scandir(dirpath) as it:
                for ent in it:
                    if ent.is_dir(follow_symlinks=False):
                        continue
                    try:
                        if os.access(ent.path, os.X_OK):
                            found.add(ent.name)
                    except OSError:
                        continue
        except OSError:
            continue
    return found


class CommandDatabase:
    """Thread-safe union of allowlist, tldr, and PATH command names."""

    __slots__ = ("_lock", "_names")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._names: set[str] = set(ALLOWED_COMMANDS) | set(all_tldr_command_names())

    def merge_path(self, names: Iterable[str]) -> None:
        with self._lock:
            self._names.update(names)

    def snapshot(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._names)

    def __contains__(self, name: str) -> bool:
        with self._lock:
            return name in self._names

    def completion_candidates(self, prefix: str) -> list[str]:
        """Sorted command names that start with ``prefix`` (case-insensitive)."""
        if not prefix:
            return []
        p = prefix.casefold()
        with self._lock:
            matches = [n for n in self._names if n.casefold().startswith(p)]
        matches.sort(key=str.casefold)
        return matches
