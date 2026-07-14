"""SQLite access layer for Quinn.

Thin, dependency-free wrapper around the stdlib ``sqlite3`` module. The rest of
the codebase talks to the database through :func:`get_connection` and
:func:`init_db` so the storage details stay in one place.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Repo layout: <root>/quinn/db.py  ->  <root>/data/quinn.db
ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "quinn.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
RUNTIME_SCHEMA_PATH = Path(__file__).resolve().parent / "schema_runtime.sql"


def get_connection(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Return a connection with sane defaults.

    ``row_factory`` is set to :class:`sqlite3.Row` so callers get dict-like
    rows, and foreign keys are enforced (off by default in SQLite).
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    # Default is 0 (fail instantly on a locked DB). The outbox/claim protocol is
    # written for concurrent workers; give writers up to 5s to win the lock
    # instead of raising 'database is locked' the moment two runs overlap.
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables from the schema files if they don't already exist.

    Applies the data-layer schema (schema.sql) and the orchestration/runtime
    schema (schema_runtime.sql). Both are idempotent (CREATE ... IF NOT EXISTS),
    so calling init_db repeatedly is safe.
    """
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    if RUNTIME_SCHEMA_PATH.exists():
        conn.executescript(RUNTIME_SCHEMA_PATH.read_text(encoding="utf-8"))
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing DB up to the current schema.

    CREATE IF NOT EXISTS can't add columns to a table that already exists, so
    columns added after first ship are ALTERed in here. Each ALTER is a no-op
    (caught) when the column is already present — init_db stays idempotent.
    """
    for col in ("system_prompt", "user_prompt", "response_text"):
        try:
            conn.execute(f"ALTER TABLE llm_calls ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
