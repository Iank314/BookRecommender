"""Tests for the reading-status column on LibraryStore."""

from pathlib import Path

import pytest

from server.models.book import Books
from server.storage.library_db import LibraryStore


@pytest.fixture
def store(tmp_path: Path) -> LibraryStore:
    return LibraryStore(db_path=tmp_path / "library_test.db")


def _book(book_id: str, title: str = "T") -> Books:
    return Books(
        id=book_id, title=title, authors=["A"],
        description="d", tags=[], metadata={},
    )


def test_set_and_read_status(store: LibraryStore):
    store.add("u1", _book("b1"))
    assert store.set_status("u1", "b1", "reading") is True
    assert store.statuses("u1") == {"b1": "reading"}


def test_clear_status(store: LibraryStore):
    store.add("u1", _book("b1"))
    store.set_status("u1", "b1", "read")
    assert store.set_status("u1", "b1", None) is True
    assert store.statuses("u1") == {}


def test_status_requires_saved_book(store: LibraryStore):
    assert store.set_status("u1", "ghost", "read") is False


def test_status_is_per_user(store: LibraryStore):
    store.add("u1", _book("b1"))
    store.add("u2", _book("b1"))
    store.set_status("u1", "b1", "read")
    assert store.statuses("u2") == {}


def test_resave_preserves_status(store: LibraryStore):
    # The recommend pipeline re-persists enriched books via add() — that
    # upsert must not wipe the user's reading status.
    store.add("u1", _book("b1", title="Old Title"))
    store.set_status("u1", "b1", "want_to_read")
    store.add("u1", _book("b1", title="Enriched Title"))
    assert store.statuses("u1") == {"b1": "want_to_read"}
    assert store.get("u1", "b1").title == "Enriched Title"


def test_schema_migration_adds_column(tmp_path: Path):
    # Simulate a pre-status database: build the old table shape by hand, then
    # let LibraryStore's migration add the column without losing rows.
    import sqlite3
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE library_entries (
            user_id     TEXT NOT NULL,
            book_id     TEXT NOT NULL,
            title       TEXT NOT NULL,
            authors     TEXT NOT NULL DEFAULT '[]',
            description TEXT NOT NULL DEFAULT '',
            tags        TEXT NOT NULL DEFAULT '[]',
            metadata    TEXT NOT NULL DEFAULT '{}',
            added_at    INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
            PRIMARY KEY (user_id, book_id)
        )
        """
    )
    conn.execute(
        "INSERT INTO library_entries (user_id, book_id, title) VALUES ('u1', 'b1', 'T')"
    )
    conn.commit()
    conn.close()

    store = LibraryStore(db_path=db)
    assert [b.id for b in store.all("u1")] == ["b1"]
    assert store.set_status("u1", "b1", "read") is True
    assert store.statuses("u1") == {"b1": "read"}
