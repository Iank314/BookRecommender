"""In-process LRU cache for library recommendations.

Entries are keyed by (user_id, library_signature, top_n) and store the final
recommendation list. A hit skips the entire fetch + score + enrich pipeline.

The signature is a stable hash of the user's saved book IDs, so library
mutations (add / remove) produce a new key — old entries quietly fall out via
LRU eviction. Add / remove also call invalidate() to free memory eagerly
rather than wait for eviction.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from threading import Lock
from typing import Iterable


class RecommendationCache:
    """Thread-safe LRU cache: (user_id, signature, top_n) → recommendation list."""

    def __init__(self, max_entries: int = 128) -> None:
        self._store: OrderedDict[tuple[str, str, int], list[dict]] = OrderedDict()
        self._lock = Lock()
        self._max = max_entries

    @staticmethod
    def signature(
        saved: Iterable[str],
        liked: Iterable[str] = (),
        disliked: Iterable[str] = (),
    ) -> str:
        """Stable hash of a user's saved + thumbs-up + thumbs-down IDs.

        Order-independent within each bucket, and the bucket prefix means
        moving a book from saved to liked still produces a fresh signature.
        Any of the three changing — add, remove, flip — yields a new key.
        """
        parts: list[str] = []
        parts.extend(f"S:{b}" for b in sorted(saved))
        parts.extend(f"U:{b}" for b in sorted(liked))
        parts.extend(f"D:{b}" for b in sorted(disliked))
        return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()

    def get(self, user_id: str, sig: str, top_n: int) -> list[dict] | None:
        key = (user_id, sig, top_n)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            self._store.move_to_end(key)
            return list(entry)

    def put(self, user_id: str, sig: str, top_n: int, books: list[dict]) -> None:
        key = (user_id, sig, top_n)
        with self._lock:
            self._store[key] = list(books)
            self._store.move_to_end(key)
            while len(self._store) > self._max:
                self._store.popitem(last=False)

    def invalidate(self, user_id: str) -> None:
        """Drop every entry belonging to one user."""
        with self._lock:
            stale = [k for k in self._store if k[0] == user_id]
            for k in stale:
                del self._store[k]
