"""SQLite-backed, per-user feedback store (thumbs up / thumbs down).

Feedback is independent of the saved library: saving a book records "this is
in my collection", liking/disliking records "use this as a signal when
recommending." A book can be saved AND liked, or saved AND disliked — the
recommender treats them as separate inputs.

One row per (user_id, book_id): a new opinion overwrites the prior one
(e.g. flipping a thumbs-down to a thumbs-up), so a user can change their
mind without leaving stale entries behind.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Literal

from server.models.book import Books

FeedbackKind = Literal["up", "down"]

_DEFAULT_DB_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / "library.db"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback_entries (
    user_id     TEXT NOT NULL,
    book_id     TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('up', 'down')),
    title       TEXT NOT NULL,
    authors     TEXT NOT NULL DEFAULT '[]',
    description TEXT NOT NULL DEFAULT '',
    tags        TEXT NOT NULL DEFAULT '[]',
    metadata    TEXT NOT NULL DEFAULT '{}',
    added_at    INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
    PRIMARY KEY (user_id, book_id)
);
CREATE INDEX IF NOT EXISTS idx_feedback_user_kind_added
    ON feedback_entries(user_id, kind, added_at DESC);
"""


def _resolve_db_path() -> Path:
    env = os.environ.get("BOOKREC_DB_PATH")
    return Path(env) if env else _DEFAULT_DB_PATH


class FeedbackStore:
    """Thread-safe SQLite feedback store. One row per (user_id, book_id)."""

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

    def set(self, user_id: str, book: Books, kind: FeedbackKind) -> None:
        """Record or update feedback for a book. Overwrites any prior opinion."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO feedback_entries
                    (user_id, book_id, kind, title, authors, description, tags, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, book_id) DO UPDATE SET
                    kind        = excluded.kind,
                    title       = excluded.title,
                    authors     = excluded.authors,
                    description = excluded.description,
                    tags        = excluded.tags,
                    metadata    = excluded.metadata,
                    added_at    = strftime('%s', 'now')
                """,
                (
                    user_id,
                    book.id,
                    kind,
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
                "DELETE FROM feedback_entries WHERE user_id = ? AND book_id = ?",
                (user_id, book_id),
            )
            return cur.rowcount > 0

    def kind_for(self, user_id: str, book_id: str) -> FeedbackKind | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT kind FROM feedback_entries WHERE user_id = ? AND book_id = ?",
                (user_id, book_id),
            ).fetchone()
        return row["kind"] if row else None

    def all(
        self, user_id: str, kind: FeedbackKind | None = None,
    ) -> list[tuple[Books, FeedbackKind]]:
        """Return (book, kind) tuples for the user, newest first.

        Pass `kind` to restrict to one side; omit it to get everything.
        """
        query = "SELECT * FROM feedback_entries WHERE user_id = ?"
        params: tuple = (user_id,)
        if kind is not None:
            query += " AND kind = ?"
            params = (user_id, kind)
        query += " ORDER BY added_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [(_row_to_book(r), r["kind"]) for r in rows]

    def ids(self, user_id: str, kind: FeedbackKind) -> list[str]:
        """Cheap path for cache-signature use: just the book IDs of one kind."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT book_id FROM feedback_entries WHERE user_id = ? AND kind = ?",
                (user_id, kind),
            ).fetchall()
        return [r["book_id"] for r in rows]


def _row_to_book(row: sqlite3.Row) -> Books:
    return Books(
        id=row["book_id"],
        title=row["title"],
        authors=json.loads(row["authors"]),
        description=row["description"],
        tags=json.loads(row["tags"]),
        metadata=json.loads(row["metadata"]),
    )
