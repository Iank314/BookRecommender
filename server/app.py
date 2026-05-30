"""FastAPI server exposing the book recommender as a REST API."""

from __future__ import annotations

import math
import logging
import os
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, constr

from server.auth_throttle import LoginThrottle
from server.cache.rec_cache import RecommendationCache
from server.fetcher.fetcher import Fetcher, GOOGLE_ENDPOINT, OPENLIB_ENDPOINT
from server.models.book import Books
from server.recommender.recommendation_engine import RecommendationEngine
from server.recommender.recommender import Recommender
from server.storage.feedback_db import FeedbackKind, FeedbackStore
from server.storage.library_db import LibraryStore
from server.storage.users_db import UserStore, UsernameTakenError

SESSION_COOKIE = "bookrec_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year
# Set BOOKREC_SECURE_COOKIES=true in production (Fly/any HTTPS host) so the
# session cookie is only sent over HTTPS. Off by default so local HTTP dev
# keeps working; auto-detection isn't reliable behind reverse proxies that
# terminate TLS at the edge.
SESSION_COOKIE_SECURE = os.environ.get("BOOKREC_SECURE_COOKIES", "").lower() in (
    "1", "true", "yes", "on",
)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
logger = logging.getLogger(__name__)
NonEmptyStr = constr(strip_whitespace=True, min_length=1)
Username = constr(strip_whitespace=True, min_length=3, max_length=32)
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


class BookOut(BaseModel):
    id: str
    title: str
    authors: list[str]
    description: str
    tags: list[str]
    metadata: dict
    relevance: float | None = None


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
def search(req: SearchRequest):
    """Fetch from Google Books + Open Library, score, return paginated results."""
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
    title: str
    authors: list[str] = Field(default_factory=list)
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    top_n: int = Field(20, ge=1, le=50)


@app.post("/similar", response_model=list[BookOut], summary="Find similar books")
def find_similar(req: SimilarRequest):
    """Given a book's info, fetch related books and rank by TF-IDF similarity."""

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
    for tag in req.tags:
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

    genre_queries = specific_queries or generic_queries or ["fiction"]

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
        return []

    # --- 3) Build the source book; keep only candidates in its language ---
    # An English source shouldn't surface Russian editions of the same trope.
    source_book = Books(
        id="__source__",
        title=req.title,
        authors=req.authors,
        description=req.description,
        tags=req.tags,
        metadata={},
    )
    src_lang = _book_language(source_book)
    if src_lang and src_lang != "non-latin":
        all_books = [b for b in all_books if _book_language(b) == src_lang]
        if not all_books:
            return []

    # --- 4) Score candidates via token-set F1 over title + tags + description ---
    # TF-IDF cosine was unreliable here: source and candidate often come from
    # different sources (Google Books vs Open Library) with different tag
    # vocabularies and wildly different description lengths, so the cosine
    # collapsed near zero. Token-set F1 (recall × precision) is more
    # interpretable: it asks how much of each book's vocabulary the other
    # captures, regardless of doc length.
    src_tokens = _book_tokens(source_book)
    if not src_tokens:
        return []

    # IDF over the candidate corpus: distinctive words like "nightmare" or
    # "cultivation" carry high weight, while filler like "world"/"young"/"life"
    # gets near-zero weight. This stops verbose-description classics from
    # matching everything just because they share common vocabulary.
    idf = _compute_token_idf(all_books)
    default_idf = math.log(len(all_books) + 1) + 1  # weight for unseen source tokens

    scored: list[tuple[Books, float]] = []
    for cand in all_books:
        cand_tokens = _book_tokens(cand)
        sim = _idf_weighted_f1(src_tokens, cand_tokens, idf, default_idf)
        if sim <= 0:
            continue
        # Popularity is a gentle tiebreaker only — at 0.4 it was outranking
        # similarity and surfacing widely-edited classics.
        pop = _book_popularity(cand)
        final = sim + (1.0 - sim) * 0.1 * pop
        scored.append((cand, final))

    scored.sort(key=lambda x: x[1], reverse=True)

    # --- 5) Collapse each series to its earliest-volume entry ---
    # If several volumes of one series rank, keep only the earliest present —
    # the series holds the rank of its best-scoring volume but is displayed
    # as the earliest volume seen, so we don't recommend "book 11".
    series_rep: dict[str, tuple[Books, float, int]] = {}  # key -> (book, score, volume)
    order: list[str] = []  # series keys in best-score-first order
    for cand, score in scored:
        skey, vol = _split_series(cand.title)
        v = vol if vol is not None else 1
        if skey not in series_rep:
            order.append(skey)
            series_rep[skey] = (cand, score, v)
        elif v < series_rep[skey][2]:
            series_rep[skey] = (cand, score, v)

    top = [(series_rep[k][0], series_rep[k][1]) for k in order][: req.top_n]

    # --- 6) Swap any later-volume entry for its book 1 ---
    # Recommend the series' entry point, not "book 11". Only later-volume
    # titles trigger a lookup, and they run concurrently, so the cost is small.
    src_titles = {_norm_title(req.title)}
    src_langs = {src_lang} if src_lang and src_lang != "non-latin" else set()

    def _resolve(entry: tuple[Books, float]) -> tuple[Books, float]:
        book, score = entry
        return _entry_point_book(book, src_langs, src_titles), score

    if top:
        with ThreadPoolExecutor(max_workers=min(8, len(top))) as ex:
            top = list(ex.map(_resolve, top))

    return [_to_out(book, relevance=round(sim * 100, 1)) for book, sim in top]


_SIM_STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "are", "was", "were",
    "has", "had", "have", "but", "not", "all", "any", "you", "your", "their",
    "they", "them", "his", "her", "him", "she", "who", "what", "when", "where",
    "how", "why", "into", "out", "than", "then", "also", "more", "most", "one",
    "two", "new", "book", "books", "edition", "vol", "volume", "general",
    "first", "second", "third", "english",
    # Genre-noise: every fiction book shares these so they add no signal.
    "fiction", "novel", "novels", "story", "stories", "tale", "tales",
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


def _book_tokens(book: Books) -> set[str]:
    """Lowercased word tokens from title + tags + description, minus stopwords."""
    parts = [book.title, " ".join(book.tags), book.description]
    text = " ".join(parts).lower()
    return {
        tok for tok in re.findall(r"[a-z]+", text)
        if len(tok) > 2 and tok not in _SIM_STOPWORDS
    }


def _text_tokens(book: Books) -> set[str]:
    """Token set from title + description only — genre tags excluded.

    Used for the description-similarity signal, kept separate from the genre
    signal so genre words don't get counted twice.
    """
    text = f"{book.title} {book.description}".lower()
    return {
        tok for tok in re.findall(r"[a-z]+", text)
        if len(tok) > 2 and tok not in _SIM_STOPWORDS
    }


def _token_f1(a: set[str], b: set[str]) -> float:
    """Token-set F1: harmonic mean of recall and precision (unweighted)."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    recall = inter / len(a)
    precision = inter / len(b)
    return 2 * recall * precision / (recall + precision)


def _compute_token_idf(books: list[Books], token_fn=_book_tokens) -> dict[str, float]:
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
rec_cache = RecommendationCache()
login_throttle = LoginThrottle()


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


class AuthRequest(BaseModel):
    username: Username
    password: Password


class AuthResponse(BaseModel):
    username: str


@app.post("/auth/register", response_model=AuthResponse, summary="Create an account")
def auth_register(req: AuthRequest, response: Response):
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
    return AuthResponse(username=user_store.get_username(user_id) or req.username)


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
    return AuthResponse(username=username)


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


@app.delete("/library/{book_id}", summary="Remove a book from your library")
def library_remove(book_id: str, user_id: str = Depends(get_current_user_id)):
    if not library_store.remove(user_id, book_id):
        raise HTTPException(status_code=404, detail="Book not in library.")
    rec_cache.invalidate(user_id)
    return {"status": "ok"}


@app.get("/library", response_model=list[BookOut], summary="List your saved books")
def library_list(user_id: str = Depends(get_current_user_id)):
    return [_to_out(b) for b in library_store.all(user_id)]


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


@app.delete("/library/feedback/{book_id}", summary="Clear feedback on a book")
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


# Open Library facet prefixes. "genre:"/"subject:" carry a usable genre once the
# prefix is stripped; the rest ("series:Dungeon Crawler Carl", "person:...") are
# not genres and make terrible search queries, so they're dropped.
_FACET_KEEP = {"genre", "subject"}
_FACET_DROP = {"series", "person", "place", "time", "award", "character"}
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
            key = " ".join(words).lower()
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
    "kor": "ko", "dut": "nl", "nld": "nl",
}


def _norm_lang(code: str | None) -> str:
    """Canonicalise a language code: 'eng'/'en-US' → 'en'. '' when unknown."""
    c = (code or "").strip().lower().split("-")[0]
    return _LANG_ALIASES.get(c, c)


def _book_language(book: Books) -> str | None:
    """Best guess at a book's language: stored code if present, else inferred
    from the script of its title.

    The title is used — not the description — because Open Library synthesises
    an *English* description ("Subjects: ... First published in ...") even for
    foreign books, which would otherwise make a Japanese or Russian title read
    as English.
    """
    code = _norm_lang((book.metadata or {}).get("language"))
    if code:
        return code
    probe = (book.title or "").strip() or (book.description or "")
    if re.search(r"[぀-ヿ]", probe):      # Hiragana / Katakana → Japanese
        return "ja"
    if re.search(r"[가-힯]", probe):       # Hangul → Korean
        return "ko"
    if re.search(r"[一-鿿]", probe):       # CJK ideographs → Chinese/Japanese
        return "zh"
    if re.search(r"[Ѐ-ӿ]", probe):       # Cyrillic → Russian
        return "ru"
    latin = len(re.findall(r"[A-Za-z]", probe))
    nonlatin = len(re.findall(r"[^\x00-\x7f]", probe))
    if not probe or latin >= nonlatin:
        return "en"
    return "non-latin"


def _languages_in_library(saved: list[Books]) -> set[str]:
    return {lang for b in saved if (lang := _book_language(b))}


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


def _split_series(title: str) -> tuple[str, int | None]:
    """Split a title into (normalised series key, volume number).

    "He Who Fights with Monsters 11" -> ("he who fights with monsters", 11);
    "Last Wish System, Vol. 1-18" -> ("last wish system", 1);
    a title with no trailing number -> (normalised title, None) so it stands
    on its own and isn't merged with anything."""
    raw = (title or "").strip()
    m = _VOLUME_RE.search(raw)
    if m:
        base = raw[: m.start()].strip(" ,:;#.-")
        if base:  # guard against a pure-number title like "2001" or "1984"
            return _norm_title(base), int(m.group(1))
    return _norm_title(raw), None


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


@app.post("/library/recommend", response_model=list[BookOut],
          summary="Get recommendations based on your library")
def library_recommend(
    top_n: int = Query(20, ge=1, le=50),
    user_id: str = Depends(get_current_user_id),
):
    """Recommend books matching the saved library.

    Pipeline: back-fill saved-book details → fetch candidates by the library's
    genres (concurrently) → keep only the library's languages → enrich tagless
    candidates → score each by a blend of genre overlap and IDF-weighted
    description similarity against its best-matching saved book → diversify so
    no single saved book floods the results.
    """
    saved = library_store.all(user_id)
    if not saved:
        raise HTTPException(status_code=400, detail="Your library is empty. Save some books first.")

    # --- Fetch feedback (used by both the cache key and the scoring loop) ---
    feedback_pairs = feedback_store.all(user_id)
    liked_books = [b for b, k in feedback_pairs if k == "up"]
    disliked_books = [b for b, k in feedback_pairs if k == "down"]

    # --- Cache lookup: signature spans saved + liked + disliked so any
    # change (add, remove, flip a thumbs-up to thumbs-down) naturally
    # produces a fresh key. Hits skip the whole fetch/score/enrich pipeline.
    sig = RecommendationCache.signature(
        saved=(b.id for b in saved),
        liked=(b.id for b in liked_books),
        disliked=(b.id for b in disliked_books),
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

    # --- 2) Fetch candidates (queries run concurrently), skipping saved + disliked ---
    # The genre queries are fetched in parallel: Open Library searches are slow,
    # so issuing 8 of them sequentially dominated request time.
    # Disliked books share the same exclusion path as saved ones — by dedup
    # key and by normalised title — so they can't slip back in via a different
    # edition / source.
    seen_keys = {_dedup_key(b) for b in saved}
    seen_keys.update(_dedup_key(b) for b in disliked_books)
    saved_titles = {_norm_title(b.title) for b in saved}
    saved_titles.update(_norm_title(b.title) for b in disliked_books)
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
    if lib_langs:
        candidates = [c for c in candidates if _book_language(c) in lib_langs]

    if not candidates:
        return []

    # --- 4) Enrich the most promising tagless Open Library candidates ---
    # Recover real genres + a fuller description from OL work detail so books
    # that arrived tagless can still be scored on genre, not text alone.
    _enrich_tagless_candidates(candidates, saved)

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
    W_GENRE, W_DESC = 0.5, 0.5
    # When the library is fiction, drop candidates whose genres read as
    # nonfiction (how-tos, histories) — they otherwise match on shared theme
    # words like "magic" or "combat".
    drop_nonfiction = _library_is_fiction(saved)

    # Feedback signals: a thumbs-up nudges similar candidates up, a thumbs-down
    # nudges similar ones down. Same IDF-weighted F1 we already trust for
    # description scoring, against the best-matching liked / disliked book.
    # Modifier is multiplicative on `combined` and clamped so a single signal
    # can't completely override the underlying genre/description match.
    liked_text = [t for t in (_text_tokens(b) for b in liked_books) if t]
    disliked_text = [t for t in (_text_tokens(b) for b in disliked_books) if t]
    FB_ALPHA, FB_BETA = 0.5, 0.5
    FB_MOD_LO, FB_MOD_HI = 0.1, 1.5

    # (candidate, displayed_score, ranking_score, best-matching saved-book index)
    scored: list[tuple[Books, float, float, int]] = []
    for cand in candidates:
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
        if liked_text or disliked_text:
            up_sim = max(
                (_idf_weighted_f1(t, cand_text, idf, default_idf) for t in liked_text),
                default=0.0,
            )
            down_sim = max(
                (_idf_weighted_f1(t, cand_text, idf, default_idf) for t in disliked_text),
                default=0.0,
            )
            fb_mod = max(FB_MOD_LO, min(FB_MOD_HI, 1.0 + FB_ALPHA * up_sim - FB_BETA * down_sim))
            combined *= fb_mod
        pop = _book_popularity(cand)
        final = combined * (1.0 + 0.05 * pop)
        scored.append((cand, combined, final, best_idx))

    scored.sort(key=lambda x: x[2], reverse=True)

    # --- 6) Collapse each series to its entry point ---
    # If several volumes of a series rank, show only the earliest one present
    # (book 1 / lowest volume) — recommending "book 11" of a series is useless.
    # The series keeps the rank of its best-scoring volume but is displayed as
    # the entry-point book.
    series_rep: dict[str, tuple[Books, float, int]] = {}  # key -> (book, score, volume)
    series_src: dict[str, int] = {}
    order: list[str] = []  # series keys in best-score-first order
    for cand, score, _final, src_idx in scored:
        skey, vol = _split_series(cand.title)
        v = vol if vol is not None else 1
        if skey not in series_rep:
            order.append(skey)
            series_rep[skey] = (cand, score, v)
            series_src[skey] = src_idx
        elif v < series_rep[skey][2]:
            series_rep[skey] = (cand, score, v)  # earlier volume -> display this instead

    # --- 7) Diversify: spread picks across the library's books and authors ---
    # Caps per saved book (so no single saved book dominates) and per author
    # (so several series by one author can't crowd everything else out).
    per_source_cap = max(2, math.ceil(top_n / len(saved_text)))
    author_cap = 3
    src_counts: dict[int, int] = defaultdict(int)
    author_counts: dict[str, int] = defaultdict(int)
    chosen: list[tuple[Books, float]] = []
    overflow: list[tuple[Books, float]] = []
    for skey in order:
        cand, score, _v = series_rep[skey]
        src_idx = series_src[skey]
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
    # Recommend the entry point, not "book 11". Only later-volume titles trigger
    # a lookup, and they run concurrently, so the cost is small.
    def _resolve(entry: tuple[Books, float]) -> tuple[Books, float]:
        book, score = entry
        return _entry_point_book(book, lib_langs, saved_titles), score

    if chosen:
        with ThreadPoolExecutor(max_workers=min(8, len(chosen))) as ex:
            chosen = list(ex.map(_resolve, chosen))

    result = [_to_out(book, relevance=round(score * 100, 1)) for book, score in chosen]
    rec_cache.put(user_id, sig, top_n, result)
    return result


def _to_out(b, relevance: float | None = None) -> dict:
    out = {
        "id": b.id,
        "title": b.title,
        "authors": b.authors,
        "description": b.description,
        "tags": b.tags,
        "metadata": b.metadata,
    }
    if relevance is not None:
        out["relevance"] = relevance
    return out


@app.get("/", include_in_schema=False)
def serve_frontend():
    return FileResponse(FRONTEND_DIR / "index.html")
