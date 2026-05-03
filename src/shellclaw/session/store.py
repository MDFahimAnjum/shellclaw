"""SQLite session storage.

Stores a record of every diagnostic session: the problem description,
commands run, solution applied, and user-confirmed outcome.

Uses only the Python standard library (sqlite3) — no ORM.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..config import DATA_DIR

DB_PATH = DATA_DIR / "sessions.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    problem     TEXT NOT NULL,
    tags        TEXT DEFAULT '',
    closed_at   TEXT,
    outcome     TEXT
);

CREATE TABLE IF NOT EXISTS commands (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id),
    ran_at      TEXT NOT NULL,
    command     TEXT NOT NULL,
    output      TEXT,
    exit_code   INTEGER,
    cwd         TEXT
);

CREATE TABLE IF NOT EXISTS solution_steps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id),
    step_order  INTEGER NOT NULL,
    command     TEXT NOT NULL,
    description TEXT,
    approved    INTEGER DEFAULT 0,
    executed    INTEGER DEFAULT 0
);
"""


@dataclass
class SessionRecord:
    id: int
    started_at: str
    problem: str
    tags: str
    closed_at: str | None
    outcome: str | None


@dataclass
class CommandRecord:
    id: int
    session_id: int
    ran_at: str
    command: str
    output: str | None


@contextmanager
def _db(path: Path = DB_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate_commands_metadata(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(commands)").fetchall()}
    if "exit_code" not in cols:
        conn.execute("ALTER TABLE commands ADD COLUMN exit_code INTEGER")
    if "cwd" not in cols:
        conn.execute("ALTER TABLE commands ADD COLUMN cwd TEXT")


def _ensure_schema() -> None:
    with _db() as conn:
        conn.executescript(SCHEMA)
        _migrate_commands_metadata(conn)


class SessionStore:
    """High-level interface for reading and writing session history."""

    def __init__(self) -> None:
        _ensure_schema()

    def create_session(self, problem: str) -> int:
        """Open a new session record. Returns the session ID."""
        with _db() as conn:
            cur = conn.execute(
                "INSERT INTO sessions (started_at, problem) VALUES (?, ?)",
                (datetime.now().isoformat(), problem),
            )
            return cur.lastrowid

    def add_command(
        self,
        session_id: int,
        command: str,
        output: str,
        *,
        exit_code: int | None = None,
        cwd: str | None = None,
    ) -> None:
        with _db() as conn:
            conn.execute(
                "INSERT INTO commands (session_id, ran_at, command, output, exit_code, cwd) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, datetime.now().isoformat(), command, output, exit_code, cwd),
            )

    def add_solution_step(
        self,
        session_id: int,
        order: int,
        command: str,
        description: str,
    ) -> None:
        with _db() as conn:
            conn.execute(
                "INSERT INTO solution_steps (session_id, step_order, command, description) "
                "VALUES (?, ?, ?, ?)",
                (session_id, order, command, description),
            )

    def mark_step_executed(self, session_id: int, command: str) -> None:
        with _db() as conn:
            conn.execute(
                "UPDATE solution_steps SET executed=1 WHERE session_id=? AND command=?",
                (session_id, command),
            )

    def close_session(self, session_id: int, outcome: str, tags: str = "") -> None:
        """Record the outcome of a session (e.g. 'resolved', 'rejected', 'abandoned')."""
        with _db() as conn:
            conn.execute(
                "UPDATE sessions SET closed_at=?, outcome=?, tags=? WHERE id=?",
                (datetime.now().isoformat(), outcome, tags, session_id),
            )

    def recent_sessions(self, limit: int = 20) -> list[SessionRecord]:
        with _db() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_session(r) for r in rows]

    def search_sessions(self, query: str) -> list[SessionRecord]:
        with _db() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE problem LIKE ? OR tags LIKE ? "
                "ORDER BY started_at DESC LIMIT 50",
                (f"%{query}%", f"%{query}%"),
            ).fetchall()
        return [_row_to_session(r) for r in rows]

    def get_commands_for_session(self, session_id: int) -> list[CommandRecord]:
        with _db() as conn:
            rows = conn.execute(
                "SELECT * FROM commands WHERE session_id=? ORDER BY ran_at",
                (session_id,),
            ).fetchall()
        return [_row_to_command(r) for r in rows]


def _row_to_session(row: sqlite3.Row) -> SessionRecord:
    return SessionRecord(
        id=row["id"],
        started_at=row["started_at"],
        problem=row["problem"],
        tags=row["tags"] or "",
        closed_at=row["closed_at"],
        outcome=row["outcome"],
    )


def _row_to_command(row: sqlite3.Row) -> CommandRecord:
    return CommandRecord(
        id=row["id"],
        session_id=row["session_id"],
        ran_at=row["ran_at"],
        command=row["command"],
        output=row["output"],
    )
