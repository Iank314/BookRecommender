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
from typing import Literal

from server.models.book import Books
from server.storage._base import SQLiteStore, row_to_book

FeedbackKind = Literal["up", "down"]


class FeedbackStore(SQLiteStore):
    """Thread-safe SQLite feedback store. One row per (user_id, book_id)."""

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
        return [(row_to_book(r), r["kind"]) for r in rows]

    def ids(self, user_id: str, kind: FeedbackKind) -> list[str]:
        """Book IDs of one feedback kind, without the payloads. (The app now
        derives cache-signature IDs from all(); kept as a cheap query path.)"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT book_id FROM feedback_entries WHERE user_id = ? AND kind = ?",
                (user_id, kind),
            ).fetchall()
        return [r["book_id"] for r in rows]
