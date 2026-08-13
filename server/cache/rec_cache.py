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
import time
from collections import OrderedDict
from threading import Lock
from typing import Any, Callable, Iterable

# Bump whenever the shape of a cached payload changes — _to_out fields,
# display-tag cleanup, scoring that alters what gets returned. The version is
# mixed into every signature, so payloads cached by older code can't survive
# a hot reload and be served with the new code's expectations.
# v2: token plural-folding + genre-synonym folding changed scoring output.
# v3: liked books excluded from recommendation candidates.
# v4: cover thumbnails added to book metadata.
# v5: entity-tag demotion + tag-aware genre derivation changed display tags;
#     language-gate resilience changed recommendation output.
# v6: relevance clamped at 100 (was showing 114%); form:/franchise:/nyt: facet
#     handling + broader content->genre derivation changed display tags & scoring.
# v7: description weighted above genre (0.6/0.4); candidates with no description
#     dropped (genre-only is too weak to recommend on); author-aware feedback
#     re-weighting — all change recommendation ranking/output.
# v8: reader-note/review text (e.g. "...gmb 3/15/20") dropped from Open Library
#     descriptions at ingestion, changing both display blurbs and scoring tokens.
# v9: /similar's genre queries now share _genre_atoms' tag cleanup (facet
#     prefixes dropped, trailing periods stripped, "Fiction." recognised as
#     generic), changing /similar's candidate pools and therefore its results.
CACHE_VERSION = 9


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
        scope: Iterable[str] = (),
    ) -> str:
        """Stable hash of a user's saved + thumbs-up + thumbs-down IDs.

        Order-independent within each bucket, and the bucket prefix means
        moving a book from saved to liked still produces a fresh signature.
        Any of the three changing — add, remove, flip — yields a new key.

        `scope` is the subset of book IDs a recommendation was scoped to
        (a section or an ad-hoc selection). It's hashed as its own bucket so
        a full-library run, a section run, and a hand-picked run over the
        same library each get distinct cache entries.
        """
        parts: list[str] = [f"V:{CACHE_VERSION}"]
        parts.extend(f"S:{b}" for b in sorted(saved))
        parts.extend(f"U:{b}" for b in sorted(liked))
        parts.extend(f"D:{b}" for b in sorted(disliked))
        parts.extend(f"C:{b}" for b in sorted(scope))
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

    def __len__(self) -> int:
        """Current entry count — surfaced in /admin/stats for leak-watching."""
        with self._lock:
            return len(self._store)


class TTLCache:
    """Thread-safe LRU cache whose entries also expire after a fixed TTL.

    Used for /similar results: the endpoint is anonymous (no per-user
    invalidation hook), so entries age out instead. The TTL keeps a book's
    "similar" list from fossilizing while still absorbing the common case of
    several users (or one indecisive user) clicking the same book repeatedly.

    `clock` is injectable for tests; production uses time.monotonic.

    `copier` makes a defensive copy on the way in and out so a caller can't
    mutate the cached *container*. It defaults to `list` (the /similar payload
    is a list of book dicts); the fetcher's response cache passes `dict`, since
    its payloads are decoded JSON objects. Both are SHALLOW copies of the right
    container type — the top-level list/dict is isolated, but nested structures
    (a cached dict's inner lists, a cached list's inner dicts) stay shared, so
    callers must treat anything below the top level as read-only. Pass a value
    whose type matches the copier.

    `max_bytes` adds a second, independent ceiling. An entry cap alone bounds
    memory only when entries are roughly the same size, and the fetcher's
    aren't: one Open Library search response at limit=1000 is megabytes, while
    a work-detail lookup is a few KB. 512 of the former is gigabytes — the cap
    reads like a bound and isn't one. Callers pass a per-entry `weight` (the
    raw response length) and eviction runs until both ceilings are satisfied.
    """

    def __init__(
        self,
        max_entries: int = 256,
        ttl_seconds: float = 3600.0,
        clock=time.monotonic,
        copier: Callable[[Any], Any] = list,
        max_bytes: int | None = None,
    ) -> None:
        # value tuples are (expires_at, value, weight)
        self._store: OrderedDict[str, tuple[float, Any, int]] = OrderedDict()
        self._lock = Lock()
        self._max = max_entries
        self._max_bytes = max_bytes
        self._bytes = 0
        self._ttl = ttl_seconds
        self._clock = clock
        self._copy = copier

    def _drop_locked(self, key: str) -> None:
        """Remove one entry, keeping the byte total in step. Caller holds lock."""
        self._bytes -= self._store.pop(key)[2]

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, value, _ = entry
            if self._clock() >= expires_at:
                self._drop_locked(key)
                return None
            self._store.move_to_end(key)
            return self._copy(value)

    def put(self, key: str, value: Any, weight: int = 0) -> None:
        with self._lock:
            if key in self._store:
                self._drop_locked(key)  # replacing: don't double-count
            self._store[key] = (self._clock() + self._ttl, self._copy(value), weight)
            self._bytes += weight
            self._store.move_to_end(key)
            # `len(self._store) > 1` keeps a single oversized entry cached
            # rather than evicting it into an empty cache and re-fetching it
            # every time — one entry over budget beats a permanent cache miss.
            while self._store and (
                len(self._store) > self._max
                or (self._max_bytes is not None
                    and self._bytes > self._max_bytes
                    and len(self._store) > 1)
            ):
                self._drop_locked(next(iter(self._store)))

    def __len__(self) -> int:
        """Current entry count — surfaced in /admin/stats for leak-watching."""
        with self._lock:
            return len(self._store)

    def nbytes(self) -> int:
        """Total weight of cached entries — the number that actually tracks
        memory, since entry count doesn't when payload sizes vary."""
        with self._lock:
            return self._bytes
