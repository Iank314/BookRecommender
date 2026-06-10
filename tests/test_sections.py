"""Tests for library sections — LibraryStore section CRUD + membership, and
the scope bucket on RecommendationCache.signature that keeps section-scoped
recommendation runs from colliding with full-library ones."""

from pathlib import Path

import pytest

from server.cache.rec_cache import RecommendationCache
from server.models.book import Books
from server.storage.library_db import LibraryStore, SectionNameTakenError


@pytest.fixture
def store(tmp_path: Path) -> LibraryStore:
    return LibraryStore(db_path=tmp_path / "library_test.db")


def _book(book_id: str, title: str = "T") -> Books:
    return Books(
        id=book_id, title=title, authors=["A"],
        description="d", tags=[], metadata={},
    )


# ---- section CRUD ----------------------------------------------------------

def test_create_and_list_sections(store: LibraryStore):
    s1 = store.create_section("u1", "Sci-fi")
    s2 = store.create_section("u1", "Cozy reads")
    listed = store.sections("u1")
    assert [s["name"] for s in listed] == ["Sci-fi", "Cozy reads"]
    assert listed[0]["id"] == s1["id"]
    assert listed[1]["book_ids"] == []
    assert s2["book_ids"] == []


def test_duplicate_section_name_rejected(store: LibraryStore):
    store.create_section("u1", "Sci-fi")
    with pytest.raises(SectionNameTakenError):
        store.create_section("u1", "Sci-fi")
    # ...but another user can reuse the name.
    store.create_section("u2", "Sci-fi")


def test_rename_section(store: LibraryStore):
    s = store.create_section("u1", "Sci-fi")
    assert store.rename_section("u1", s["id"], "Science Fiction") is True
    assert store.sections("u1")[0]["name"] == "Science Fiction"


def test_rename_to_existing_name_rejected(store: LibraryStore):
    store.create_section("u1", "Sci-fi")
    s2 = store.create_section("u1", "Fantasy")
    with pytest.raises(SectionNameTakenError):
        store.rename_section("u1", s2["id"], "Sci-fi")


def test_rename_foreign_section_fails(store: LibraryStore):
    s = store.create_section("u1", "Sci-fi")
    assert store.rename_section("u2", s["id"], "Stolen") is False


def test_delete_section_keeps_books(store: LibraryStore):
    store.add("u1", _book("b1"))
    s = store.create_section("u1", "Sci-fi")
    store.add_to_section("u1", s["id"], "b1")
    assert store.delete_section("u1", s["id"]) is True
    assert store.sections("u1") == []
    assert [b.id for b in store.all("u1")] == ["b1"]  # book untouched
    assert store.delete_section("u1", s["id"]) is False  # already gone


# ---- membership ------------------------------------------------------------

def test_add_and_list_section_books(store: LibraryStore):
    store.add("u1", _book("b1"))
    store.add("u1", _book("b2"))
    s = store.create_section("u1", "Sci-fi")
    assert store.add_to_section("u1", s["id"], "b1") is True
    books = store.section_books("u1", s["id"])
    assert [b.id for b in books] == ["b1"]
    assert store.sections("u1")[0]["book_ids"] == ["b1"]


def test_add_to_section_requires_saved_book(store: LibraryStore):
    s = store.create_section("u1", "Sci-fi")
    assert store.add_to_section("u1", s["id"], "not-saved") is False


def test_add_to_section_requires_own_section(store: LibraryStore):
    store.add("u2", _book("b1"))
    s = store.create_section("u1", "Sci-fi")
    assert store.add_to_section("u2", s["id"], "b1") is False


def test_add_is_idempotent(store: LibraryStore):
    store.add("u1", _book("b1"))
    s = store.create_section("u1", "Sci-fi")
    store.add_to_section("u1", s["id"], "b1")
    store.add_to_section("u1", s["id"], "b1")
    assert len(store.section_books("u1", s["id"])) == 1


def test_remove_from_section(store: LibraryStore):
    store.add("u1", _book("b1"))
    s = store.create_section("u1", "Sci-fi")
    store.add_to_section("u1", s["id"], "b1")
    assert store.remove_from_section("u1", s["id"], "b1") is True
    assert store.section_books("u1", s["id"]) == []
    assert store.remove_from_section("u1", s["id"], "b1") is False


def test_section_books_none_for_unknown_section(store: LibraryStore):
    # None (unknown section) must be distinguishable from [] (empty section).
    assert store.section_books("u1", 999) is None
    s = store.create_section("u1", "Sci-fi")
    assert store.section_books("u1", s["id"]) == []


def test_library_remove_cascades_membership(store: LibraryStore):
    # A book removed from the library must vanish from its sections too,
    # otherwise section_books would resurrect a ghost entry on re-save.
    store.add("u1", _book("b1"))
    s = store.create_section("u1", "Sci-fi")
    store.add_to_section("u1", s["id"], "b1")
    store.remove("u1", "b1")
    assert store.section_books("u1", s["id"]) == []
    assert store.sections("u1")[0]["book_ids"] == []


# ---- moving between sections ------------------------------------------------

def test_move_between_sections(store: LibraryStore):
    store.add("u1", _book("b1"))
    src = store.create_section("u1", "To Read")
    dst = store.create_section("u1", "LitRPG")
    store.add_to_section("u1", src["id"], "b1")
    assert store.move_between_sections("u1", "b1", src["id"], dst["id"]) is True
    assert store.section_books("u1", src["id"]) == []
    assert [b.id for b in store.section_books("u1", dst["id"])] == ["b1"]


def test_move_requires_book_in_source(store: LibraryStore):
    store.add("u1", _book("b1"))
    src = store.create_section("u1", "To Read")
    dst = store.create_section("u1", "LitRPG")
    # b1 was never added to src — nothing to move, and dst must stay empty.
    assert store.move_between_sections("u1", "b1", src["id"], dst["id"]) is False
    assert store.section_books("u1", dst["id"]) == []


def test_move_requires_own_target_section(store: LibraryStore):
    store.add("u1", _book("b1"))
    src = store.create_section("u1", "To Read")
    store.add_to_section("u1", src["id"], "b1")
    foreign = store.create_section("u2", "Theirs")
    assert store.move_between_sections("u1", "b1", src["id"], foreign["id"]) is False
    # Failed move must not have removed the book from the source.
    assert [b.id for b in store.section_books("u1", src["id"])] == ["b1"]


def test_move_to_same_section_is_a_noop(store: LibraryStore):
    store.add("u1", _book("b1"))
    s = store.create_section("u1", "To Read")
    store.add_to_section("u1", s["id"], "b1")
    assert store.move_between_sections("u1", "b1", s["id"], s["id"]) is False
    assert [b.id for b in store.section_books("u1", s["id"])] == ["b1"]


def test_move_when_already_in_target_just_leaves_source(store: LibraryStore):
    # Membership is many-to-many, so the book may already be in the target;
    # the move then collapses to "remove from source" without duplicating.
    store.add("u1", _book("b1"))
    src = store.create_section("u1", "To Read")
    dst = store.create_section("u1", "LitRPG")
    store.add_to_section("u1", src["id"], "b1")
    store.add_to_section("u1", dst["id"], "b1")
    assert store.move_between_sections("u1", "b1", src["id"], dst["id"]) is True
    assert store.section_books("u1", src["id"]) == []
    assert [b.id for b in store.section_books("u1", dst["id"])] == ["b1"]


# ---- cache signature scope bucket ------------------------------------------

def test_signature_scope_distinguishes_section_runs():
    base = dict(saved=["a", "b", "c"], liked=["l"], disliked=["d"])
    full = RecommendationCache.signature(**base)
    scoped_ab = RecommendationCache.signature(**base, scope=["a", "b"])
    scoped_bc = RecommendationCache.signature(**base, scope=["b", "c"])
    assert len({full, scoped_ab, scoped_bc}) == 3


def test_signature_scope_is_order_independent():
    sig1 = RecommendationCache.signature(saved=["a", "b"], scope=["a", "b"])
    sig2 = RecommendationCache.signature(saved=["b", "a"], scope=["b", "a"])
    assert sig1 == sig2
