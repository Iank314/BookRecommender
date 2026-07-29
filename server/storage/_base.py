"""Shared SQLite scaffolding for the storage stores.

Every store keeps its table family in the same library.db file, and each one
used to carry its own copy of the path resolution, connection contextmanager,
and WAL/synchronous schema prologue. One copy lives here instead — which also
means the planned Postgres port (see README) has one connection layer to
rewrite, not four.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from server.models.book import Books

_DEFAULT_DB_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / "library.db"
)


def resolve_db_path() -> Path:
    env = os.environ.get("BOOKREC_DB_PATH")
    return Path(env) if env else _DEFAULT_DB_PATH


def row_to_book(row: sqlite3.Row) -> Books:
    """Build a Books from a row — library_entries and feedback_entries share
    the same book-payload columns (JSON-encoded authors/tags/metadata)."""
    return Books(
        id=row["book_id"],
        title=row["title"],
        authors=json.loads(row["authors"]),
        description=row["description"],
        tags=json.loads(row["tags"]),
        metadata=json.loads(row["metadata"]),
    )


class SQLiteStore:
    """Base for the SQLite stores: db-path resolution, a row-factory
    connection contextmanager, and WAL schema init on construction.

    Subclasses set `_SCHEMA` (executed idempotently via CREATE ... IF NOT
    EXISTS) and may override `_migrate(conn)` for ALTER-TABLE migrations that
    CREATE TABLE IF NOT EXISTS can't express on pre-existing tables.
    """

    _SCHEMA: str = ""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else resolve_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.executescript(self._SCHEMA)
            self._migrate(conn)
            conn.commit()
        finally:
            conn.close()

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Hook for column migrations on databases created by older code."""
