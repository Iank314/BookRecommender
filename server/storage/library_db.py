"""SQLite-backed, per-user library store."""

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

_SCHEMA = """
CREATE TABLE IF NOT EXISTS library_entries (
    user_id     TEXT NOT NULL,
    book_id     TEXT NOT NULL,
    title       TEXT NOT NULL,
    authors     TEXT NOT NULL DEFAULT '[]',
    description TEXT NOT NULL DEFAULT '',
    tags        TEXT NOT NULL DEFAULT '[]',
    metadata    TEXT NOT NULL DEFAULT '{}',
    added_at    INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    PRIMARY KEY (user_id, book_id)
);
CREATE INDEX IF NOT EXISTS idx_library_user_added
    ON library_entries(user_id, added_at DESC);
"""


def _resolve_db_path() -> Path:
    env = os.environ.get("BOOKREC_DB_PATH")
    return Path(env) if env else _DEFAULT_DB_PATH


class LibraryStore:
    """Thread-safe SQLite library store. One row per (user_id, book_id)."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else _resolve_db_path()
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
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def add(self, user_id: str, book: Books) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO library_entries
                    (user_id, book_id, title, authors, description, tags, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, book_id) DO UPDATE SET
                    title       = excluded.title,
                    authors     = excluded.authors,
                    description = excluded.description,
                    tags        = excluded.tags,
                    metadata    = excluded.metadata
                """,
                (
                    user_id,
                    book.id,
                    book.title,
                    json.dumps(book.authors),
                    book.description,
                    json.dumps(book.tags),
                    json.dumps(book.metadata),
                ),
            )

    def remove(self, user_id: str, book_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM library_entries WHERE user_id = ? AND book_id = ?",
                (user_id, book_id),
            )
            return cur.rowcount > 0

    def get(self, user_id: str, book_id: str) -> Books | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM library_entries WHERE user_id = ? AND book_id = ?",
                (user_id, book_id),
            ).fetchone()
        return _row_to_book(row) if row else None

    def all(self, user_id: str) -> list[Books]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM library_entries
                WHERE user_id = ?
                ORDER BY added_at DESC
                """,
                (user_id,),
            ).fetchall()
        return [_row_to_book(r) for r in rows]


def _row_to_book(row: sqlite3.Row) -> Books:
    return Books(
        id=row["book_id"],
        title=row["title"],
        authors=json.loads(row["authors"]),
        description=row["description"],
        tags=json.loads(row["tags"]),
        metadata=json.loads(row["metadata"]),
    )
