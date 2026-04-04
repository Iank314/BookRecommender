"""FastAPI server exposing the book recommender as a REST API."""

from __future__ import annotations

import math
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from server.fetcher.fetcher import Fetcher, GOOGLE_ENDPOINT, OPENLIB_ENDPOINT
from server.recommender.recommendation_engine import RecommendationEngine
from server.recommender.recommender import Recommender

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

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
    query: str = "coming-of-age fantasy"
    max_results: int = 40
    source: str = GOOGLE_ENDPOINT
    category: str = "general"


class SearchRequest(BaseModel):
    query: str
    category: str = "general"  # "title", "author", "genre", or "general"
    top_n: int = 100
    page: int = 1
    page_size: int = 20


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

    # --- 1) Google Books (up to 120 results via 3 pages of 40) ---
    gb_fetcher = Fetcher(source=GOOGLE_ENDPOINT)
    for start_idx in range(0, 120, 40):
        try:
            gb_books, gb_total = gb_fetcher.fetch_google_page(
                req.query, max_results=40,
                start_index=start_idx, category=req.category,
            )
        except Exception:
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
    # Base: how well do the tags match?
    base = 0.0
    query_words = set(query_lower.split())
    for tag in book.tags:
        tag_lower = tag.lower()
        if query_lower == tag_lower:
            base = 70.0
            break
        if query_lower in tag_lower:
            base = max(base, 65.0)
        tag_words = set(tag_lower.split())
        if query_words & tag_words:
            overlap = len(query_words & tag_words) / len(query_words)
            base = max(base, 55.0 * overlap)

    # Check title and description for the genre keyword
    combined = f"{book.title} {book.description}".lower()
    if query_lower in combined:
        base = max(base, 60.0)

    # Open Library's subject search already pre-filters by genre,
    # so even books with empty tags are relevant — give them a base score
    if from_subject_search and base == 0:
        base = 55.0

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
    authors: list[str] = []
    description: str = ""
    tags: list[str] = []
    top_n: int = 20


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

    # Only keep tags that look like genres, not proper nouns or title references
    genre_queries = []
    for tag in req.tags:
        tag_lower = tag.lower()
        # Skip if the tag is basically the title or author
        tag_words_set = set(tag_lower.split())
        if tag_words_set & title_words and len(tag_words_set & title_words) / max(len(tag_words_set), 1) > 0.5:
            continue
        if tag_words_set & author_words:
            continue
        genre_queries.append(tag)

    # If no genre tags survived filtering, use broad genre terms
    if not genre_queries:
        genre_queries = ["fiction"]

    # Limit to top 3 genre queries
    genre_queries = genre_queries[:3]

    # --- 2) Fetch candidates using genre queries ---
    all_books = []
    seen_keys: set[str] = set()
    source_key = _dedup_key_raw(req.title, req.authors[0] if req.authors else "")
    seen_keys.add(source_key)

    # Words to check for "about the source" filtering
    filter_words = title_words | author_words

    for query in genre_queries:
        # Google Books
        gb_fetcher = Fetcher(source=GOOGLE_ENDPOINT)
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
            pass

        # Open Library
        ol_fetcher = Fetcher(source=OPENLIB_ENDPOINT)
        try:
            ol_books, _ = ol_fetcher.fetch_page(
                query, batch_size=200, category="genre",
            )
            for b in ol_books:
                if _is_about_source(b, filter_words):
                    continue
                dk = _dedup_key(b)
                if dk not in seen_keys:
                    seen_keys.add(dk)
                    all_books.append(b)
        except Exception:
            pass

    if not all_books:
        return []

    # --- 3) Build TF-IDF index from candidates ---
    fetcher = Fetcher(source=OPENLIB_ENDPOINT)
    engine = RecommendationEngine()
    rec = Recommender(fetcher, engine)

    for b in all_books:
        rec.library.add(b)

    cleaned = {}
    for b in rec.library.all():
        parts = [b.title, " ".join(b.tags), b.description]
        cleaned[b.id] = rec.preprocessor.process(" ".join(parts))
    tags = {b.id: b.tags for b in rec.library.all()}

    try:
        features, ids = rec.extractor.fit_transform(cleaned, tags)
        rec.engine.fit(features, ids)
    except Exception:
        return []

    # --- 4) Query using ONLY description + genre tags (not title/author) ---
    query_parts = list(genre_queries)
    if req.description:
        query_parts.append(req.description)
    query_text = " ".join(query_parts)

    scored = rec.recommend_by_text(query_text, top_n=req.top_n)

    return [_to_out(book, relevance=score) for book, score in scored]


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
from server.models.library import Library
from server.models.book import Books

user_library = Library()


class SaveBookRequest(BaseModel):
    id: str
    title: str
    authors: list[str] = []
    description: str = ""
    tags: list[str] = []
    metadata: dict = {}


@app.post("/library/add", response_model=BookOut, summary="Save a book to your library")
def library_add(req: SaveBookRequest):
    book = Books(
        id=req.id, title=req.title, authors=req.authors,
        description=req.description, tags=req.tags, metadata=req.metadata,
    )
    user_library.add(book)
    return _to_out(book)


@app.delete("/library/{book_id}", summary="Remove a book from your library")
def library_remove(book_id: str):
    if user_library.get_by_id(book_id) is None:
        raise HTTPException(status_code=404, detail="Book not in library.")
    user_library.remove(book_id)
    return {"status": "ok"}


@app.get("/library", response_model=list[BookOut], summary="List your saved books")
def library_list():
    return [_to_out(b) for b in user_library.all()]


@app.post("/library/recommend", response_model=list[BookOut],
          summary="Get recommendations based on your library")
def library_recommend(top_n: int = Query(20, ge=1, le=50)):
    """Build TF-IDF from saved books + fetched candidates, return recommendations."""
    saved = user_library.all()
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
            pass

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
            pass

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
