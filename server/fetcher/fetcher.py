"""
server/fetcher/fetcher.py
Fetch book data from a local JSON file, Google Books, or Open Library
and return them as `Books` instances.
"""

from __future__ import annotations

import json
import time
from typing import List, Optional

try:
    import requests  # type: ignore
except ImportError:  # pragma: no cover
    requests = None

from server.models.book import Books

GOOGLE_ENDPOINT  = "https://www.googleapis.com/books/v1/volumes"
OPENLIB_ENDPOINT = "https://openlibrary.org/search.json"


class Fetcher:
    """
    Parameters
    ----------
    source : str
        Either a **filepath** (for local JSON) or one of the endpoint
        constants above.
    api_key : str | None
        Optional Google Books API key.  Unused for local/ Open Library.
    """

    def __init__(self, source: str, api_key: Optional[str] = None):
        self.source  = source
        self.api_key = api_key

    # ------------------------------------------------------------------ #
    # Public unified entrypoints
    # ------------------------------------------------------------------ #

    def fetch(self, query: str | None = None, max_results: int = 40,
              category: str = "general") -> List[Books]:
        if self.source in (GOOGLE_ENDPOINT, OPENLIB_ENDPOINT):
            if not query:
                raise ValueError("`query` is required when fetching remotely.")
            if self.source == GOOGLE_ENDPOINT:
                return self._fetch_google_books(query, max_results, category=category)
            books, _ = self._fetch_open_library(query, max_results, category=category)
            return books
        return self._fetch_from_file()

    def fetch_page(self, query: str, batch_size: int = 500,
                   offset: int = 0, category: str = "general"):
        """Fetch a single page from Open Library. Returns (books, total_available)."""
        return self._fetch_open_library(query, batch_size,
                                        category=category, offset=offset)

    def fetch_google_page(self, query: str, max_results: int = 40,
                          start_index: int = 0, category: str = "general"):
        """Fetch a page from Google Books. Returns (books, total_available)."""
        return self._fetch_google_books(query, max_results,
                                        category=category,
                                        start_index=start_index,
                                        return_total=True)

    # ------------------------------------------------------------------ #
    # Local JSON
    # ------------------------------------------------------------------ #

    def _fetch_from_file(self) -> List[Books]:
        with open(self.source, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return [self._from_local_dict(obj) for obj in raw]

    @staticmethod
    def _from_local_dict(raw: dict) -> Books:
        return Books(
            id=raw["id"],
            title=raw["title"],
            authors=raw.get("authors", []),
            description=raw.get("description", ""),
            tags=raw.get("tags", []),
            metadata=raw.get("metadata", {}),
        )

    # ------------------------------------------------------------------ #
    # Google Books
    # ------------------------------------------------------------------ #

    def _fetch_google_books(self, query: str, max_results: int,
                            category: str = "general",
                            start_index: int = 0,
                            return_total: bool = False):
        if requests is None:  # pragma: no cover
            raise ImportError("Install `requests` to use Google Books fetching.")

        # Build category-targeted query for Google Books
        if category == "title":
            q = f"intitle:{query}"
        elif category == "author":
            q = f"inauthor:{query}"
        elif category == "genre":
            q = f"subject:{query}"
        else:
            q = query

        params = {
            "q": q,
            "maxResults": min(max_results, 40),  # Google caps at 40
            "startIndex": start_index,
        }
        if self.api_key:
            params["key"] = self.api_key

        resp = requests.get(GOOGLE_ENDPOINT, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        total = data.get("totalItems", 0)
        books = [self._from_google_item(it) for it in items]

        if return_total:
            return books, total
        return books

    @staticmethod
    def _from_google_item(item: dict) -> Books:
        info = item.get("volumeInfo", {})
        return Books(
            id=f"gb_{item.get('id', '')}",
            title=info.get("title", ""),
            authors=info.get("authors", []),
            description=info.get("description", ""),
            tags=info.get("categories", []),
            metadata={
                "publishedDate": info.get("publishedDate"),
                "pageCount": info.get("pageCount"),
                "infoLink": info.get("infoLink"),
                "source": "google_books",
            },
        )

    # ------------------------------------------------------------------ #
    # Open Library
    # ------------------------------------------------------------------ #

    def _fetch_open_library(self, query: str, max_results: int,
                             category: str = "general",
                             offset: int = 0):
        if requests is None:  # pragma: no cover
            raise ImportError("Install `requests` to use Open Library fetching.")

        if category == "title":
            params = {"title": query, "limit": max_results, "offset": offset}
        elif category == "author":
            params = {"author": query, "limit": max_results, "offset": offset}
        elif category == "genre":
            params = {"subject": query, "limit": max_results, "offset": offset}
        else:
            params = {"q": query, "limit": max_results, "offset": offset}

        resp = requests.get(OPENLIB_ENDPOINT, params=params, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        docs = data.get("docs", [])
        total = data.get("numFound", 0)
        return [self._from_openlib_doc(doc) for doc in docs], total

    @staticmethod
    def _from_openlib_doc(doc: dict) -> Books:
        # Try first_sentence first
        raw = doc.get("first_sentence", "")
        if isinstance(raw, list):
            raw = raw[0] if raw else ""
        if isinstance(raw, dict):
            raw = raw.get("value", "")

        # Build a description from available fields if first_sentence is empty
        if not raw:
            parts = []
            subtitle = doc.get("subtitle", "")
            if subtitle:
                parts.append(subtitle)
            subjects = doc.get("subject", [])
            if subjects:
                parts.append("Subjects: " + ", ".join(subjects[:8]))
            year = doc.get("first_publish_year")
            if year:
                parts.append(f"First published in {year}.")
            authors = doc.get("author_name", [])
            if authors:
                parts.append(f"By {', '.join(authors[:3])}.")
            raw = " | ".join(parts) if parts else ""

        return Books(
            id=f"ol_{doc.get('key', '')}",
            title=doc.get("title", ""),
            authors=doc.get("author_name", []),
            description=str(raw),
            tags=doc.get("subject", [])[:5],
            metadata={
                "publish_year": doc.get("first_publish_year"),
                "edition_count": doc.get("edition_count", 0),
                "ratings_average": doc.get("ratings_average", 0),
                "ratings_count": doc.get("ratings_count", 0),
                "want_to_read_count": doc.get("want_to_read_count", 0),
                "already_read_count": doc.get("already_read_count", 0),
                "source": "open_library",
            },
        )
