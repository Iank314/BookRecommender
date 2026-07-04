"""FastAPI server exposing the book recommender as a REST API."""

from __future__ import annotations

import math
import logging
import os
import re
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from fastapi import Body, Cookie, Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, constr

import hashlib

from server.auth_throttle import LoginThrottle
from server.moderation import username_is_clean
from server.cache.rec_cache import CACHE_VERSION, RecommendationCache, TTLCache
from server.fetcher.fetcher import (
    Fetcher,
    GOOGLE_ENDPOINT,
    OPENLIB_ENDPOINT,
    cache_size as fetcher_cache_size,
)
from server.models.book import Books
from server.recommender.recommendation_engine import RecommendationEngine
from server.recommender.recommender import Recommender
from server.storage.activity_db import ActivityStore
from server.storage.feedback_db import FeedbackKind, FeedbackStore
from server.storage.library_db import LibraryStore, SectionNameTakenError
from server.storage.users_db import UserStore, UsernameTakenError


def _env_flag(name: str) -> bool:
    """True if env var `name` is set to a truthy string (1/true/yes/on)."""
    return os.environ.get(name, "").lower() in ("1", "true", "yes", "on")


def _env_number(name: str, default, cast=float):
    """Parse a numeric env var, falling back to `default` if unset or malformed.

    Guards module import: a typo'd BOOKREC_* number (e.g. "2s") would otherwise
    raise ValueError at import and stop the whole app from booting.
    """
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r; falling back to default %r.", name, raw, default)
        return default


SESSION_COOKIE = "bookrec_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year
# Set BOOKREC_SECURE_COOKIES=true in production (Fly/any HTTPS host) so the
# session cookie is only sent over HTTPS. Off by default so local HTTP dev
# keeps working; auto-detection isn't reliable behind reverse proxies that
# terminate TLS at the edge.
SESSION_COOKIE_SECURE = _env_flag("BOOKREC_SECURE_COOKIES")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
logger = logging.getLogger(__name__)
NonEmptyStr = constr(strip_whitespace=True, min_length=1)
Username = constr(strip_whitespace=True, min_length=2, max_length=32)
Password = constr(min_length=6, max_length=128)
Category = Literal["title", "author", "genre", "general"]
RemoteSource = Literal[
    "https://www.googleapis.com/books/v1/volumes",
    "https://openlibrary.org/search.json",
]

app = FastAPI(title="Book Recommender API")
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

# --- Shared state built on startup ---
recommender: Recommender | None = None


# ------------------------------------------------------------------ #
# Observability — request timing + optional tracemalloc leak-finder
# ------------------------------------------------------------------ #
# Requests slower than this log at WARNING instead of INFO: a memory leak, a
# runaway query, or a slow enrichment path all surface as latency creep you can
# grep for. Tune via env; 0 disables the WARNING escalation.
SLOW_REQUEST_MS = _env_number("BOOKREC_SLOW_REQUEST_MS", 2000.0)

# tracemalloc is a heavy-ish allocation tracer, so it's opt-in. When enabled we
# snapshot a baseline at import and log the top allocation growth every N
# requests — the actual leak-*finder* for once timing/RSS say memory is climbing.
_TRACEMALLOC_ON = _env_flag("BOOKREC_TRACEMALLOC")
_TRACEMALLOC_EVERY = max(1, _env_number("BOOKREC_TRACEMALLOC_EVERY", 500, cast=int))
_tm_lock = threading.Lock()
_tm_request_count = 0
_tm_baseline = None
if _TRACEMALLOC_ON:
    import tracemalloc

    tracemalloc.start()
    _tm_baseline = tracemalloc.take_snapshot()
    logger.info(
        "tracemalloc enabled - baseline captured; logging top growth every %d requests.",
        _TRACEMALLOC_EVERY,
    )


def _maybe_log_tracemalloc() -> None:
    """Every N requests, log the top-10 allocation growth vs the startup baseline."""
    global _tm_request_count
    with _tm_lock:
        _tm_request_count += 1
        if _tm_request_count % _TRACEMALLOC_EVERY != 0:
            return
        count = _tm_request_count
    top = tracemalloc.take_snapshot().compare_to(_tm_baseline, "lineno")[:10]
    logger.warning("tracemalloc - top 10 allocation growth after %d requests:", count)
    for stat in top:
        logger.warning("  %s", stat)


@app.middleware("http")
async def _timing_middleware(request, call_next):
    """Log method / path / status / duration for every request. This is the one
    piece of runtime visibility the app previously lacked entirely."""
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.exception(
            "%s %s -> EXC %.1fms", request.method, request.url.path, elapsed_ms
        )
        raise
    elapsed_ms = (time.perf_counter() - start) * 1000
    level = (
        logging.WARNING
        if SLOW_REQUEST_MS and elapsed_ms >= SLOW_REQUEST_MS
        else logging.INFO
    )
    logger.log(
        level, "%s %s -> %s %.1fms",
        request.method, request.url.path, response.status_code, elapsed_ms,
    )
    if _TRACEMALLOC_ON:
        # Snapshotting the whole heap is slow; run it off the event loop so the
        # periodic dump never stalls concurrent requests (and then gets logged
        # as slow by this very middleware).
        await run_in_threadpool(_maybe_log_tracemalloc)
    return response


class BookOut(BaseModel):
    id: str
    title: str
    authors: list[str]
    description: str
    tags: list[str]
    metadata: dict
    relevance: float | None = None
    # Populated on /library listings only; None everywhere else.
    reading_status: Literal["want_to_read", "reading", "read"] | None = None


class BuildRequest(BaseModel):
    query: NonEmptyStr = "coming-of-age fantasy"
    max_results: int = Field(40, ge=1, le=200)
    source: RemoteSource = GOOGLE_ENDPOINT
    category: Category = "general"


class SearchRequest(BaseModel):
    query: NonEmptyStr
    category: Category = "general"
    top_n: int = Field(100, ge=1, le=200)
    page: int = Field(1, ge=1)
    page_size: int = Field(20, ge=1, le=50)
    # Optional published-year window. When either bound is set, books whose
    # year falls outside — or can't be determined — are filtered out.
    year_from: int | None = Field(None, ge=0, le=3000)
    year_to: int | None = Field(None, ge=0, le=3000)


def _publish_year(book) -> int | None:
    """Best-effort published year: OL's first_publish_year (int) or the
    leading year of GB's publishedDate ('2005-03-01' / '2005')."""
    meta = book.metadata or {}
    year = meta.get("publish_year")
    if isinstance(year, int):
        return year
    m = re.match(r"\s*(\d{4})", str(meta.get("publishedDate") or year or ""))
    return int(m.group(1)) if m else None


def _year_in_range(book, lo: int | None, hi: int | None) -> bool:
    """True when no filter is set; with a filter, unknown years are excluded —
    the point of filtering is curation, so 'maybe' doesn't make the cut."""
    if lo is None and hi is None:
        return True
    year = _publish_year(book)
    if year is None:
        return False
    return (lo is None or year >= lo) and (hi is None or year <= hi)


@app.post("/build", summary="Build the recommendation index")
def build_index(req: BuildRequest):
    """Fetch books and build the similarity index."""
    global recommender
    fetcher = Fetcher(source=req.source)
    engine = RecommendationEngine()
    rec = Recommender(fetcher, engine)
    try:
        rec.build(query=req.query, max_results=req.max_results,
                  category=req.category)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to build index: {exc}")
    recommender = rec
    count = len(recommender.library.all())
    return {"status": "ok", "books_indexed": count}


class SearchResponse(BaseModel):
    books: list[BookOut]
    total: int
    page: int
    page_size: int
    total_pages: int


@app.post("/search", response_model=SearchResponse, summary="Search and recommend")
def search(
    req: SearchRequest,
    bookrec_session: str | None = Cookie(default=None),
):
    """Fetch from Google Books + Open Library, score, return paginated results."""
    # Only counts page 1 so paging through results isn't counted as N searches.
    if req.page == 1:
        _record_activity("search", _soft_user_id(bookrec_session))
    fetcher = Fetcher(source=OPENLIB_ENDPOINT)
    query_lower = req.query.lower().strip()

    THRESHOLD = 50.0 if req.category == "genre" else 60.0
    seen_keys: set[str] = set()
    accepted: list[tuple] = []  # (book, score)
    provider_errors: list[str] = []

    # --- 1) Google Books (up to 120 results via 3 pages of 40) ---
    gb_fetcher = Fetcher(source=GOOGLE_ENDPOINT)
    for start_idx in range(0, 120, 40):
        try:
            gb_books, gb_total = gb_fetcher.fetch_google_page(
                req.query, max_results=40,
                start_index=start_idx, category=req.category,
            )
        except Exception:
            provider_errors.append("Google Books")
            logger.warning("Google Books search failed for query %r.", req.query, exc_info=True)
            break  # Google rate-limited or errored — continue with OL
        for book in gb_books:
            dedup_key = _dedup_key(book)
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)
            if not _year_in_range(book, req.year_from, req.year_to):
                continue
            score = _score_book(book, query_lower, req.category)
            if score >= THRESHOLD:
                accepted.append((book, score))
        if start_idx + 40 >= gb_total:
            break

    # --- 2) Open Library (batched, fill up to top_n) ---
    OL_BATCH = 500
    MAX_OL_BATCHES = 5
    ol_offset = 0
    for _ in range(MAX_OL_BATCHES):
        if len(accepted) >= req.top_n:
            break
        try:
            ol_books, ol_total = fetcher.fetch_page(
                req.query, batch_size=OL_BATCH,
                offset=ol_offset, category=req.category,
            )
        except Exception:
            provider_errors.append("Open Library")
            logger.warning("Open Library search failed for query %r.", req.query, exc_info=True)
            break
        if not ol_books:
            break
        for book in ol_books:
            dedup_key = _dedup_key(book)
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)
            if not _year_in_range(book, req.year_from, req.year_to):
                continue
            score = _score_book(book, query_lower, req.category)
            if score >= THRESHOLD:
                accepted.append((book, score))
        ol_offset += OL_BATCH
        if ol_offset >= ol_total:
            break

    # --- 3) Sort, paginate ---
    accepted.sort(key=lambda x: x[1], reverse=True)
    accepted = accepted[: req.top_n]

    if not accepted and len(set(provider_errors)) == 2:
        raise HTTPException(
            status_code=502,
            detail="Search providers failed. Please try again later.",
        )

    total = len(accepted)
    total_pages = math.ceil(total / req.page_size) if total > 0 else 0
    start = (req.page - 1) * req.page_size
    end = start + req.page_size
    page_items = accepted[start:end]

    return SearchResponse(
        books=[_to_out(book, relevance=round(score, 1)) for book, score in page_items],
        total=total,
        page=req.page,
        page_size=req.page_size,
        total_pages=total_pages,
    )


def _dedup_key(book) -> str:
    """Create a dedup key from title + first author, normalised."""
    title = book.title.lower().strip()
    author = book.authors[0].lower().strip() if book.authors else ""
    return f"{title}||{author}"


def _score_book(book, query_lower: str, category: str) -> float:
    """Score a book 0–100 based on category-specific matching + popularity."""

    if category == "title":
        return _score_title(book, query_lower)
    elif category == "author":
        return _score_author(book, query_lower)
    elif category == "genre":
        return _score_genre(book, query_lower, from_subject_search=True)
    # general: combine all signals
    return max(
        _score_title(book, query_lower),
        _score_author(book, query_lower),
        _score_genre(book, query_lower, from_subject_search=False),
    )


def _score_title(book, query_lower: str) -> float:
    title_lower = book.title.lower()
    if not title_lower:
        return 0.0
    # Exact match
    if query_lower == title_lower:
        return 100.0
    # Query is contained in title (e.g. "harry potter" in "harry potter and the ...")
    if query_lower in title_lower:
        # Score higher the more of the title the query covers
        ratio = len(query_lower) / len(title_lower)
        return 80.0 + 20.0 * ratio
    # Title contains part of the query
    query_words = set(query_lower.split())
    title_words = set(title_lower.split())
    if query_words and query_words & title_words:
        overlap = len(query_words & title_words) / len(query_words)
        return 60.0 * overlap
    return 0.0


def _score_author(book, query_lower: str) -> float:
    if not book.authors:
        return 0.0
    best = 0.0
    for author in book.authors:
        author_lower = author.lower()
        if query_lower == author_lower:
            best = 100.0
            break
        if query_lower in author_lower or author_lower in query_lower:
            ratio = min(len(query_lower), len(author_lower)) / max(len(query_lower), len(author_lower))
            best = max(best, 80.0 + 20.0 * ratio)
        else:
            # Partial word overlap (e.g. "rowling" matches "J.K. Rowling")
            q_words = set(query_lower.split())
            a_words = set(author_lower.replace(".", " ").split())
            if q_words & a_words:
                overlap = len(q_words & a_words) / len(q_words)
                best = max(best, 70.0 * overlap)
    return best


def _score_genre(book, query_lower: str, from_subject_search: bool = True) -> float:
    """Score by tag match + popularity boost from Open Library metadata."""
    # Bases are tuned so a clean match lands near 100 regardless of source.
    # Google Books carries no popularity metadata, so without a high base
    # GB results would cap well below OL counterparts.
    base = 0.0
    query_words = set(query_lower.split())
    for tag in book.tags:
        tag_lower = tag.lower()
        if query_lower == tag_lower:
            base = 100.0
            break
        if query_lower in tag_lower:
            base = max(base, 95.0)
        tag_words = set(tag_lower.split())
        if query_words & tag_words:
            overlap = len(query_words & tag_words) / len(query_words)
            base = max(base, 85.0 * overlap)

    # Check title and description for the genre keyword
    combined = f"{book.title} {book.description}".lower()
    if query_lower in combined:
        base = max(base, 85.0)

    # Open Library's subject search already pre-filters by genre,
    # so even books with empty tags are relevant — give them a base score
    if from_subject_search and base == 0:
        base = 80.0

    if base == 0:
        return 0.0

    # Popularity boost (up to +30 points)
    meta = book.metadata
    edition_count = meta.get("edition_count", 0) or 0
    ratings_count = meta.get("ratings_count", 0) or 0
    ratings_avg = meta.get("ratings_average", 0) or 0
    want_to_read = meta.get("want_to_read_count", 0) or 0
    already_read = meta.get("already_read_count", 0) or 0

    # Normalize popularity signals with log scale (diminishing returns)
    pop_score = 0.0
    if edition_count > 0:
        pop_score += min(math.log10(edition_count + 1) / 3.0, 1.0) * 10  # up to 10
    if ratings_count > 0:
        pop_score += min(math.log10(ratings_count + 1) / 4.0, 1.0) * 8   # up to 8
    if ratings_avg > 0:
        pop_score += (ratings_avg / 5.0) * 6                               # up to 6
    if want_to_read + already_read > 0:
        engagement = want_to_read + already_read
        pop_score += min(math.log10(engagement + 1) / 4.0, 1.0) * 6       # up to 6

    return min(base + pop_score, 100.0)


class SimilarRequest(BaseModel):
    # id is optional and only used to enrich a sparse source: an Open Library
    # work id lets us back-fill genres + a real description before scoring.
    # Sparse OL records ("First published in 2005", no tags) otherwise match
    # candidates on publication boilerplate instead of story content.
    id: str = ""
    title: str
    authors: list[str] = Field(default_factory=list)
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    top_n: int = Field(20, ge=1, le=50)


# Results scoring below this floor are dropped: at ~0.05 a "match" is a single
# shared token, and showing one wrong book reads worse than showing none.
MIN_SIMILAR_SCORE = 0.05

# /similar is anonymous and re-runs the whole fetch+score pipeline per click,
# so identical clicks within the TTL window are served from memory. Keyed on
# the source book's identity (title+authors+tags+description), not a user.
similar_cache = TTLCache(max_entries=256, ttl_seconds=3600)


def _similar_cache_key(req: SimilarRequest) -> str:
    parts = [
        f"V:{CACHE_VERSION}",
        f"I:{req.id.strip()}",
        f"T:{req.title.strip().lower()}",
        f"A:{'|'.join(a.strip().lower() for a in req.authors)}",
        f"G:{'|'.join(t.strip().lower() for t in req.tags)}",
        f"D:{req.description.strip().lower()}",
        f"N:{req.top_n}",
    ]
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()


def _squash_author(name: str) -> str:
    """Lowercase, letters only — 'Guilty Three' and 'Guiltythree' both
    become 'guiltythree', so edition-level author spelling can't block a match."""
    return re.sub(r"[^a-z]", "", name.lower())


def _enrich_source_by_title_lookup(source: Books) -> None:
    """Last-resort source enrichment: find the same book in either provider by
    title and borrow the richest record's tags + description. Saves sparse
    records (webnovels, fan prints) whose own id has nothing to enrich from —
    without this they fall back to 'fiction' queries and match on noise."""
    skey, _ = _split_series(source.title)
    src_authors = {_squash_author(a) for a in source.authors if a}

    def _gb() -> list[Books]:
        return Fetcher(source=GOOGLE_ENDPOINT).fetch_google_page(
            source.title, max_results=20, category="title")[0]

    def _ol() -> list[Books]:
        return Fetcher(source=OPENLIB_ENDPOINT).fetch_page(
            source.title, batch_size=40, category="title")[0]

    best_tags: list[str] = []
    best_desc = ""
    for fetch in (_gb, _ol):
        try:
            results = fetch()
        except Exception:
            continue
        for cand in results:
            if _split_series(cand.title)[0] != skey:
                continue
            if src_authors:
                cand_authors = {_squash_author(a) for a in cand.authors if a}
                if cand_authors and not (src_authors & cand_authors):
                    continue  # same title, different book
            if len(cand.description) > len(best_desc):
                best_desc = cand.description
            if cand.tags and not best_tags:
                best_tags = cand.tags
        if best_tags and len(best_desc) >= 60:
            break  # rich enough, skip the second provider

    if best_tags and not source.tags:
        source.tags = best_tags
    if len(best_desc) > len(source.description):
        source.description = best_desc


def _gather_similar_candidates(
    req: SimilarRequest,
) -> tuple[Books, str | None, list[Books]]:
    """Candidate-gathering half of "Find Similar": build genre queries from
    the source's tags, fetch from both providers, keep the source's language,
    enrich tagless candidates, and drop text-only sequels / nonfiction.
    Returns (source_book, source_language, candidates) — candidates may be
    empty. Shared by the /similar endpoint and scripts/explain_similar.py,
    so the debug tool exercises exactly the pipeline users hit.
    """
    source_book = Books(
        id=req.id or "__source__",
        title=req.title,
        authors=req.authors,
        description=req.description,
        tags=req.tags,
        metadata={},
    )
    # --- 0) Enrich a sparse source before anything keys off its tags/text ---
    # _ensure_details back-fills genres + the full description for Open
    # Library works; the genre queries below and the scorer both depend on it.
    if not source_book.tags or len(source_book.description) < 60:
        try:
            _ensure_details(source_book)
        except Exception:
            logger.warning("Source enrichment failed for %r.", req.title, exc_info=True)
    # Still sparse (webnovels, fan prints — no enrichable work id)? Find the
    # same book in either provider by title and borrow its tags/description.
    if not source_book.tags or len(source_book.description) < 60:
        try:
            _enrich_source_by_title_lookup(source_book)
        except Exception:
            logger.warning("Title-lookup enrichment failed for %r.", req.title, exc_info=True)
    src_lang = _book_language(source_book)

    # --- 1) Build genre-only search queries, filtering out proper nouns ---
    title_lower = req.title.lower()
    author_words = set()
    for a in req.authors:
        for w in a.lower().replace(".", " ").split():
            if len(w) > 2:
                author_words.add(w)
    # Extract title words to filter out (e.g. "harry", "potter")
    title_words = {w for w in title_lower.split() if len(w) > 2}

    # Split slash- and comma-separated tags into atoms, then filter.
    # Google Books returns categories like "Fiction / Fantasy / Action &
    # Adventure" — searching that whole string as a subject pulls back
    # noise (e.g. random Russian fiction) instead of clean genre matches.
    raw_tags: list[str] = []
    for tag in source_book.tags:  # post-enrichment, may be richer than req.tags
        for part in re.split(r"[/,]", tag):
            atom = part.strip()
            if atom:
                raw_tags.append(atom)

    specific_queries: list[str] = []
    generic_queries: list[str] = []  # fallback if no specific queries survive
    seen_query_lower: set[str] = set()
    for tag in raw_tags:
        # Strip nationality/language qualifiers: "Russian fantasy" → "fantasy".
        # Otherwise the subject search returns books *from* that country
        # rather than books in the actual genre.
        words = [w for w in tag.split() if w.lower() not in _LANG_QUALIFIERS]
        if not words:
            continue
        cleaned = " ".join(words)
        tag_lower = cleaned.lower()
        # Not a genre — never a useful subject-search query. Checked on the raw
        # (unfolded) tag, unlike _genre_atoms which folds synonyms first; safe
        # while no noise atom is also a _GENRE_SYNONYMS key/target (add here too
        # if that ever changes).
        if tag_lower.rstrip(".") in _GENRE_NOISE_ATOMS:
            continue
        if tag_lower in seen_query_lower:
            continue
        tag_words_set = set(tag_lower.split())
        # Skip if the tag is basically the title or author
        if tag_words_set & title_words and len(tag_words_set & title_words) / max(len(tag_words_set), 1) > 0.5:
            continue
        if tag_words_set & author_words:
            continue
        seen_query_lower.add(tag_lower)
        # "Fiction", "Literature", etc. on their own return mostly classics
        # via OL's subject search — only use them if nothing specific exists.
        if tag_lower in _GENERIC_GENRE_TAGS:
            generic_queries.append(cleaned)
        else:
            specific_queries.append(cleaned)

    genre_queries = specific_queries or generic_queries
    if not genre_queries:
        # No usable tags at all — scan title + description for a known genre
        # keyword (same fallback the display layer uses) before surrendering
        # to "fiction", whose candidates are mostly random classics.
        derived = _derive_genre_from_text(f"{source_book.title} {source_book.description}")
        genre_queries = [derived.lower()] if derived else ["fiction"]

    # Up to 5 atom queries — slash-splitting often produces more useful atoms.
    genre_queries = genre_queries[:5]

    # --- 2) Fetch candidates using genre queries ---
    all_books = []
    seen_keys: set[str] = set()
    source_key = _dedup_key_raw(req.title, req.authors[0] if req.authors else "")
    seen_keys.add(source_key)

    # Words to check for "about the source" filtering
    filter_words = title_words | author_words

    gb_fetcher = Fetcher(source=GOOGLE_ENDPOINT)
    ol_fetcher = Fetcher(source=OPENLIB_ENDPOINT)

    for query in genre_queries:
        # Google Books — top 40 most relevant
        try:
            gb_books, _ = gb_fetcher.fetch_google_page(
                query, max_results=40, category="genre",
            )
            for b in gb_books:
                if _is_about_source(b, filter_words):
                    continue
                dk = _dedup_key(b)
                if dk not in seen_keys:
                    seen_keys.add(dk)
                    all_books.append(b)
        except Exception:
            logger.warning("Google Books similar-book fetch failed for query %r.", query, exc_info=True)

        # Open Library — wider net since OL is where the long tail lives
        try:
            ol_books, _ = ol_fetcher.fetch_page(
                query, batch_size=300, category="genre",
            )
            for b in ol_books:
                if _is_about_source(b, filter_words):
                    continue
                dk = _dedup_key(b)
                if dk not in seen_keys:
                    seen_keys.add(dk)
                    all_books.append(b)
        except Exception:
            logger.warning("Open Library similar-book fetch failed for query %r.", query, exc_info=True)

    if not all_books:
        return source_book, src_lang, []

    # --- 3) Keep only candidates in the source's language ---
    # An English source shouldn't surface Russian editions of the same trope.
    if src_lang and src_lang != "non-latin":
        all_books = [b for b in all_books if _book_language(b) == src_lang]

    # --- 4) Enrich, filter, and score — same pipeline as /library/recommend ---
    # Tagless OL candidates get genres + a fuller description back-filled so
    # they can be judged on genre, not on a one-line description.
    _enrich_tagless_candidates(all_books, [source_book])

    # Drop candidates whose description marks them as a mid-series volume the
    # title doesn't reveal ("the fourth book in...") — title-marked volumes
    # still flow through the series collapse + entry-point swap below.
    all_books = [
        c for c in all_books
        if _split_series(c.title)[1] is not None or not _desc_is_sequel(c.description)
    ]

    # A fiction source shouldn't surface nonfiction that shares theme words
    # ("magic" the occult topic vs. fantasy magic).
    if _fiction_signal(source_book) > 0:
        all_books = [c for c in all_books if _fiction_signal(c) >= 0]

    return source_book, src_lang, all_books


@app.post("/similar", response_model=list[BookOut], summary="Find similar books")
def find_similar(
    req: SimilarRequest,
    bookrec_session: str | None = Cookie(default=None),
):
    """Given a book's info, fetch genre-matched candidates and rank them by
    the same genre-overlap ⊕ description-similarity blend as /library/recommend."""

    # Recorded before the cache check — a cache hit is still a use.
    _record_activity("similar", _soft_user_id(bookrec_session))

    cache_key = _similar_cache_key(req)
    cached = similar_cache.get(cache_key)
    if cached is not None:
        return cached

    source_book, src_lang, candidates = _gather_similar_candidates(req)
    if not candidates:
        return []

    scored = _score_similar_candidates(source_book, candidates)
    # Floor: a 2% match is a single shared token, not a recommendation.
    # Better an honest "no similar books found" than one wrong book.
    scored = [(c, s) for c, s in scored if s >= MIN_SIMILAR_SCORE]
    if not scored:
        return []

    # --- 5) Collapse each series to its earliest-volume entry ---
    collapsed = _collapse_series_picks([(cand, score, None) for cand, score in scored])
    top = [(book, score) for book, score, _ in collapsed][: req.top_n]

    # --- 6) Swap any later-volume entry for its book 1 ---
    src_titles = {_norm_title(req.title)}
    src_langs = {src_lang} if src_lang and src_lang != "non-latin" else set()
    top = _swap_to_entry_points(top, src_langs, src_titles)

    result = [_to_out(book, relevance=round(sim * 100, 1)) for book, sim in top]
    # Empty results aren't cached — the earlier exits are usually transient
    # provider failures, and an hour of cached emptiness would mask recovery.
    if result:
        similar_cache.put(cache_key, result)
    return result


_SIM_STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "are", "was", "were",
    "has", "had", "have", "but", "not", "all", "any", "you", "your", "their",
    "they", "them", "his", "her", "him", "she", "who", "what", "when", "where",
    "how", "why", "into", "out", "than", "then", "also", "more", "most", "one",
    "two", "new", "book", "books", "edition", "vol", "volume", "general",
    "first", "second", "third", "english",
    # Genre-noise: every fiction book shares these so they add no signal.
    "fiction", "novel", "novels", "story", "stories", "tale", "tales",
    # Publication boilerplate — sparse Open Library descriptions are often just
    # "First published in 2005", and these tokens were the TOP match driver for
    # such records (found via scripts/explain_similar.py).
    "published", "publisher", "publishing", "publication", "bestselling",
    "bestseller", "author", "authors", "copyright", "paperback", "hardcover",
    "translated", "translation", "reprint", "york",  # "New York Times bestseller"
}

_LANG_QUALIFIERS = {
    "russian", "english", "american", "british", "french", "german", "japanese",
    "chinese", "korean", "spanish", "italian", "indian", "irish", "scottish",
    "australian", "canadian", "literature",
}

_GENERIC_GENRE_TAGS = {
    "fiction", "general", "literature", "novels", "nonfiction", "non-fiction",
}


def _book_popularity(book: Books) -> float:
    """0-1 popularity proxy from Open Library metadata. 0 when unavailable."""
    meta = book.metadata or {}
    edition_count = meta.get("edition_count") or 0
    ratings_count = meta.get("ratings_count") or 0
    ratings_avg = meta.get("ratings_average") or 0
    engagement = (meta.get("want_to_read_count") or 0) + (meta.get("already_read_count") or 0)

    score = 0.0
    if edition_count > 0:
        score = max(score, min(math.log10(edition_count + 1) / 3.0, 1.0))
    if ratings_count > 0:
        score = max(score, min(math.log10(ratings_count + 1) / 4.0, 1.0))
    if ratings_avg > 0:
        score = max(score, ratings_avg / 5.0)
    if engagement > 0:
        score = max(score, min(math.log10(engagement + 1) / 4.0, 1.0))
    return score


def _fold_token(tok: str) -> str:
    """Light plural/variant folding so 'swords'/'sword' and 'stories'/'story'
    land on the same token — without folding, they contribute zero overlap.

    Deliberately tiny: a real stemmer (Porter) over-stems short description
    vocabulary and hurts precision. The folded form doesn't need to be a real
    word ('stories' and 'story' both → 'stori') because both sides of every
    comparison fold identically.
    """
    if len(tok) > 4 and tok.endswith("ies"):
        return tok[:-3] + "i"          # stories → stori (with y→i below, story → stori)
    if len(tok) > 4 and tok.endswith(("sses", "ches", "shes", "xes", "zes")):
        return tok[:-2]                # witches → witch, kisses → kiss
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith(("ss", "us", "is")):
        tok = tok[:-1]                 # swords → sword, horses → horse
    if len(tok) > 3 and tok.endswith("y"):
        return tok[:-1] + "i"          # story → stori, matching the ies-fold
    return tok


def _text_tokens(book: Books) -> set[str]:
    """Folded token set from title + description only — genre tags excluded.

    Used for the description-similarity signal, kept separate from the genre
    signal so genre words don't get counted twice.
    """
    text = f"{book.title} {book.description}".lower()
    out: set[str] = set()
    for tok in re.findall(r"[a-z]+", text):
        if len(tok) <= 2 or tok in _SIM_STOPWORDS:
            continue
        folded = _fold_token(tok)
        if folded in _SIM_STOPWORDS:   # e.g. "ones" → "one"
            continue
        out.add(folded)
    return out


def _compute_token_idf(books: list[Books], token_fn=_text_tokens) -> dict[str, float]:
    """IDF for every token across the candidate corpus."""
    df: dict[str, int] = {}
    for book in books:
        for tok in token_fn(book):
            df[tok] = df.get(tok, 0) + 1
    n = len(books)
    return {tok: math.log((n + 1) / (count + 1)) + 1 for tok, count in df.items()}


def _idf_weighted_f1(
    src: set[str], cand: set[str],
    idf: dict[str, float], default_idf: float,
) -> float:
    """F1 where each token contributes its IDF weight, not 1."""
    if not src or not cand:
        return 0.0
    inter = src & cand
    if not inter:
        return 0.0
    w_inter = sum(idf.get(t, default_idf) for t in inter)
    w_src = sum(idf.get(t, default_idf) for t in src)
    w_cand = sum(idf.get(t, default_idf) for t in cand)
    if w_src == 0 or w_cand == 0:
        return 0.0
    recall = w_inter / w_src
    precision = w_inter / w_cand
    return 2 * recall * precision / (recall + precision)


def _has_recommendable_content(book: Books) -> bool:
    """True if a candidate has a description to recommend on.

    The description is the primary similarity signal (weighted highest in
    scoring); without one, a candidate can only be judged on genre + title
    tokens — too weak to be a real content-based recommendation. Two ways this
    bit users: "The City Cantabile Choir Presents" (no author, no description)
    rode in on a lone matching genre atom, and "An Atlas of Fantasy" (a geology
    / literary-criticism reference book with no blurb) matched an all-fantasy
    library on the word "fantasy" in its title alone. Both recommendation paths
    enrich sparse Open Library descriptions BEFORE this gate runs, so a
    still-empty description means the providers genuinely have no content to
    assess — drop it rather than guess from genre.
    """
    return bool((book.description or "").strip())


# --- Feedback re-weighting -------------------------------------------------- #
# A thumbs-up nudges similar candidates up; a thumbs-down pushes similar ones
# down. Two signals combine multiplicatively on the base genre/description
# score:
#   1. Description-token similarity (the same IDF-weighted F1 used for ranking)
#      to the nearest liked / disliked book. Dislikes weigh more than likes
#      (FB_BETA > FB_ALPHA): users react more strongly to "stop showing me
#      this" than to "give me more like this".
#   2. Author match. Disliking one or more books by an author is a clear "not
#      this author" signal that token overlap between sibling books routinely
#      misses (two Warrior Cats novels share little prose), so a candidate by a
#      disliked author is cut hard. A liked author gets a gentle lift.
# The clamp lets a single description signal only dampen/lift, while the author
# penalty can still drive a rejected author/series far down the list.
FB_ALPHA, FB_BETA = 0.5, 1.0
FB_MOD_LO, FB_MOD_HI = 0.02, 1.5
FB_AUTHOR_DISLIKE = 0.2
FB_AUTHOR_LIKE = 1.15


def _feedback_authors(books: list[Books]) -> set[str]:
    """Squashed author names across a feedback list (edition-spelling-proof via
    _squash_author, so 'Erin Hunter' and 'erin  hunter' collapse to one key)."""
    return {
        _squash_author(a)
        for b in books for a in b.authors
        if a and a.strip()
    }


def _feedback_modifier(
    cand: Books,
    cand_text: set[str],
    liked_text: list[set[str]],
    disliked_text: list[set[str]],
    liked_authors: set[str],
    disliked_authors: set[str],
    idf: dict[str, float],
    default_idf: float,
) -> float:
    """Multiplicative score re-weight in [FB_MOD_LO, FB_MOD_HI] from thumbs
    up/down — see the block comment above for the two signals it blends."""
    up_sim = max(
        (_idf_weighted_f1(t, cand_text, idf, default_idf) for t in liked_text),
        default=0.0,
    )
    down_sim = max(
        (_idf_weighted_f1(t, cand_text, idf, default_idf) for t in disliked_text),
        default=0.0,
    )
    mod = 1.0 + FB_ALPHA * up_sim - FB_BETA * down_sim
    cand_authors = {_squash_author(a) for a in cand.authors if a and a.strip()}
    if cand_authors & disliked_authors:
        mod *= FB_AUTHOR_DISLIKE
    elif cand_authors & liked_authors:
        mod *= FB_AUTHOR_LIKE
    return max(FB_MOD_LO, min(FB_MOD_HI, mod))


def _score_similar_candidates(
    source: Books, candidates: list[Books],
) -> list[tuple[Books, float]]:
    """Score "Find Similar" candidates against one source book, best first.

    Same math as the /library/recommend scoring loop: IDF-weighted token-set
    F1 over title + description (genre tags excluded so genre isn't counted
    twice), blended 50/50 with genre-atom overlap when both sides carry tags,
    and popularity as a small multiplicative tiebreaker. The endpoint
    previously lumped title + tags + description into one token bag and added
    popularity on top — a candidate sharing only genre-tag strings scored as
    if its text matched, and popular-but-irrelevant books nearly doubled
    their score at the low end.
    """
    src_text = _text_tokens(source)
    src_genres = set(_genre_atoms(source.tags)[0])
    if not src_text and not src_genres:
        return []

    idf = _compute_token_idf(candidates)
    default_idf = math.log(len(candidates) + 1) + 1
    # Description outweighs genre (0.6 vs 0.4): the description is the primary
    # relevance signal, genre a strong secondary, popularity only a tiebreaker.
    W_GENRE, W_DESC = 0.4, 0.6

    scored: list[tuple[Books, float]] = []
    for cand in candidates:
        if not _has_recommendable_content(cand):
            continue  # no description and no author — data junk, not a rec
        cand_text = _text_tokens(cand)
        desc_score = _idf_weighted_f1(src_text, cand_text, idf, default_idf)
        # When the source has text, a candidate must share some of it — genre
        # overlap alone can't rank it (every candidate was fetched by genre).
        if src_text and desc_score <= 0:
            continue
        cand_genres = set(_genre_atoms(cand.tags)[0])
        if cand_genres and src_genres:
            combined = W_GENRE * _genre_score(cand_genres, src_genres) + W_DESC * desc_score
        else:
            combined = desc_score
        if combined <= 0:
            continue
        final = combined * (1.0 + 0.05 * _book_popularity(cand))
        scored.append((cand, final))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def _is_about_source(book, filter_words: set) -> bool:
    """Check if a book is about the source (biography, companion, etc.)."""
    title_lower = book.title.lower()
    title_word_set = set(title_lower.split())
    # If 2+ filter words appear in the title, it's likely about the source
    matches = title_word_set & filter_words
    return len(matches) >= 2


def _dedup_key_raw(title: str, author: str) -> str:
    return f"{title.lower().strip()}||{author.lower().strip()}"


# ------------------------------------------------------------------ #
# Authentication — accounts + login sessions
# ------------------------------------------------------------------ #
user_store = UserStore()
library_store = LibraryStore()
feedback_store = FeedbackStore()
activity_store = ActivityStore()
rec_cache = RecommendationCache()
login_throttle = LoginThrottle()


def _soft_user_id(session_token: str | None) -> str | None:
    """Resolve a session cookie to a user_id without requiring one — for
    attributing activity on endpoints that are open to anonymous visitors."""
    return user_store.user_for_session(session_token) if session_token else None


def _record_activity(kind: str, user_id: str | None) -> None:
    """Best-effort: a stats insert must never fail a real request."""
    try:
        activity_store.record(kind, user_id)
    except Exception:
        logger.warning("Failed to record %s activity.", kind, exc_info=True)


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="lax",
    )


def _clear_session_cookie(response: Response) -> None:
    # The browser only deletes a cookie when the delete request mirrors the
    # attributes it was set with — secure/samesite in particular — otherwise
    # it's treated as a different cookie and the original lingers.
    response.delete_cookie(
        key=SESSION_COOKIE,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="lax",
    )


def get_current_user_id(
    bookrec_session: str | None = Cookie(default=None),
) -> str:
    """Resolve the logged-in user from the session cookie, or 401."""
    if bookrec_session:
        user_id = user_store.user_for_session(bookrec_session)
        if user_id:
            return user_id
    raise HTTPException(status_code=401, detail="Not logged in.")


def get_admin_user_id(user_id: str = Depends(get_current_user_id)) -> str:
    """Like get_current_user_id, but 403s unless the account is an admin.
    Admin is granted via scripts/make_admin.py only — there is deliberately
    no web path to set the flag."""
    if not user_store.is_admin(user_id):
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user_id


class AuthRequest(BaseModel):
    username: Username
    password: Password


class AuthResponse(BaseModel):
    username: str
    is_admin: bool = False


@app.post("/auth/register", response_model=AuthResponse, summary="Create an account")
def auth_register(req: AuthRequest, response: Response):
    if not username_is_clean(req.username):
        raise HTTPException(
            status_code=422,
            detail="That username isn't allowed. Please pick a different one.",
        )
    try:
        user_id = user_store.create_user(req.username, req.password)
    except UsernameTakenError:
        raise HTTPException(status_code=409, detail="Username already taken.")
    _set_session_cookie(response, user_store.create_session(user_id))
    return AuthResponse(username=req.username)


@app.post("/auth/login", response_model=AuthResponse, summary="Log in")
def auth_login(req: AuthRequest, response: Response):
    if not login_throttle.is_allowed(req.username):
        raise HTTPException(
            status_code=429,
            detail="Too many failed login attempts. Try again in a minute.",
        )
    user_id = user_store.verify_credentials(req.username, req.password)
    if not user_id:
        login_throttle.record_failure(req.username)
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    login_throttle.clear(req.username)
    _set_session_cookie(response, user_store.create_session(user_id))
    return AuthResponse(
        username=user_store.get_username(user_id) or req.username,
        is_admin=user_store.is_admin(user_id),
    )


@app.post("/auth/logout", summary="Log out")
def auth_logout(
    response: Response,
    bookrec_session: str | None = Cookie(default=None),
):
    if bookrec_session:
        user_store.delete_session(bookrec_session)
    _clear_session_cookie(response)
    return {"status": "ok"}


@app.get("/auth/me", response_model=AuthResponse, summary="Current logged-in user")
def auth_me(user_id: str = Depends(get_current_user_id)):
    username = user_store.get_username(user_id)
    if not username:
        raise HTTPException(status_code=401, detail="Not logged in.")
    return AuthResponse(username=username, is_admin=user_store.is_admin(user_id))


# ------------------------------------------------------------------ #
# Admin — usage statistics (admin accounts only)
# ------------------------------------------------------------------ #
def _process_memory() -> dict:
    """Best-effort process memory, in MiB. Turns 'is memory growing?' into a
    number. Empty on platforms where the source isn't available (e.g. Windows
    dev has no /proc and no `resource` module) — prod is Linux, where it matters.
    """
    out: dict[str, float] = {}
    # Current RSS from /proc (Linux) — the number that climbs on a leak.
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    out["rss_mb"] = round(int(line.split()[1]) / 1024, 1)  # KiB→MiB
                    break
    except (OSError, ValueError, IndexError):
        # Missing /proc (non-Linux) or a malformed VmRSS line — best-effort, so
        # degrade to no rss_mb rather than 500 the admin panel.
        pass
    # Peak RSS via resource (POSIX). ru_maxrss is KiB on Linux (bytes on macOS,
    # but prod is Linux) — a high-water mark that only ever grows.
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        out["peak_rss_mb"] = round(peak / 1024, 1)
    except (ImportError, ValueError):
        pass
    return out


@app.get("/admin/stats", summary="Admin: accounts and usage statistics")
def admin_stats(user_id: str = Depends(get_admin_user_id)):
    now = int(time.time())
    day, week = now - 86_400, now - 7 * 86_400

    last_seen = activity_store.last_seen_by_user()
    accounts = []
    for a in user_store.list_accounts():
        accounts.append({
            "username": a["username"],
            "is_admin": a["is_admin"],
            "created_at": a["created_at"],
            "books_saved": len(library_store.all(a["user_id"])),
            "last_active": last_seen.get(a["user_id"]),
        })

    return {
        "now": now,
        "accounts_total": len(accounts),
        "accounts": accounts,
        # Events by kind: search (page 1 only), similar, recommend.
        "activity": {
            "last_24h": activity_store.counts_since(day),
            "last_7d": activity_store.counts_since(week),
            "all_time": activity_store.counts_since(0),
        },
        # Distinct logged-in users with any tracked event.
        "active_users": {
            "last_24h": activity_store.active_users_since(day),
            "last_7d": activity_store.active_users_since(week),
        },
        # Tracked events from visitors with no session (anonymous searches).
        "anonymous_events": {
            "last_24h": activity_store.anonymous_events_since(day),
            "last_7d": activity_store.anonymous_events_since(week),
        },
        # In-process cache occupancy — watch for any that pin at its cap and
        # never drain, or memory that climbs while these stay flat (leak is
        # elsewhere). All three are bounded LRUs.
        "caches": {
            "recommendation": len(rec_cache),
            "similar": len(similar_cache),
            "fetcher": fetcher_cache_size(),
        },
        # Process memory in MiB (Linux prod only; empty on Windows dev).
        "memory": _process_memory(),
    }


# ------------------------------------------------------------------ #
# Personal Library — save, list, remove, get recommendations
# ------------------------------------------------------------------ #
class SaveBookRequest(BaseModel):
    id: str
    title: str
    authors: list[str] = Field(default_factory=list)
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


@app.post("/library/add", response_model=BookOut, summary="Save a book to your library")
def library_add(req: SaveBookRequest, user_id: str = Depends(get_current_user_id)):
    book = Books(
        id=req.id, title=req.title, authors=req.authors,
        description=req.description, tags=req.tags, metadata=req.metadata,
    )
    # Capture language and back-fill sparse genres/description once, at save
    # time, so recommendations don't have to re-fetch the same book later.
    _ensure_details(book)
    library_store.add(user_id, book)
    rec_cache.invalidate(user_id)
    return _to_out(book)


@app.get("/library", response_model=list[BookOut], summary="List your saved books")
def library_list(user_id: str = Depends(get_current_user_id)):
    status_by_id = library_store.statuses(user_id)
    out = []
    for b in library_store.all(user_id):
        item = _to_out(b)
        item["reading_status"] = status_by_id.get(b.id)
        out.append(item)
    return out


class ReadingStatusRequest(BaseModel):
    book_id: NonEmptyStr
    status: Literal["want_to_read", "reading", "read"] | None = None  # None clears


@app.post("/library/status", summary="Set or clear a saved book's reading status")
def library_set_status(
    req: ReadingStatusRequest, user_id: str = Depends(get_current_user_id),
):
    if not library_store.set_status(user_id, req.book_id, req.status):
        raise HTTPException(status_code=404, detail="Book not in library.")
    # No rec-cache invalidation: status doesn't feed the scoring pipeline;
    # status-scoped recommendations go through the book_ids scope, which is
    # already part of the cache signature.
    return {"status": "ok"}


# ------------------------------------------------------------------ #
# Sections — user-defined shelves within the saved library
# ------------------------------------------------------------------ #
SectionName = constr(strip_whitespace=True, min_length=1, max_length=60)


class SectionRequest(BaseModel):
    name: SectionName


class SectionOut(BaseModel):
    id: int
    name: str
    book_ids: list[str]


class SectionBookRequest(BaseModel):
    book_id: NonEmptyStr
    # When set, the book is moved: removed from this section and added to the
    # target in one transaction. Omit for a plain add (book can be in both).
    from_section_id: int | None = None


@app.get("/library/sections", response_model=list[SectionOut],
         summary="List your library sections")
def sections_list(user_id: str = Depends(get_current_user_id)):
    return library_store.sections(user_id)


@app.post("/library/sections", response_model=SectionOut,
          summary="Create a library section")
def sections_create(req: SectionRequest, user_id: str = Depends(get_current_user_id)):
    try:
        return library_store.create_section(user_id, req.name)
    except SectionNameTakenError:
        raise HTTPException(status_code=409, detail="You already have a section with that name.")


@app.patch("/library/sections/{section_id}", summary="Rename a section")
def sections_rename(
    section_id: int, req: SectionRequest,
    user_id: str = Depends(get_current_user_id),
):
    try:
        if not library_store.rename_section(user_id, section_id, req.name):
            raise HTTPException(status_code=404, detail="Section not found.")
    except SectionNameTakenError:
        raise HTTPException(status_code=409, detail="You already have a section with that name.")
    return {"status": "ok"}


@app.delete("/library/sections/{section_id}", summary="Delete a section")
def sections_delete(section_id: int, user_id: str = Depends(get_current_user_id)):
    if not library_store.delete_section(user_id, section_id):
        raise HTTPException(status_code=404, detail="Section not found.")
    rec_cache.invalidate(user_id)
    return {"status": "ok"}


@app.post("/library/sections/{section_id}/books",
          summary="Add a saved book to a section (or move it from another)")
def sections_add_book(
    section_id: int, req: SectionBookRequest,
    user_id: str = Depends(get_current_user_id),
):
    if req.from_section_id is not None:
        ok = library_store.move_between_sections(
            user_id, req.book_id, req.from_section_id, section_id,
        )
        detail = "Section not found, or that book isn't in the source section."
    else:
        ok = library_store.add_to_section(user_id, section_id, req.book_id)
        detail = "Section not found, or that book isn't in your library."
    if not ok:
        raise HTTPException(status_code=404, detail=detail)
    rec_cache.invalidate(user_id)
    return {"status": "ok"}


@app.delete("/library/sections/{section_id}/books/{book_id:path}",
            summary="Remove a book from a section")
def sections_remove_book(
    section_id: int, book_id: str,
    user_id: str = Depends(get_current_user_id),
):
    if not library_store.remove_from_section(user_id, section_id, book_id):
        raise HTTPException(status_code=404, detail="That book isn't in this section.")
    rec_cache.invalidate(user_id)
    return {"status": "ok"}


# ------------------------------------------------------------------ #
# Feedback — thumbs up / thumbs down signals for the recommender
# ------------------------------------------------------------------ #
class FeedbackRequest(BaseModel):
    id: str
    title: str
    kind: FeedbackKind
    authors: list[str] = Field(default_factory=list)
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class FeedbackOut(BookOut):
    kind: FeedbackKind


@app.post("/library/feedback", response_model=FeedbackOut,
          summary="Record thumbs up / down on a book")
def feedback_set(req: FeedbackRequest, user_id: str = Depends(get_current_user_id)):
    book = Books(
        id=req.id, title=req.title, authors=req.authors,
        description=req.description, tags=req.tags, metadata=req.metadata,
    )
    # Back-fill genres/description/language at feedback time too, so the
    # recommender's similarity signal has real tokens to work with — a sparse
    # OL record would otherwise drag down or boost based on almost nothing.
    _ensure_details(book)
    feedback_store.set(user_id, book, req.kind)
    rec_cache.invalidate(user_id)
    out = _to_out(book)
    out["kind"] = req.kind
    return out


# NOTE: Open Library book ids contain slashes ("ol_/works/OL123W"). The ASGI
# spec decodes %2F before routing, so these book-id segments must use the
# :path converter or every OL book 404s on delete.
@app.delete("/library/feedback/{book_id:path}", summary="Clear feedback on a book")
def feedback_remove(book_id: str, user_id: str = Depends(get_current_user_id)):
    if not feedback_store.remove(user_id, book_id):
        raise HTTPException(status_code=404, detail="No feedback recorded for that book.")
    rec_cache.invalidate(user_id)
    return {"status": "ok"}


@app.get("/library/feedback", response_model=list[FeedbackOut],
         summary="List your thumbs-up/down books")
def feedback_list(
    kind: FeedbackKind | None = Query(default=None),
    user_id: str = Depends(get_current_user_id),
):
    rows = feedback_store.all(user_id, kind=kind)
    out: list[dict] = []
    for book, k in rows:
        item = _to_out(book)
        item["kind"] = k
        out.append(item)
    return out


# Registered AFTER /library/feedback/... and /library/sections/... on purpose:
# the {book_id:path} converter matches slashes (required for OL ids), so this
# route would swallow those URLs if it were registered first.
@app.delete("/library/{book_id:path}", summary="Remove a book from your library")
def library_remove(book_id: str, user_id: str = Depends(get_current_user_id)):
    if not library_store.remove(user_id, book_id):
        raise HTTPException(status_code=404, detail="Book not in library.")
    rec_cache.invalidate(user_id)
    return {"status": "ok"}


# Canonical names for genre atoms that mean the same thing across Google Books
# and Open Library vocabularies — without folding, a GB "Science Fiction" book
# and an OL "sci-fi" book score ZERO genre overlap. Targets are chosen so the
# fiction/nonfiction signal survives the mapping (e.g. "fantasy fiction" →
# "fantasy", which _FICTION_GENRE_WORDS still recognises). Extend as bad recs
# surface new aliases (scripts/explain_similar.py shows each book's atoms).
_GENRE_SYNONYMS = {
    "sci-fi": "science fiction",
    "sci fi": "science fiction",
    "scifi": "science fiction",
    "science-fiction": "science fiction",
    "sf": "science fiction",
    "fantasy fiction": "fantasy",
    "horror fiction": "horror",
    "horror tales": "horror",
    "thrillers": "thriller",
    "suspense fiction": "thriller",
    "detective and mystery stories": "mystery",
    "mystery and detective": "mystery",
    "mystery & detective": "mystery",
    "mystery fiction": "mystery",
    "detective fiction": "mystery",
    "love stories": "romance",
    "romance fiction": "romance",
    "graphic novels": "graphic novel",
    "comics & graphic novels": "graphic novel",
    "ya": "young adult",
}

# Atoms that survive tag-splitting but are NOT genres: award/marketing labels
# (every genre has bestsellers) and Open Library person/topic *subject* facets
# (they describe who or what a book is about, not its genre). Scored as genres
# they manufacture cross-genre overlap — a "New York Times bestseller" self-help
# book scored 0.5 genre overlap with a NYT-bestseller epic fantasy (Peterson's
# "Beyond Order" surfaced under "The Way of Kings"), and romances matched a
# thriller via "married people" (under "Gone Girl"). Dropped in _genre_atoms and
# skipped as search queries. Extend as bad recs surface more (scripts/explain_similar).
_GENRE_NOISE_ATOMS = {
    "new york times bestseller",
    "married people", "husbands", "wives",
}

# Open Library facet prefixes. "genre:"/"subject:"/"form:" carry a usable genre
# once the prefix is stripped ("form:manga" -> "manga", "form:graphic novel");
# the rest ("series:Dungeon Crawler Carl", "person:...", "franchise:One Piece",
# "nyt:advice-how-to...=2021-03-21") are not genres and make terrible search
# queries or group headers, so they're dropped.
_FACET_KEEP = {"genre", "subject", "form"}
_FACET_DROP = {"series", "person", "place", "time", "award", "character",
               "franchise", "nyt"}
_FACET_RE = re.compile(r"^([a-z]+)\s*:\s*(.+)$", re.IGNORECASE)


def _genre_atoms(tags: list[str]) -> tuple[list[str], list[str]]:
    """Split tags into cleaned (specific, generic) genre atoms, lowercased.

    "Fiction / Fantasy / Action & Adventure" → specific ["fantasy",
    "action & adventure"], generic ["fiction"]. Nationality/language qualifiers
    are dropped so "Russian fantasy" reduces to "fantasy", and Open Library
    facet prefixes are stripped ("genre:litrpg" → "litrpg") or dropped
    ("series:..." → skipped).
    """
    specific: list[str] = []
    generic: list[str] = []
    for tag in tags:
        for part in re.split(r"[/,]", tag):
            atom = part.strip()
            if not atom:
                continue
            facet = _FACET_RE.match(atom)
            if facet:
                prefix = facet.group(1).lower()
                if prefix in _FACET_DROP:
                    continue
                if prefix in _FACET_KEEP:
                    atom = facet.group(2).strip()
            words = [w for w in atom.split() if w.lower() not in _LANG_QUALIFIERS]
            if not words:
                continue
            key = " ".join(words).lower().rstrip(".")  # OL tags like "fantasy fiction."
            key = _GENRE_SYNONYMS.get(key, key)
            if key in _GENRE_NOISE_ATOMS:
                continue  # award/marketing label or topical subject — not a genre
            if key in _GENERIC_GENRE_TAGS:
                generic.append(key)
            else:
                specific.append(key)
    return specific, generic


def _library_genre_profile(
    saved: list[Books], limit: int = 8,
) -> tuple[set[str], list[str]]:
    """Return (specific-genre profile, search queries) for a saved library.

    The profile is the set of specific genre atoms across the library, used to
    gate candidates to the right genres. The queries are those atoms ranked by
    how many saved books carry them (most representative genres first), falling
    back to generic genres, then "fiction".
    """
    counts: Counter[str] = Counter()
    generic: set[str] = set()
    for book in saved:
        spec, gen = _genre_atoms(book.tags)
        for atom in set(spec):
            counts[atom] += 1
        generic.update(gen)
    profile = set(counts)
    if counts:
        queries = [atom for atom, _ in counts.most_common(limit)]
    elif generic:
        queries = list(generic)[:limit]
    else:
        queries = ["fiction"]
    return profile, queries


def _genre_score(cand_genres: set[str], profile: set[str]) -> float:
    """Fraction of a candidate's genres that are in the library profile (0-1)."""
    if not cand_genres or not profile:
        return 0.0
    return len(cand_genres & profile) / len(cand_genres)


# Heuristic fiction/nonfiction classification from genre tags. Single tokens
# that strongly imply nonfiction (a "fiction" token always wins, so "science
# fiction" / "historical fiction" stay fiction).
_NONFICTION_WORDS = {
    "nonfiction", "biography", "autobiography", "memoir", "history", "handbook",
    "handbooks", "manual", "manuals", "cookbook", "cookery", "cooking",
    "reference", "essays", "criticism", "philosophy", "religion", "theology",
    "psychology", "sociology", "economics", "mathematics", "medicine", "medical",
    "nursing", "fitness", "nutrition", "exercise", "exercises", "recreation",
    "education", "textbook", "encyclopedia", "dictionary", "technique",
    "techniques", "instruction",
}
# Multi-word genre atoms that imply nonfiction (kept whole by _genre_atoms).
_NONFICTION_ATOMS = {
    "non-fiction", "self-help", "health & fitness", "sports & recreation",
    "body, mind & spirit", "study and teaching", "social science", "true crime",
    "biography & autobiography", "language arts & disciplines",
    "business & economics",
}
_FICTION_GENRE_WORDS = {
    "fantasy", "romance", "thriller", "mystery", "horror", "litrpg", "gamelit",
    "dystopian", "paranormal", "steampunk", "superhero", "supernatural", "noir",
    "western", "adventure",
}


def _fiction_signal(book: Books) -> int:
    """+1 if a book's genres read as fiction, -1 nonfiction, 0 unknown/mixed."""
    spec, gen = _genre_atoms(book.tags)
    fic = nonfic = False
    for atom in spec + gen:
        if atom in _NONFICTION_ATOMS:
            nonfic = True
        toks = set(atom.replace("&", " ").split())
        if "fiction" in toks or (toks & _FICTION_GENRE_WORDS):
            fic = True
        if toks & _NONFICTION_WORDS:
            nonfic = True
    if fic and not nonfic:
        return 1
    if nonfic and not fic:
        return -1
    return 0


def _library_is_fiction(saved: list[Books]) -> bool:
    """True if the saved library leans fiction (so nonfiction recs are noise)."""
    return sum(_fiction_signal(b) for b in saved) > 0


# Map MARC/3-letter and regional codes to a canonical 2-letter language code.
_LANG_ALIASES = {
    "eng": "en", "rus": "ru", "fre": "fr", "fra": "fr", "ger": "de", "deu": "de",
    "spa": "es", "ita": "it", "por": "pt", "jpn": "ja", "chi": "zh", "zho": "zh",
    "kor": "ko", "dut": "nl", "nld": "nl", "cze": "cs", "ces": "cs", "pol": "pl",
    "swe": "sv", "dan": "da", "fin": "fi", "nor": "no", "hun": "hu", "ukr": "uk",
    "tur": "tr", "ben": "bn", "est": "et", "ara": "ar", "heb": "he", "hin": "hi",
    "vie": "vi", "ind": "id", "tha": "th", "gre": "el", "ell": "el", "ron": "ro",
    "rum": "ro", "bul": "bg", "slo": "sk", "slk": "sk", "lit": "lt", "lav": "lv",
}


def _norm_lang(code: str | None) -> str:
    """Canonicalise a language code: 'eng'/'en-US' → 'en'. '' when unknown."""
    c = (code or "").strip().lower().split("-")[0]
    return _LANG_ALIASES.get(c, c)


def _book_language(book: Books) -> str | None:
    """Best guess at a book's language: title script is dispositive, with
    metadata only consulted when the title is Latin-script.

    Title script trumps metadata because Google Books and Open Library
    routinely label foreign-script titles with `language="en"` — the
    description is in English ("First published in 2005. | By 荒川弘.") so
    their classifier picks English even though the title is plainly Japanese.
    Trusting metadata over script let foreign editions slip into all-English
    library recommendations.
    """
    probe = (book.title or "").strip() or (book.description or "")
    # Script-based detection runs first. Hiragana/Katakana is a strong
    # Japanese signal; Hangul → Korean; Cyrillic → Russian. CJK ideographs
    # alone are ambiguous between Chinese and Japanese, so they only fire
    # when no Kana was present.
    if re.search(r"[぀-ヿ]", probe):      # Hiragana / Katakana → Japanese
        return "ja"
    if re.search(r"[가-힯]", probe):       # Hangul → Korean
        return "ko"
    if re.search(r"[一-鿿]", probe):       # CJK ideographs → Chinese/Japanese
        return "zh"
    if re.search(r"[Ѐ-ӿ]", probe):       # Cyrillic → Russian
        return "ru"
    # Latin-script title — now consult metadata, fall back to Latin/non-Latin
    # ratio if nothing is recorded.
    code = _norm_lang((book.metadata or {}).get("language"))
    if code:
        return code
    latin = len(re.findall(r"[A-Za-z]", probe))
    nonlatin = len(re.findall(r"[^\x00-\x7f]", probe))
    if not probe or latin >= nonlatin:
        return "en"
    return "non-latin"


def _languages_in_library(saved: list[Books]) -> set[str]:
    return {lang for b in saved if (lang := _book_language(b))}


def _apply_language_gate(candidates: list[Books], lib_langs: set[str]) -> list[Books]:
    """Keep candidates in the library's languages — unless that annihilates
    the pool, in which case the *stored language metadata* is what's wrong,
    not the candidates. (Open Library used to hand us a random translation
    language — 'ben' for Harry Potter — and one bad saved book then filtered
    every English candidate out, returning zero recommendations.)"""
    if not lib_langs:
        return candidates
    filtered = [c for c in candidates if _book_language(c) in lib_langs]
    if len(filtered) >= 10 or len(filtered) >= 0.05 * len(candidates):
        return filtered
    return candidates


def _norm_title(title: str) -> str:
    """Normalise a title for matching: lowercase, punctuation/whitespace folded.

    Lets us recognise a saved book in candidate results even when the edition's
    author spelling or punctuation differs (e.g. "Anarchist's" vs "Anarchist")."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", title.lower())).strip()


# Trailing volume marker: "... 11", "..., Vol. 1-18", "... Book 3", "... tome 1".
_VOLUME_RE = re.compile(
    r"[\s,:;#.\-]*\b(?:vol\.?|volume|book|bk\.?|tome|part|pt\.?|no\.?|#)?\s*"
    r"(\d+)(?:\s*[-–—]\s*\d+)?\s*$",
    re.IGNORECASE,
)

# Written-out volume numbers — "Book Three", "Volume Five". A volume keyword
# (book/volume/etc.) is required so titles like "The Three Musketeers" don't
# accidentally match.
_VOLUME_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20,
}
_VOLUME_RE_WORD = re.compile(
    r"[\s,:;#.\-]*\b(?:vol\.?|volume|book|bk\.?|tome|part|pt\.?)\s+"
    r"(one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)"
    r"\s*$",
    re.IGNORECASE,
)


def _split_series(title: str) -> tuple[str, int | None]:
    """Split a title into (normalised series key, volume number).

    "He Who Fights with Monsters 11" -> ("he who fights with monsters", 11);
    "Last Wish System, Vol. 1-18" -> ("last wish system", 1);
    "The Code of Survival Book Three" -> ("the code of survival", 3);
    a title with no trailing number -> (normalised title, None) so it stands
    on its own and isn't merged with anything."""
    raw = (title or "").strip()
    m = _VOLUME_RE.search(raw)
    if m:
        base = raw[: m.start()].strip(" ,:;#.-")
        if base:  # guard against a pure-number title like "2001" or "1984"
            return _norm_title(base), int(m.group(1))
    m = _VOLUME_RE_WORD.search(raw)
    if m:
        base = raw[: m.start()].strip(" ,:;#.-")
        if base:
            return _norm_title(base), _VOLUME_WORDS[m.group(1).lower()]
    return _norm_title(raw), None


# Volume nouns shared across the sequel-detection patterns below.
_VOL_NOUN = (
    r"book|novel|installment|instalment|volume|entry|tale|adventure|tome|sequel"
)

# Three patterns share OR-alternation. Each match means "this description is
# describing something other than a series entry point":
#   1. Ordinal + volume noun: "the fourth book", "fourth and final book"
#   2. Volume noun + word/digit: "Book Three of", "book 4 of"
#   3. Endpoint marker + volume noun: "the last book in the series",
#      "the final installment", "the concluding novel"
# Up to 3 filler words allowed between the modifier and the noun so phrases
# like "fourth and final book" still match.
_DESC_SEQUEL_RE = re.compile(
    r"\b(?:"
    r"(?:the\s+)?(?:second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"eleventh|twelfth|thirteenth|fourteenth|fifteenth|sixteenth|seventeenth|eighteenth|"
    r"nineteenth|twentieth)"
    rf"(?:\s+[a-z]+){{0,3}}\s+(?:{_VOL_NOUN})"
    r"|"
    r"(?:book|volume|vol\.?|installment|instalment|entry|tome)\s+"
    r"(?:two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
    r"[2-9]|1[0-9]|20)"
    r"|"
    r"(?:the\s+)?(?:last|final|concluding|closing)"
    rf"(?:\s+[a-z]+){{0,3}}\s+(?:{_VOL_NOUN})"
    r")\b",
    re.IGNORECASE,
)


def _desc_is_sequel(description: str) -> bool:
    """True when the description names this book as anything but a series
    entry point — ordinal position ("the fourth book"), explicit volume
    ("Book Three of"), or an endpoint marker ("the last book", "the final
    installment", "the concluding novel").

    For books whose title carries no volume marker (e.g. Adams' "So Long, and
    Thanks for All the Fish", or Erin Hunter's "A Dangerous Path") we can't
    recover the series' entry point from the candidate alone — the series
    name isn't reliably extractable from prose. Dropping the candidate is
    still better than recommending a stranger to start mid-series.

    False positives are guarded by requiring a book/novel/volume/installment-
    type noun ("the fourth chapter" / "the final goodbye" / "the fourth time
    he met her" don't match) and by intentionally omitting "first" / "1"
    (those describe entry points, which is what we want to keep).
    """
    if not description:
        return False
    return _DESC_SEQUEL_RE.search(description) is not None


def _entry_point_book(book: Books, lib_langs: set[str], saved_titles: set[str]) -> Books:
    """If `book` is a later volume of a series, look the series up by title and
    return its entry point (book 1 / lowest volume). Best-effort: returns the
    original book if a better entry point can't be found, isn't in a library
    language, or is already saved."""
    skey, vol = _split_series(book.title)
    if vol is None or vol <= 1:
        return book  # already an entry point / standalone
    try:
        results, _ = Fetcher(source=OPENLIB_ENDPOINT).fetch_page(
            skey, batch_size=40, category="title")
    except Exception:
        return book

    best, best_vol = book, vol
    for cand in results:
        ck, cv = _split_series(cand.title)
        if ck != skey:
            continue
        v = cv if cv is not None else 1
        if v >= best_vol:
            continue
        if lib_langs and _book_language(cand) not in lib_langs:
            continue
        if _norm_title(cand.title) in saved_titles:
            continue
        best, best_vol = cand, v
    return best


def _collapse_series_picks(
    scored: list[tuple[Books, float, object]],
) -> list[tuple[Books, float, object]]:
    """Collapse multi-volume series so each series appears once.

    Input is (book, score, payload) in best-score-first order. A series takes
    the rank (list position) of its best-scoring volume but is displayed as the
    earliest volume present, with that earliest volume's score — recommending
    "book 11" of a series is useless. The payload rides along from the
    best-scoring volume untouched: /library/recommend uses it to carry the
    best-matching saved-book index for the diversity step, /similar passes None.

    Shared by /similar and /library/recommend so the two stay aligned; the
    entry-point swap in _swap_to_entry_points then recovers book 1 when even the
    earliest present volume is still a sequel.
    """
    # key -> [book, score, volume, payload]
    rep: dict[str, list] = {}
    order: list[str] = []  # series keys in best-score-first order
    for book, score, payload in scored:
        skey, vol = _split_series(book.title)
        v = vol if vol is not None else 1
        if skey not in rep:
            order.append(skey)
            rep[skey] = [book, score, v, payload]
        elif v < rep[skey][2]:
            # earlier volume -> display this instead, but keep the rank and
            # payload of the best-scoring volume seen first.
            rep[skey][0], rep[skey][1], rep[skey][2] = book, score, v
    return [(rep[k][0], rep[k][1], rep[k][3]) for k in order]


def _swap_to_entry_points(
    entries: list[tuple[Books, float]], langs: set[str], titles: set[str],
) -> list[tuple[Books, float]]:
    """Swap any later-volume series entry for its book 1, concurrently.

    Recommend the series' entry point, not "book 11". Only later-volume titles
    trigger a provider lookup (see _entry_point_book), and they run in parallel,
    so the cost is small. `langs`/`titles` constrain the match to the source's
    language and exclude books the user already has. Shared by /similar and
    /library/recommend.
    """
    if not entries:
        return entries

    def _resolve(entry: tuple[Books, float]) -> tuple[Books, float]:
        book, score = entry
        return _entry_point_book(book, langs, titles), score

    with ThreadPoolExecutor(max_workers=min(8, len(entries))) as ex:
        return list(ex.map(_resolve, entries))


def _ensure_details(book: Books) -> bool:
    """Fill in a book's language and (for sparse Open Library works) its genres
    and full description from work detail. Mutates in place; returns True if
    anything changed, so callers can persist the enriched record."""
    changed = False
    meta = dict(book.metadata or {})
    if not meta.get("language"):
        lang = _book_language(book)
        if lang and lang != "non-latin":
            meta["language"] = lang
            changed = True
    book.metadata = meta

    if book.id.startswith("ol_/works/") and (not book.tags or len(book.description) < 60):
        desc, subjects = Fetcher(source=OPENLIB_ENDPOINT).fetch_work_detail(
            book.id[len("ol_"):])
        if subjects and not book.tags:
            book.tags = subjects[:5]
            changed = True
        if desc and len(desc) > len(book.description):
            book.description = desc
            changed = True
    return changed


# Cap how many tagless candidates we enrich with Open Library work detail, and
# how many of those fetches run at once, to bound the added request latency.
_ENRICH_LIMIT = 30
_ENRICH_WORKERS = 10


def _enrich_tagless_candidates(candidates: list[Books], saved: list[Books]) -> None:
    """In place: recover genres + a fuller description for promising tagless
    Open Library candidates.

    OL search results often have no subjects and only a one-line description.
    The candidates whose text best matches the library (cheap token overlap)
    are worth a detail fetch so the scorer can judge them on genre too, instead
    of text alone. Bounded by _ENRICH_LIMIT and run concurrently; best-effort.
    """
    lib_tokens: set[str] = set()
    for b in saved:
        lib_tokens |= _text_tokens(b)
    if not lib_tokens:
        return

    targets: list[tuple[int, Books]] = []
    for c in candidates:
        if c.tags or not c.id.startswith("ol_/works/"):
            continue
        overlap = len(_text_tokens(c) & lib_tokens)
        if overlap > 0:
            targets.append((overlap, c))
    if not targets:
        return
    targets.sort(key=lambda x: x[0], reverse=True)
    to_enrich = [c for _, c in targets[:_ENRICH_LIMIT]]

    detail_fetcher = Fetcher(source=OPENLIB_ENDPOINT)

    def _enrich(book: Books) -> None:
        desc, subjects = detail_fetcher.fetch_work_detail(book.id[len("ol_"):])
        if subjects:
            book.tags = subjects[:5]
        if desc and len(desc) > len(book.description):
            book.description = desc

    try:
        with ThreadPoolExecutor(max_workers=_ENRICH_WORKERS) as ex:
            list(ex.map(_enrich, to_enrich))
    except Exception:
        logger.warning("Library recommendation enrichment failed.", exc_info=True)


def _recommendation_exclusions(
    library_books: list[Books],
    liked_books: list[Books],
    disliked_books: list[Books],
) -> tuple[set[str], set[str]]:
    """(dedup keys, normalised titles) a recommendation must never return.

    Spans the FULL library (not just a scoped subset — a section run must not
    recommend back a book saved elsewhere) plus both feedback lists: disliked
    books are unwanted, and liked books are already known to the user — their
    job is to *re-weight* similar candidates, not to reappear as picks. Both
    keys and titles are matched so a different edition / source can't slip
    back in.
    """
    keys = {_dedup_key(b) for b in library_books}
    titles = {_norm_title(b.title) for b in library_books}
    for b in (*liked_books, *disliked_books):
        keys.add(_dedup_key(b))
        titles.add(_norm_title(b.title))
    return keys, titles


def _fetch_genre_candidates(query: str) -> list[Books]:
    """Fetch Google Books + Open Library results for one genre query."""
    out: list[Books] = []
    try:
        gb_books, _ = Fetcher(source=GOOGLE_ENDPOINT).fetch_google_page(
            query, max_results=40, category="genre")
        out.extend(gb_books)
    except Exception:
        logger.warning("Google Books library recommendation fetch failed for query %r.", query, exc_info=True)
    try:
        ol_books, _ = Fetcher(source=OPENLIB_ENDPOINT).fetch_page(
            query, batch_size=200, category="genre")
        out.extend(ol_books)
    except Exception:
        logger.warning("Open Library library recommendation fetch failed for query %r.", query, exc_info=True)
    return out


class RecommendScopeRequest(BaseModel):
    """Optional body for /library/recommend narrowing which saved books drive
    the recommendations. `section_id` scopes to a section; `book_ids` to an
    ad-hoc selection of saved books. Omit both (or send no body) for the
    whole library. When both are present, `section_id` wins."""
    section_id: int | None = None
    book_ids: list[str] | None = None


@app.post("/library/recommend", response_model=list[BookOut],
          summary="Get recommendations based on your library")
def library_recommend(
    scope_req: RecommendScopeRequest | None = Body(default=None),
    top_n: int = Query(20, ge=1, le=50),
    user_id: str = Depends(get_current_user_id),
):
    """Recommend books matching the saved library (or a scoped subset of it).

    Pipeline: back-fill saved-book details → fetch candidates by the scope's
    genres (concurrently) → keep only the scope's languages → enrich tagless
    candidates → score each by a blend of genre overlap and IDF-weighted
    description similarity against its best-matching saved book → diversify so
    no single saved book floods the results.

    Scoring, genre profile, and language gating run on the scoped books only;
    the exclusion sets (don't recommend back what the user already has) always
    span the full library plus dislikes.
    """
    _record_activity("recommend", user_id)

    library_books = library_store.all(user_id)
    if not library_books:
        raise HTTPException(status_code=400, detail="Your library is empty. Save some books first.")

    # --- Resolve the scope: a section, an ad-hoc selection, or everything ---
    scoped = False
    if scope_req and scope_req.section_id is not None:
        section = library_store.section_books(user_id, scope_req.section_id)
        if section is None:
            raise HTTPException(status_code=404, detail="Section not found.")
        if not section:
            raise HTTPException(
                status_code=400,
                detail="That section is empty. Add some books to it first.",
            )
        saved, scoped = section, True
    elif scope_req and scope_req.book_ids:
        wanted = set(scope_req.book_ids)
        saved = [b for b in library_books if b.id in wanted]
        if not saved:
            raise HTTPException(
                status_code=400,
                detail="None of the selected books are in your library.",
            )
        scoped = True
    else:
        saved = library_books

    # --- Fetch feedback (used by both the cache key and the scoring loop) ---
    feedback_pairs = feedback_store.all(user_id)
    liked_books = [b for b, k in feedback_pairs if k == "up"]
    disliked_books = [b for b, k in feedback_pairs if k == "down"]

    # --- Cache lookup: signature spans saved + liked + disliked (+ the scope
    # when one was requested) so any change — add, remove, flip a thumbs-up,
    # different section / selection — naturally produces a fresh key. Hits
    # skip the whole fetch/score/enrich pipeline.
    sig = RecommendationCache.signature(
        saved=(b.id for b in library_books),
        liked=(b.id for b in liked_books),
        disliked=(b.id for b in disliked_books),
        scope=(b.id for b in saved) if scoped else (),
    )
    cached = rec_cache.get(user_id, sig, top_n)
    if cached is not None:
        return cached

    # --- 0) Back-fill missing language/genres/descriptions on saved books ---
    # Books added before enrichment existed (or that were saved sparse) get
    # filled in once and persisted, so future runs are fast and the genre +
    # language signals below have something to work with.
    for b in saved:
        if _ensure_details(b):
            library_store.add(user_id, b)

    lib_langs = _languages_in_library(saved)

    # --- 1) Build the library's genre profile + search queries ---
    profile_specific, genre_queries = _library_genre_profile(saved)

    # --- 2) Fetch candidates (queries run concurrently), skipping books the
    # user already has an opinion on. The genre queries are fetched in
    # parallel: Open Library searches are slow, so issuing 8 of them
    # sequentially dominated request time.
    seen_keys, saved_titles = _recommendation_exclusions(
        library_books, liked_books, disliked_books,
    )
    candidates: list[Books] = []
    with ThreadPoolExecutor(max_workers=min(8, len(genre_queries))) as ex:
        fetched = list(ex.map(_fetch_genre_candidates, genre_queries))
    for books in fetched:
        for b in books:
            dk = _dedup_key(b)
            if dk in seen_keys or _norm_title(b.title) in saved_titles:
                continue  # already saved (by key or title), don't recommend it back
            seen_keys.add(dk)
            candidates.append(b)

    # --- 3) Keep only candidates in a language the library uses ---
    # An all-English library shouldn't surface Russian editions of classics.
    candidates = _apply_language_gate(candidates, lib_langs)

    if not candidates:
        return []

    # --- 4) Enrich the most promising tagless Open Library candidates ---
    # Recover real genres + a fuller description from OL work detail so books
    # that arrived tagless can still be scored on genre, not text alone.
    _enrich_tagless_candidates(candidates, saved)

    # --- 4a) Drop sequels whose volume position is only in the description ---
    # E.g. Adams' "So long, and thanks for all the fish" — the title gives no
    # hint, but the description says "the fourth book in the Hitchhiker's
    # Trilogy". Title-driven series collapsing can't recover book 1 for these
    # (we don't have the series name), so dropping them is better than
    # recommending a stranger to start mid-series. Sequels whose title DOES
    # carry a volume marker still flow through the normal collapse + swap.
    def _is_text_only_sequel(c: Books) -> bool:
        if _split_series(c.title)[1] is not None:
            return False  # title already declares volume — collapse logic handles it
        return _desc_is_sequel(c.description)

    candidates = [c for c in candidates if not _is_text_only_sequel(c)]

    if not candidates:
        return []

    # --- 5) Combined scoring: description match + genre match ---
    # description_score (IDF-weighted token-set F1 over title + description) is
    # always available; genre_score only when the book has tags. We blend the
    # two when genres exist and fall back to description-only when they don't —
    # so a tagless book is judged purely on how its description reads, while a
    # tagged book in none of the library's genres is dragged down by a near-zero
    # genre_score. Popularity stays a multiplicative tiebreaker.
    saved_text = [t for t in (_text_tokens(b) for b in saved) if t]
    if not saved_text:
        return []

    idf = _compute_token_idf(candidates, token_fn=_text_tokens)
    default_idf = math.log(len(candidates) + 1) + 1
    # Description outweighs genre (0.6 vs 0.4): the description is the primary
    # relevance signal, genre a strong secondary, popularity only a tiebreaker.
    W_GENRE, W_DESC = 0.4, 0.6
    # When the library is fiction, drop candidates whose genres read as
    # nonfiction (how-tos, histories) — they otherwise match on shared theme
    # words like "magic" or "combat".
    drop_nonfiction = _library_is_fiction(saved)

    # Feedback signals (see _feedback_modifier): thumbs-up/down re-weight each
    # candidate by description similarity to the nearest liked/disliked book
    # AND by whether it shares an author with a disliked book.
    liked_text = [t for t in (_text_tokens(b) for b in liked_books) if t]
    disliked_text = [t for t in (_text_tokens(b) for b in disliked_books) if t]
    liked_authors = _feedback_authors(liked_books)
    # An author both liked and disliked isn't penalised — the like cancels it.
    disliked_authors = _feedback_authors(disliked_books) - liked_authors
    has_feedback = bool(liked_text or disliked_text or liked_authors or disliked_authors)

    # (candidate, displayed_score, ranking_score, best-matching saved-book index)
    scored: list[tuple[Books, float, float, int]] = []
    for cand in candidates:
        if not _has_recommendable_content(cand):
            continue  # no description and no author — data junk, not a rec
        if drop_nonfiction and _fiction_signal(cand) < 0:
            continue
        cand_text = _text_tokens(cand)
        if not cand_text:
            continue
        best_idx, desc_score = 0, 0.0
        for i, src in enumerate(saved_text):
            v = _idf_weighted_f1(src, cand_text, idf, default_idf)
            if v > desc_score:
                desc_score, best_idx = v, i
        if desc_score <= 0:
            continue
        cand_genres = set(_genre_atoms(cand.tags)[0])
        if cand_genres:
            combined = W_GENRE * _genre_score(cand_genres, profile_specific) + W_DESC * desc_score
        else:
            combined = desc_score
        if has_feedback:
            combined *= _feedback_modifier(
                cand, cand_text, liked_text, disliked_text,
                liked_authors, disliked_authors, idf, default_idf,
            )
        pop = _book_popularity(cand)
        final = combined * (1.0 + 0.05 * pop)
        scored.append((cand, combined, final, best_idx))

    scored.sort(key=lambda x: x[2], reverse=True)

    # --- 6) Collapse each series to its entry point ---
    # The payload carried alongside each pick is the best-matching saved-book
    # index, which the diversity step below caps on.
    collapsed = _collapse_series_picks(
        [(cand, score, src_idx) for cand, score, _final, src_idx in scored]
    )

    # --- 7) Diversify: spread picks across the library's books and authors ---
    # Caps per saved book (so no single saved book dominates) and per author
    # (so several series by one author can't crowd everything else out).
    per_source_cap = max(2, math.ceil(top_n / len(saved_text)))
    author_cap = 3
    src_counts: dict[int, int] = defaultdict(int)
    author_counts: dict[str, int] = defaultdict(int)
    chosen: list[tuple[Books, float]] = []
    overflow: list[tuple[Books, float]] = []
    for cand, score, src_idx in collapsed:
        author = _norm_title(cand.authors[0]) if cand.authors else ""
        if src_counts[src_idx] < per_source_cap and (not author or author_counts[author] < author_cap):
            chosen.append((cand, score))
            src_counts[src_idx] += 1
            if author:
                author_counts[author] += 1
        else:
            overflow.append((cand, score))
        if len(chosen) >= top_n:
            break
    if len(chosen) < top_n:
        chosen.extend(overflow[: top_n - len(chosen)])

    # --- 8) Swap any later-volume series entry for its book 1 ---
    chosen = _swap_to_entry_points(chosen, lib_langs, saved_titles)

    result = [_to_out(book, relevance=round(score * 100, 1)) for book, score in chosen]
    # Empty results are NOT cached: they're usually a transient provider
    # failure (or a data bug), and serving cached emptiness until the next
    # library change would make "no recommendations" sticky.
    if result:
        rec_cache.put(user_id, sig, top_n, result)
    return result


# Words that mark a tag as a series name rather than a genre — "Hitchhiker's
# Trilogy", "Cosmere Saga", etc. A tag containing any of these gets ranked
# below tags that look like real genres, so it doesn't get picked as the
# display category for grouping.
_SERIES_HINT_WORDS = {
    "trilogy", "saga", "series", "cycle", "chronicles", "sequence",
    "tetralogy", "quintet", "duology",
}

# Broad genre-keyword vocabulary used to rank "is this tag genre-ish?". Not
# the source of truth for recommender scoring — just a tiebreaker for display.
_GENRE_VOCAB = {
    "fiction", "nonfiction", "fantasy", "romance", "thriller", "mystery",
    "horror", "litrpg", "gamelit", "dystopian", "paranormal", "steampunk",
    "cyberpunk", "superhero", "supernatural", "noir", "western", "adventure",
    "sci", "scifi", "science", "historical", "young", "adult", "memoir",
    "biography", "autobiography", "cookbook", "philosophy", "religion",
    "psychology", "sociology", "economics", "history", "poetry", "drama",
    "comedy", "humor", "satire", "crime", "spy", "espionage", "war",
    "military", "epic", "urban", "magical", "vampire", "zombie",
    "apocalyptic", "xianxia", "wuxia", "isekai", "litfic", "literary",
}

# Vague catch-all tags that technically pass the genre vocab check but make
# terrible display categories ("Fiction", "Literature" alone are uninformative).
_GENERIC_DISPLAY_TAGS = {"fiction", "nonfiction", "non-fiction", "general", "literature"}

# Description / title scan for a fallback genre when a book has no usable
# tags. Ordered most-specific first so "Sci-fi LitRPG" lands on "LitRPG" not
# "Science Fiction". Word boundaries enforced at match time.
_DERIVED_GENRES = [
    # Specific subgenres — checked first so "Sci-fi LitRPG" lands on LitRPG
    # instead of Science Fiction.
    ("LitRPG", ["litrpg", "lit-rpg", "lit rpg"]),
    ("GameLit", ["gamelit", "game lit"]),
    ("Xianxia", ["xianxia"]),
    ("Wuxia", ["wuxia"]),
    ("Isekai", ["isekai"]),
    ("Cyberpunk", ["cyberpunk"]),
    ("Steampunk", ["steampunk"]),
    ("Space Opera", ["space opera"]),
    ("Urban Fantasy", ["urban fantasy"]),
    ("Dark Fantasy", ["dark fantasy"]),
    ("Epic Fantasy", ["epic fantasy"]),
    ("High Fantasy", ["high fantasy"]),
    ("Sword and Sorcery", ["sword and sorcery", "sword & sorcery"]),
    ("Historical Fiction", ["historical fiction"]),
    ("Paranormal Romance", ["paranormal romance"]),
    ("Romantasy", ["romantasy"]),
    ("Cozy Mystery", ["cozy mystery"]),
    ("Police Procedural", ["police procedural"]),
    ("Hardboiled", ["hardboiled", "hard-boiled"]),
    ("Coming of Age", ["coming-of-age", "coming of age"]),
    ("Magical Realism", ["magical realism"]),
    ("Graphic Novel", ["graphic novel"]),
    ("Picture Book", ["picture book"]),
    ("Middle Grade", ["middle grade", "middle-grade"]),
    ("Young Adult", ["young adult"]),
    ("Children's Fiction", ["children's fiction", "children's novel", "children's book",
                            "children's books", "juvenile literature", "juvenile fiction"]),
    ("Manga", ["manga"]),
    ("Light Novel", ["light novel"]),
    ("Animal Fiction", ["warrior cats", "talking cats", "anthropomorphic animals", "anthropomorphic"]),
    # Mid-specificity standalone genres.
    ("Dystopian", ["dystopian", "dystopia"]),
    ("Paranormal", ["paranormal", "vampires", "ghosts", "witches", "werewolves"]),
    ("Superhero", ["superhero", "super-hero"]),
    ("Post-Apocalyptic", ["post-apocalyptic", "post apocalyptic", "postapocalyptic"]),
    ("Christian Fiction", ["christian fiction", "christian novel"]),
    # Broad fiction categories — last because they're easy to false-positive
    # against specific subgenres above. The extra single-word signals here are a
    # last resort before "Other": they only fire when a book carried no
    # recognised genre tag, so a book whose only OL subjects are content
    # descriptors ("magic", "imaginary places", "spaceships") still lands in a
    # real genre instead of an unhelpful subject header.
    ("Science Fiction", ["science fiction", "sci-fi", "sci fi", "scifi",
                         "spaceship", "spaceships", "starship", "starships",
                         "spacecraft", "galaxy", "galactic", "interstellar",
                         "extraterrestrial", "aliens", "android", "androids",
                         "cyborg", "space station", "outer space"]),
    ("Fantasy", ["fantasy", "witchcraft", "wizardry", "sorcery", "wizards",
                "wizard", "sorcerer", "sorceress", "magic", "magical",
                "imaginary places", "dragons", "elves", "dwarves",
                "enchanted", "spellcasting"]),
    ("Thriller", ["thriller", "psychological thriller"]),
    ("Mystery", ["mystery", "whodunit", "detective", "detectives"]),
    ("Horror", ["horror", "haunted", "haunting"]),
    ("Romance", ["romance", "love story", "romantic"]),
    ("Western", ["western novel", "old west"]),
    ("Action", ["action novel", "action-packed", "action adventure"]),
    ("Adventure", ["adventure novel", "adventure story", "epic adventure"]),
    ("Crime", ["crime novel", "crime fiction"]),
    ("War", ["war novel", "war fiction"]),
    # Nonfiction.
    ("Memoir", ["memoir"]),
    ("Biography", ["autobiography", "biography"]),
    ("Cookbook", ["cookbook"]),
    ("Self-Help", ["self-help", "self help"]),
    ("True Crime", ["true crime"]),
    ("Travel", ["travel guide", "travel memoir"]),
    ("History", ["history of "]),
    ("Essays", ["essays on", "collection of essays"]),
]


# OL subjects that are story ENTITIES, not genres: "Elder Wand (Imaginary
# object)", "Hermione Granger (Fictitious character)", and bare "the X" names
# ("the Elder Wand"). Terrible grouping headers — rank them below everything.
_ENTITY_TAG_RE = re.compile(
    r"\((imaginary|fictitious|fictional|legendary)\b", re.IGNORECASE,
)


def _tag_display_score(tag: str) -> int:
    """Higher = better candidate for the grouping category.

    -1: looks like a series name ("Trilogy of Four") or a story entity
        ("the Elder Wand", "Dementors (Imaginary creatures)")
     0: neutral / unrecognised
     1: too-generic genre word ("Fiction" on its own)
     3: specific genre tag ("Science Fiction", "LitRPG", "fantasy fiction")
    """
    lower = tag.lower().strip()
    if not lower:
        return -1
    if _ENTITY_TAG_RE.search(lower) or lower.startswith("the "):
        return -1
    words = set(re.findall(r"[a-z]+", lower))
    if words & _SERIES_HINT_WORDS:
        return -1
    if lower in _GENERIC_DISPLAY_TAGS:
        return 1
    if words & _GENRE_VOCAB:
        return 3
    return 0


def _derive_genre_from_text(text: str) -> str | None:
    """Pull a display genre out of free text — title + description.

    Used when a book arrives with no usable tags (Google Books occasionally
    omits categories entirely; Open Library often does for new releases).
    Returns None if nothing distinctive matched, in which case the caller
    falls back to 'Uncategorized'.
    """
    if not text:
        return None
    haystack = text.lower()
    for label, patterns in _DERIVED_GENRES:
        for pat in patterns:
            if re.search(r"\b" + re.escape(pat) + r"\b", haystack):
                return label
    return None


def _clean_tags_for_display(tags: list[str], description: str, title: str) -> list[str]:
    """Reshape tags for the response: strip facet noise, rank genres first,
    derive a fallback from text when nothing usable survives.

    - "series:Dungeon Crawler Carl" → dropped (display-only — _genre_atoms
      still sees the raw tag internally for scoring elsewhere).
    - "genre:LitRPG" → "LitRPG"
    - ["Trilogy of Four", "Science Fiction"] → ["Science Fiction", "Trilogy of Four"]
      so the frontend's `tags[0]` grouping picks the real genre.
    - A book with no tags but a description mentioning "Sci-fi LitRPG" →
      ["LitRPG"], so it doesn't end up under "Uncategorized".
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for tag in tags or []:
        atom = (tag or "").strip()
        if not atom:
            continue
        m = _FACET_RE.match(atom)
        if m:
            prefix = m.group(1).lower()
            if prefix in _FACET_DROP:
                continue  # series:/person:/etc — display noise
            if prefix in _FACET_KEEP:
                atom = m.group(2).strip()
                if not atom:
                    continue
        key = atom.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(atom)

    # Stable-sort by score so genre tags come first without otherwise
    # disturbing OL/GB's own ordering among equally-scored tags.
    cleaned.sort(key=lambda t: -_tag_display_score(t))

    # Still nothing display-worthy? Scan title + description + the raw tags
    # for a known genre keyword — OL entity-ish subjects ("Witches",
    # "vampires") aren't genre headers themselves but they signal one, and a
    # tag-less Google Books result usually names the genre in its description.
    if not cleaned or _tag_display_score(cleaned[0]) <= 0:
        derived = _derive_genre_from_text(
            f"{title} {description} {' '.join(tags or [])}"
        )
        if derived:
            # Prepend so the frontend's first-tag grouping uses it; keep any
            # other tags around so they still render as chips.
            if derived.lower() not in seen:
                cleaned.insert(0, derived)

    return cleaned


def _to_out(b, relevance: float | None = None) -> dict:
    out = {
        "id": b.id,
        "title": b.title,
        "authors": b.authors,
        "description": b.description,
        "tags": _clean_tags_for_display(b.tags, b.description, b.title),
        "metadata": b.metadata,
    }
    if relevance is not None:
        # Displayed as a "match %". The ranking score can exceed 1.0 — the
        # feedback modifier (up to 1.5x in /library/recommend) and the
        # popularity multiplier (/similar) both push it past a clean match —
        # so cap the number a user sees at 100 rather than showing "114%".
        out["relevance"] = min(relevance, 100.0)
    return out


# HEAD is what uptime monitors (UptimeRobot et al.) send by default — without
# it the homepage answers 405 and monitoring reports the site "down" while
# browsers (GET) work fine. FileResponse handles HEAD natively (headers only).
@app.get("/", include_in_schema=False)
@app.head("/", include_in_schema=False)
def serve_frontend():
    return FileResponse(FRONTEND_DIR / "index.html")
