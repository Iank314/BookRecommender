"""
server/fetcher/fetcher.py
Fetch book data from a local JSON file, Google Books, or Open Library
and return them as `Books` instances.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import List, Optional

try:
    import requests  # type: ignore
except ImportError:  # pragma: no cover
    requests = None

from server.cache.rec_cache import TTLCache
from server.models.book import Books

GOOGLE_ENDPOINT  = "https://www.googleapis.com/books/v1/volumes"
OPENLIB_ENDPOINT = "https://openlibrary.org/search.json"
OPENLIB_BASE     = "https://openlibrary.org"

# (connect, read) timeouts — fail fast instead of stalling ~60s on a dead socket.
_HTTP_TIMEOUT = (5, 15)

# In-memory response cache so repeated genre queries (litrpg, fantasy, ...) across
# /search, /similar and /library/recommend don't re-hit the APIs within the TTL.
# Bounded LRU + TTL: every distinct (url, params) is a fresh key, so an unbounded
# dict would grow one entry per unique query for the life of the process — a slow
# leak on a long-running server. The cap makes it evict instead. `copier=dict`
# shallow-copies the decoded JSON on the way in/out.
_CACHE_TTL = 600.0  # seconds
_CACHE_MAX_ENTRIES = 512
_cache = TTLCache(max_entries=_CACHE_MAX_ENTRIES, ttl_seconds=_CACHE_TTL, copier=dict)


# Open Library's `description` and `first_sentence` fields are community-editable,
# and some users paste a personal review or note in there instead of a blurb —
# e.g. The Long Earth's description is literally "...just not to my liking. gmb
# 3/15/20". We'd rather show and score NO description than someone's opinion of
# the book, so descriptions matching these patterns are dropped at ingestion.
# A trailing initials+date / bare-date signature is the highest-precision tell
# (a real publisher blurb virtually never ends "gmb 3/15/20"); a small set of
# first-person opinion phrases catches unsigned notes. Kept deliberately narrow
# to avoid discarding legitimate blurbs.
_READER_NOTE_SIGNATURE = re.compile(r"\b\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}\s*$")
# Kept narrow: only phrasing a reader uses about their OWN reaction. Marketing
# blurbs collide with "highly recommended for fans of…" and generic "readers
# found this…", so those are deliberately excluded to avoid dropping real
# descriptions (which the no-description gate would then cut the book on).
_READER_NOTE_OPINION = re.compile(
    r"\b(to my liking|in my opinion|my least favou?rite|not for me|"
    r"i (?:couldn'?t finish|could not finish|didn'?t care for))\b",
    re.IGNORECASE,
)


def _looks_like_reader_note(text: str) -> bool:
    """True if an Open Library description is a reader's review/note, not a blurb."""
    t = (text or "").strip()
    if not t:
        return False
    return bool(_READER_NOTE_SIGNATURE.search(t) or _READER_NOTE_OPINION.search(t))


def cache_size() -> int:
    """Live entry count of the response cache — surfaced in /admin/stats."""
    return len(_cache)

# Cap concurrent Google Books requests. Unauthenticated Google has a very low
# per-IP limit, and we fan genre queries out across a thread pool; without this
# they all fire at once and trip 429. An API key raises the ceiling but this
# keeps us polite regardless.
_GB_SEMAPHORE = threading.Semaphore(3)

# Circuit breaker: once Google Books 429s, stop calling it for a cooldown window
# so we fail fast (and politely) instead of retry-sleeping on every later query.
_GB_COOLDOWN_SECONDS = 60.0
_gb_cooldown_until = 0.0
_gb_state_lock = threading.Lock()


def _gb_in_cooldown() -> bool:
    with _gb_state_lock:
        return time.time() < _gb_cooldown_until


def _set_gb_cooldown() -> None:
    global _gb_cooldown_until
    with _gb_state_lock:
        _gb_cooldown_until = time.time() + _GB_COOLDOWN_SECONDS


def _cache_key(url: str, params: dict | None) -> str:
    items = sorted((params or {}).items())
    return url + "?" + "&".join(f"{k}={v}" for k, v in items)


def _get_json(url: str, params: dict | None, *,
              semaphore: threading.Semaphore | None = None,
              retries: int = 0) -> dict:
    """GET JSON with a TTL cache, optional concurrency cap, and 429 backoff."""
    key = _cache_key(url, params)
    hit = _cache.get(key)
    if hit is not None:
        return hit

    attempt = 0
    while True:
        if semaphore is not None:
            semaphore.acquire()
        try:
            resp = requests.get(url, params=params, timeout=_HTTP_TIMEOUT)
        finally:
            if semaphore is not None:
                semaphore.release()
        if resp.status_code == 429 and attempt < retries:
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if (retry_after or "").isdigit() else 0.5 * (2 ** attempt)
            time.sleep(min(wait, 5.0))
            attempt += 1
            continue
        resp.raise_for_status()
        data = resp.json()
        _cache.put(key, data)
        return data


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

    def fetch_work_detail(self, work_key: str):
        """Fetch an Open Library work's full description + subjects.

        `work_key` is an OL work path like "/works/OL12345W". Best-effort:
        returns (description, subjects) and ("", []) on any failure, since this
        is used only to enrich already-fetched candidates.
        """
        if requests is None:  # pragma: no cover
            return "", []
        key = work_key.strip()
        if not key.startswith("/"):
            key = "/" + key
        try:
            data = _get_json(f"{OPENLIB_BASE}{key}.json", None)
        except Exception:
            return "", []

        desc = data.get("description", "")
        if isinstance(desc, dict):
            desc = desc.get("value", "")
        desc = str(desc or "")
        if _looks_like_reader_note(desc):
            desc = ""  # a reader's note, not a blurb — keep the subjects only
        subjects = data.get("subjects", [])
        if not isinstance(subjects, list):
            subjects = []
        return desc, [str(s) for s in subjects]

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

        # Recently rate-limited — skip the call and let Open Library carry this run.
        if _gb_in_cooldown():
            return ([], 0) if return_total else []

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
        key = self.api_key or os.environ.get("GOOGLE_BOOKS_API_KEY")
        if key:
            params["key"] = key

        try:
            data = _get_json(GOOGLE_ENDPOINT, params,
                             semaphore=_GB_SEMAPHORE, retries=1)
        except requests.exceptions.HTTPError as exc:
            resp = getattr(exc, "response", None)
            if resp is not None and resp.status_code == 429:
                _set_gb_cooldown()  # back off Google for a while, lean on OL
                return ([], 0) if return_total else []
            raise
        items = data.get("items", [])
        total = data.get("totalItems", 0)
        books = [self._from_google_item(it) for it in items]

        if return_total:
            return books, total
        return books

    @staticmethod
    def _from_google_item(item: dict) -> Books:
        info = item.get("volumeInfo", {})
        # Cover thumbnail — Google serves these over http:// in the API
        # response; force https so the image isn't blocked as mixed content.
        thumb = (info.get("imageLinks") or {}).get("thumbnail") or ""
        if thumb.startswith("http://"):
            thumb = "https://" + thumb[len("http://"):]
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
                "language": info.get("language"),
                "thumbnail": thumb or None,
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

        data = _get_json(OPENLIB_ENDPOINT, params)
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
        if _looks_like_reader_note(str(raw or "")):
            raw = ""  # community-edited note, not the book's first sentence

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

        # OL's `language` field lists EVERY edition language, roughly
        # alphabetically — for a much-translated book languages[0] is junk
        # ("ben" for Harry Potter). The search result's title/description are
        # English-leaning, so prefer eng when present; a single-language book
        # is unaffected.
        languages = doc.get("language", []) or []
        if isinstance(languages, list) and languages:
            language = "eng" if "eng" in languages else languages[0]
        else:
            language = None

        # Open Library covers are addressed by the numeric cover_i id.
        cover_id = doc.get("cover_i")
        thumb = (
            f"https://covers.openlibrary.org/b/id/{cover_id}-M.jpg"
            if cover_id else None
        )

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
                "language": language,
                "thumbnail": thumb,
                "source": "open_library",
            },
        )
