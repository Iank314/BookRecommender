"""Tests for the core LibraryStore CRUD — saving a book is the single most
common user action, so its add/get/all/remove contract and the JSON payload
round-trip get pinned here directly (the section and reading-status suites only
exercise these incidentally). Section-cascade and status-preservation behaviour
live in test_sections.py / test_reading_status.py and aren't repeated."""

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


def test_add_then_get_returns_the_book(store: LibraryStore):
    store.add("u1", _book("b1", title="Dune"))
    got = store.get("u1", "b1")
    assert got is not None
    assert got.id == "b1"
    assert got.title == "Dune"


def test_get_missing_book_is_none(store: LibraryStore):
    store.add("u1", _book("b1"))
    assert store.get("u1", "ghost") is None
    assert store.get("nobody", "b1") is None


def test_full_payload_round_trips(store: LibraryStore):
    # The store JSON-encodes authors/tags/metadata; a regression in that
    # serialization would silently corrupt every saved book. Nothing else
    # asserts a fully-populated book survives a save -> load.
    original = Books(
        id="b1", title="A Book", authors=["X", "Y"],
        description="Some description.",
        tags=["fantasy", "adventure"],
        metadata={"language": "en", "edition_count": 12, "thumbnail": None},
    )
    store.add("u1", original)
    restored = store.get("u1", "b1")
    assert restored.id == "b1"
    assert restored.title == "A Book"
    assert restored.authors == ["X", "Y"]
    assert restored.description == "Some description."
    assert restored.tags == ["fantasy", "adventure"]
    assert restored.metadata == {"language": "en", "edition_count": 12, "thumbnail": None}


def test_add_upserts_on_repeat_save(store: LibraryStore):
    # Re-saving the same id (e.g. enrichment re-persisting a fuller record)
    # updates the stored fields in place rather than inserting a duplicate.
    store.add("u1", _book("b1", title="Sparse"))
    enriched = Books(
        id="b1", title="Enriched", authors=["New Author"],
        description="A much fuller description.",
        tags=["epic fantasy"], metadata={"language": "en"},
    )
    store.add("u1", enriched)
    assert len(store.all("u1")) == 1  # upsert, not a second row
    got = store.get("u1", "b1")
    assert got.title == "Enriched"
    assert got.authors == ["New Author"]
    assert got.tags == ["epic fantasy"]
    assert got.metadata == {"language": "en"}


def test_all_returns_every_saved_book(store: LibraryStore):
    store.add("u1", _book("b1"))
    store.add("u1", _book("b2"))
    store.add("u1", _book("b3"))
    # added_at has 1-second granularity, so the DESC sort order isn't stable
    # within a fast test run — assert membership, not exact order.
    assert {b.id for b in store.all("u1")} == {"b1", "b2", "b3"}


def test_all_unknown_user_is_empty(store: LibraryStore):
    assert store.all("nobody") == []


def test_remove_returns_true_only_when_something_was_there(store: LibraryStore):
    store.add("u1", _book("b1"))
    assert store.remove("u1", "b1") is True
    assert store.get("u1", "b1") is None
    assert store.remove("u1", "b1") is False  # already gone


def test_books_are_isolated_per_user(store: LibraryStore):
    store.add("u1", _book("b1", title="U1's copy"))
    store.add("u2", _book("b1", title="U2's copy"))
    # Same book id, two users — neither should see or affect the other's row.
    assert store.get("u1", "b1").title == "U1's copy"
    assert store.get("u2", "b1").title == "U2's copy"
    assert store.remove("u1", "b1") is True
    assert store.get("u1", "b1") is None
    assert store.get("u2", "b1").title == "U2's copy"  # untouched
    assert [b.id for b in store.all("u2")] == ["b1"]
