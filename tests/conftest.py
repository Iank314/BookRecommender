"""Shared fixtures/factories for the store test suites.

`make_book` and the LibraryStore `store` fixture were previously copy-pasted
per file; test modules import the factory directly
(`from conftest import make_book as _book`).
"""

from pathlib import Path

import pytest

from server.models.book import Books
from server.storage.library_db import LibraryStore


def make_book(book_id: str, title: str = "T", tags: list[str] | None = None) -> Books:
    """Minimal saved-book payload used across the store tests."""
    return Books(
        id=book_id, title=title, authors=["A"],
        description="d", tags=tags or [], metadata={},
    )


@pytest.fixture
def store(tmp_path: Path) -> LibraryStore:
    """Fresh LibraryStore on a tmp DB. test_feedback_store shadows this with
    a FeedbackStore fixture of the same name."""
    return LibraryStore(db_path=tmp_path / "library_test.db")
