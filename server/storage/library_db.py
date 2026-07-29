"""SQLite-backed, per-user library store."""

from __future__ import annotations

import json
import sqlite3

from server.models.book import Books
from server.storage._base import SQLiteStore, row_to_book


class SectionNameTakenError(Exception):
    """A section with this name already exists for the user."""


class LibraryStore(SQLiteStore):
    """Thread-safe SQLite library store. One row per (user_id, book_id)."""

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
        reading_status TEXT,  -- 'want_to_read' | 'reading' | 'read' | NULL
        PRIMARY KEY (user_id, book_id)
    );
    CREATE INDEX IF NOT EXISTS idx_library_user_added
        ON library_entries(user_id, added_at DESC);

    -- User-defined shelves within a library. Membership is many-to-many so one
    -- book can sit in several sections and "recommend from these picked books"
    -- is just an unsaved membership set.
    CREATE TABLE IF NOT EXISTS library_sections (
        section_id  INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     TEXT NOT NULL,
        name        TEXT NOT NULL,
        created_at  INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
        UNIQUE (user_id, name)
    );
    CREATE TABLE IF NOT EXISTS section_books (
        section_id  INTEGER NOT NULL,
        user_id     TEXT NOT NULL,
        book_id     TEXT NOT NULL,
        added_at    INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
        PRIMARY KEY (section_id, book_id)
    );
    CREATE INDEX IF NOT EXISTS idx_section_books_user_book
        ON section_books(user_id, book_id);
    """

    def _migrate(self, conn: sqlite3.Connection) -> None:
        # Migration for DBs created before reading status existed —
        # CREATE TABLE IF NOT EXISTS won't add columns to an old table.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(library_entries)")}
        if "reading_status" not in cols:
            conn.execute(
                "ALTER TABLE library_entries ADD COLUMN reading_status TEXT"
            )

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
            # A book leaving the library also leaves every section it was in.
            conn.execute(
                "DELETE FROM section_books WHERE user_id = ? AND book_id = ?",
                (user_id, book_id),
            )
            return cur.rowcount > 0

    def get(self, user_id: str, book_id: str) -> Books | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM library_entries WHERE user_id = ? AND book_id = ?",
                (user_id, book_id),
            ).fetchone()
        return row_to_book(row) if row else None

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
        return [row_to_book(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Reading status — want_to_read / reading / read (or unset), one per
    # saved book. Exclusive by nature, so it's a column rather than a
    # section: a book can sit in many sections but has one status.
    # ------------------------------------------------------------------ #
    def set_status(self, user_id: str, book_id: str, status: str | None) -> bool:
        """Set or clear (None) a saved book's reading status. False when the
        book isn't in the user's library."""
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE library_entries SET reading_status = ?
                WHERE user_id = ? AND book_id = ?
                """,
                (status, user_id, book_id),
            )
            return cur.rowcount > 0

    def statuses(self, user_id: str) -> dict[str, str]:
        """{book_id: status} for every saved book with a status set."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT book_id, reading_status FROM library_entries
                WHERE user_id = ? AND reading_status IS NOT NULL
                """,
                (user_id,),
            ).fetchall()
        return {r["book_id"]: r["reading_status"] for r in rows}

    # ------------------------------------------------------------------ #
    # Sections — user-defined shelves, many-to-many with library books
    # ------------------------------------------------------------------ #
    def create_section(self, user_id: str, name: str) -> dict:
        """Create a section; returns {"id", "name", "book_ids"}.

        Raises SectionNameTakenError on a duplicate name for this user.
        """
        with self._connect() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO library_sections (user_id, name) VALUES (?, ?)",
                    (user_id, name),
                )
            except sqlite3.IntegrityError as exc:
                raise SectionNameTakenError(name) from exc
            return {"id": cur.lastrowid, "name": name, "book_ids": []}

    def rename_section(self, user_id: str, section_id: int, name: str) -> bool:
        """Rename a section. False if the section isn't the user's; raises
        SectionNameTakenError if the new name collides."""
        with self._connect() as conn:
            try:
                cur = conn.execute(
                    """
                    UPDATE library_sections SET name = ?
                    WHERE section_id = ? AND user_id = ?
                    """,
                    (name, section_id, user_id),
                )
            except sqlite3.IntegrityError as exc:
                raise SectionNameTakenError(name) from exc
            return cur.rowcount > 0

    def delete_section(self, user_id: str, section_id: int) -> bool:
        """Delete a section and its memberships (books stay in the library)."""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM library_sections WHERE section_id = ? AND user_id = ?",
                (section_id, user_id),
            )
            if cur.rowcount == 0:
                return False
            conn.execute(
                "DELETE FROM section_books WHERE section_id = ? AND user_id = ?",
                (section_id, user_id),
            )
            return True

    def sections(self, user_id: str) -> list[dict]:
        """All of a user's sections, oldest first, each with its member IDs."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT section_id, name FROM library_sections
                WHERE user_id = ?
                ORDER BY created_at, section_id
                """,
                (user_id,),
            ).fetchall()
            members = conn.execute(
                """
                SELECT section_id, book_id FROM section_books
                WHERE user_id = ?
                ORDER BY added_at, book_id
                """,
                (user_id,),
            ).fetchall()
        by_section: dict[int, list[str]] = {}
        for m in members:
            by_section.setdefault(m["section_id"], []).append(m["book_id"])
        return [
            {"id": r["section_id"], "name": r["name"],
             "book_ids": by_section.get(r["section_id"], [])}
            for r in rows
        ]

    def add_to_section(self, user_id: str, section_id: int, book_id: str) -> bool:
        """Put a saved book in a section. False when the section isn't the
        user's or the book isn't in their library (no orphan memberships)."""
        with self._connect() as conn:
            owns_section = conn.execute(
                "SELECT 1 FROM library_sections WHERE section_id = ? AND user_id = ?",
                (section_id, user_id),
            ).fetchone()
            owns_book = conn.execute(
                "SELECT 1 FROM library_entries WHERE user_id = ? AND book_id = ?",
                (user_id, book_id),
            ).fetchone()
            if not owns_section or not owns_book:
                return False
            conn.execute(
                """
                INSERT OR IGNORE INTO section_books (section_id, user_id, book_id)
                VALUES (?, ?, ?)
                """,
                (section_id, user_id, book_id),
            )
            return True

    def move_between_sections(
        self, user_id: str, book_id: str,
        from_section_id: int, to_section_id: int,
    ) -> bool:
        """Move a book from one section to another in a single transaction —
        it can't end up in both (or neither) on a partial failure. False when
        either section isn't the user's or the book isn't in the source."""
        if from_section_id == to_section_id:
            return False
        with self._connect() as conn:
            owns_target = conn.execute(
                "SELECT 1 FROM library_sections WHERE section_id = ? AND user_id = ?",
                (to_section_id, user_id),
            ).fetchone()
            if not owns_target:
                return False
            cur = conn.execute(
                """
                DELETE FROM section_books
                WHERE section_id = ? AND user_id = ? AND book_id = ?
                """,
                (from_section_id, user_id, book_id),
            )
            if cur.rowcount == 0:
                return False  # not in the source section (or foreign section)
            conn.execute(
                """
                INSERT OR IGNORE INTO section_books (section_id, user_id, book_id)
                VALUES (?, ?, ?)
                """,
                (to_section_id, user_id, book_id),
            )
            return True

    def remove_from_section(self, user_id: str, section_id: int, book_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                """
                DELETE FROM section_books
                WHERE section_id = ? AND user_id = ? AND book_id = ?
                """,
                (section_id, user_id, book_id),
            )
            return cur.rowcount > 0

    def section_books(self, user_id: str, section_id: int) -> list[Books] | None:
        """Books in one section, oldest membership first. None when the
        section doesn't exist for this user (distinct from an empty section)."""
        with self._connect() as conn:
            owns = conn.execute(
                "SELECT 1 FROM library_sections WHERE section_id = ? AND user_id = ?",
                (section_id, user_id),
            ).fetchone()
            if not owns:
                return None
            rows = conn.execute(
                """
                SELECT le.* FROM section_books sb
                JOIN library_entries le
                  ON le.user_id = sb.user_id AND le.book_id = sb.book_id
                WHERE sb.section_id = ? AND sb.user_id = ?
                ORDER BY sb.added_at, sb.book_id
                """,
                (section_id, user_id),
            ).fetchall()
        return [row_to_book(r) for r in rows]
