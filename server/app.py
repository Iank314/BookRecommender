"""FastAPI server exposing the book recommender as a REST API."""

from __future__ import annotations

import math
import logging
import re
import uuid
from pathlib import Path
from typing import Literal

from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, constr

from server.fetcher.fetcher import Fetcher, GOOGLE_ENDPOINT, OPENLIB_ENDPOINT
from server.models.book import Books
from server.recommender.recommendation_engine import RecommendationEngine
from server.recommender.recommender import Recommender
from server.storage.library_db import LibraryStore

USER_COOKIE = "bookrec_user_id"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
logger = logging.getLogger(__name__)
NonEmptyStr = constr(strip_whitespace=True, min_length=1)
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

    # --- 3) Score candidates via token-set F1 over title + tags + description ---
    # TF-IDF cosine was unreliable here: source and candidate often come from
    # different sources (Google Books vs Open Library) with different tag
    # vocabularies and wildly different description lengths, so the cosine
    # collapsed near zero. Token-set F1 (recall × precision) is more
    # interpretable: it asks how much of each book's vocabulary the other
    # captures, regardless of doc length.
    source_book = Books(
        id="__source__",
        title=req.title,
        authors=req.authors,
        description=req.description,
        tags=req.tags,
        metadata={},
    )
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
    top = scored[: req.top_n]
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


def _compute_token_idf(books: list[Books]) -> dict[str, float]:
    """IDF for every token across the candidate corpus."""
    df: dict[str, int] = {}
    for book in books:
        for tok in _book_tokens(book):
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
# Personal Library — save, list, remove, get recommendations
# ------------------------------------------------------------------ #
library_store = LibraryStore()


def get_user_id(
    response: Response,
    bookrec_user_id: str | None = Cookie(default=None),
) -> str:
    """Return the caller's user id, minting and setting a cookie on first visit."""
    if bookrec_user_id:
        return bookrec_user_id
    new_id = uuid.uuid4().hex
    response.set_cookie(
        key=USER_COOKIE,
        value=new_id,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return new_id


class SaveBookRequest(BaseModel):
    id: str
    title: str
    authors: list[str] = Field(default_factory=list)
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


@app.post("/library/add", response_model=BookOut, summary="Save a book to your library")
def library_add(req: SaveBookRequest, user_id: str = Depends(get_user_id)):
    book = Books(
        id=req.id, title=req.title, authors=req.authors,
        description=req.description, tags=req.tags, metadata=req.metadata,
    )
    library_store.add(user_id, book)
    return _to_out(book)


@app.delete("/library/{book_id}", summary="Remove a book from your library")
def library_remove(book_id: str, user_id: str = Depends(get_user_id)):
    if not library_store.remove(user_id, book_id):
        raise HTTPException(status_code=404, detail="Book not in library.")
    return {"status": "ok"}


@app.get("/library", response_model=list[BookOut], summary="List your saved books")
def library_list(user_id: str = Depends(get_user_id)):
    return [_to_out(b) for b in library_store.all(user_id)]


@app.post("/library/recommend", response_model=list[BookOut],
          summary="Get recommendations based on your library")
def library_recommend(
    top_n: int = Query(20, ge=1, le=50),
    user_id: str = Depends(get_user_id),
):
    """Build TF-IDF from saved books + fetched candidates, return recommendations."""
    saved = library_store.all(user_id)
    if not saved:
        raise HTTPException(status_code=400, detail="Your library is empty. Save some books first.")

    # Collect genre tags from all saved books
    all_tags: set[str] = set()
    for b in saved:
        all_tags.update(b.tags)

    # Filter to genre-level tags (skip short/generic ones)
    genre_queries = [t for t in all_tags if len(t) > 3][:5]
    if not genre_queries:
        genre_queries = ["fiction"]

    # Build exclusion set from saved book titles
    saved_keys: set[str] = set()
    for b in saved:
        saved_keys.add(_dedup_key(b))

    # Fetch candidates
    all_candidates = []
    seen_keys = set(saved_keys)

    for query in genre_queries:
        # Google Books
        try:
            gb_fetcher = Fetcher(source=GOOGLE_ENDPOINT)
            gb_books, _ = gb_fetcher.fetch_google_page(
                query, max_results=40, category="genre",
            )
            for b in gb_books:
                dk = _dedup_key(b)
                if dk not in seen_keys:
                    seen_keys.add(dk)
                    all_candidates.append(b)
        except Exception:
            logger.warning("Google Books library recommendation fetch failed for query %r.", query, exc_info=True)

        # Open Library
        try:
            ol_fetcher = Fetcher(source=OPENLIB_ENDPOINT)
            ol_books, _ = ol_fetcher.fetch_page(
                query, batch_size=200, category="genre",
            )
            for b in ol_books:
                dk = _dedup_key(b)
                if dk not in seen_keys:
                    seen_keys.add(dk)
                    all_candidates.append(b)
        except Exception:
            logger.warning("Open Library library recommendation fetch failed for query %r.", query, exc_info=True)

    if not all_candidates:
        return []

    # Build TF-IDF index from candidates
    fetcher = Fetcher(source=OPENLIB_ENDPOINT)
    engine = RecommendationEngine()
    rec = Recommender(fetcher, engine)

    for b in all_candidates:
        rec.library.add(b)

    cleaned = {}
    for b in rec.library.all():
        parts = [b.title, " ".join(b.tags), b.description]
        cleaned[b.id] = rec.preprocessor.process(" ".join(parts))
    tags_map = {b.id: b.tags for b in rec.library.all()}

    try:
        features, ids = rec.extractor.fit_transform(cleaned, tags_map)
        rec.engine.fit(features, ids)
    except Exception:
        logger.warning("Failed to build library recommendation feature index.", exc_info=True)
        return []

    # Build query from all saved books' descriptions + tags
    query_parts = []
    for b in saved:
        query_parts.extend(b.tags)
        if b.description:
            query_parts.append(b.description)

    scored = rec.recommend_by_text(" ".join(query_parts), top_n=top_n)
    return [_to_out(book, relevance=score) for book, score in scored]


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
