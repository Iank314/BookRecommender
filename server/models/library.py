from typing import List, Optional
from .book import Books

class Library:
    """
    Manages a collection of Books, providing CRUD and query methods.
    """
    def __init__(self):
        self._books: dict[str, Books] = {}

    def add(self, book: Books) -> None:
        """Add or replace a book in the library."""
        self._books[book.id] = book

    def all(self) -> List[Books]:
        """Return all books in the library."""
        return list(self._books.values())

    def get_by_id(self, book_id: str) -> Optional[Books]:
        """Fetch a single book by ID, or None if not found."""
        return self._books.get(book_id)