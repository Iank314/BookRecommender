"""Tests for the SQLite-backed FeedbackStore."""

from pathlib import Path

import pytest

from server.models.book import Books
from server.storage.feedback_db import FeedbackStore


@pytest.fixture
def store(tmp_path: Path) -> FeedbackStore:
    return FeedbackStore(db_path=tmp_path / "feedback_test.db")


def _book(book_id: str, title: str = "T", tags: list[str] | None = None) -> Books:
    return Books(
        id=book_id, title=title, authors=["A"],
        description="d", tags=tags or [], metadata={},
    )


def test_set_and_lookup_kind(store: FeedbackStore):
    store.set("u1", _book("b1"), "up")
    assert store.kind_for("u1", "b1") == "up"
    assert store.kind_for("u1", "missing") is None


def test_set_overwrites_prior_opinion(store: FeedbackStore):
    # Flipping a thumbs-up to a thumbs-down must replace, not duplicate.
    # The unique (user_id, book_id) PK guarantees this; the test pins it.
    store.set("u1", _book("b1"), "up")
    store.set("u1", _book("b1"), "down")
    assert store.kind_for("u1", "b1") == "down"
    assert len(store.all("u1")) == 1


def test_remove_returns_true_only_when_something_was_there(store: FeedbackStore):
    store.set("u1", _book("b1"), "up")
    assert store.remove("u1", "b1") is True
    assert store.remove("u1", "b1") is False
    assert store.kind_for("u1", "b1") is None


def test_all_filters_by_kind(store: FeedbackStore):
    store.set("u1", _book("liked1"), "up")
    store.set("u1", _book("liked2"), "up")
    store.set("u1", _book("disliked1"), "down")

    ups = store.all("u1", kind="up")
    downs = store.all("u1", kind="down")
    everything = store.all("u1")

    assert {b.id for b, _ in ups} == {"liked1", "liked2"}
    assert {b.id for b, _ in downs} == {"disliked1"}
    assert len(everything) == 3
    assert all(k in ("up", "down") for _, k in everything)


def test_ids_returns_only_one_kind(store: FeedbackStore):
    store.set("u1", _book("up1"), "up")
    store.set("u1", _book("up2"), "up")
    store.set("u1", _book("down1"), "down")
    assert set(store.ids("u1", "up")) == {"up1", "up2"}
    assert set(store.ids("u1", "down")) == {"down1"}


def test_users_are_isolated(store: FeedbackStore):
    store.set("alice", _book("b1"), "up")
    store.set("bob", _book("b1"), "down")
    assert store.kind_for("alice", "b1") == "up"
    assert store.kind_for("bob", "b1") == "down"
    assert len(store.all("alice")) == 1
    assert len(store.all("bob")) == 1


def test_book_payload_round_trips(store: FeedbackStore):
    original = Books(
        id="b1", title="A Book", authors=["X", "Y"],
        description="Some description.",
        tags=["fantasy", "adventure"],
        metadata={"language": "en", "edition_count": 12},
    )
    store.set("u1", original, "up")
    rows = store.all("u1")
    assert len(rows) == 1
    restored, kind = rows[0]
    assert kind == "up"
    assert restored.id == "b1"
    assert restored.title == "A Book"
    assert restored.authors == ["X", "Y"]
    assert restored.tags == ["fantasy", "adventure"]
    assert restored.metadata == {"language": "en", "edition_count": 12}


def test_kind_check_rejects_garbage(store: FeedbackStore, tmp_path: Path):
    # The CHECK constraint should refuse anything but 'up' / 'down', so a
    # typo'd kind from a future caller can't silently land in the DB.
    import sqlite3
    db_path = tmp_path / "feedback_test.db"
    # The store fixture already created the DB; open it directly and try a
    # raw insert with an invalid kind.
    conn = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO feedback_entries (user_id, book_id, kind, title) "
                "VALUES ('u', 'b', 'sideways', 't')"
            )
            conn.commit()
    finally:
        conn.close()
