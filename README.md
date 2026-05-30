# BookRecommender — Content-Based Book Recommender

## Next Features

**Recommendation quality**
- **Smarter nonfiction/homonym filtering** — catch tagless nonfiction and homonym genres (e.g. "magic" the occult topic vs. fantasy magic, "cultivation" the agriculture topic vs. the xianxia genre) by curating nonfiction subject markers and weighting specific genres over broad ones
- **Thumbs-down / "not interested"** — let users dismiss a recommendation; store it per-account and exclude it from future runs
- **Semantic similarity (embeddings)** — sentence-embed descriptions for better "writing style" matching than token overlap (adds a model dependency, so only if token scoring plateaus)

**Robustness & polish**
- **Verify Google Books end-to-end with an API key** — confirm Google actually contributes results (unauthenticated testing keeps hitting the rate limit and falling back to Open Library)
- **Cache recommendation results per library signature** — return instantly for an unchanged library without re-scoring
- **Extend series collapsing + language filter to `/similar`** — those currently live only in `/library/recommend`, so "Find Similar" can still surface book 11 or a foreign edition

**Product**
- **Docker containerization** — package the app for easy deployment and consistent environments
- **Reading status** — want-to-read / reading / read on library books, and recommend from only the "read" ones
- **Account management** — password reset and email verification (currently username + password only)

---

A full-stack book recommendation system that searches **Google Books** and **Open Library**, scores results by relevance and popularity, and recommends titles by blending genre overlap with IDF-weighted description similarity. Users register an account, build a personal library that persists across sessions and devices, and get recommendations based on their saved collection.

---

## Tech Stack

| Layer | Technology | Why |
|-------|------------|-----|
| Backend | **Python 3.11, FastAPI, Uvicorn** | Async-ready API with automatic docs |
| Data & NLP | **scikit-learn, NumPy, SciPy** | TF-IDF index for the build pipeline; live recommendations use IDF-weighted token-set similarity (pure stdlib) |
| Storage | **SQLite** | Per-user accounts and saved libraries, single-file DB |
| Auth | **stdlib `hashlib` (PBKDF2) + `secrets`** | Salted password hashing and session tokens, no extra dependencies |
| HTTP | **requests + ThreadPoolExecutor** | REST client for Google Books & Open Library; genre queries and enrichment run concurrently, with a TTL response cache, a Google Books concurrency cap, and 429 backoff/cooldown |
| Frontend | **HTML5, CSS3, JavaScript** | Lightweight single-page app, no framework overhead |
| Testing | **pytest, unittest** | Fast, readable unit and integration tests |

---

## Features

### Multi-Source Book Search
- Search by **title**, **author**, or **genre** across Google Books and Open Library
- Deduplicates results across APIs
- Scores books 0–100 using a hybrid formula: match quality + popularity metrics (edition count, ratings, want-to-read signals)
- Paginated results (20 per page) with relevance badges

### Content-Based Recommendations
- **Similar books**: given a book, fetches candidates matching its genres and ranks them by IDF-weighted token-set similarity (plain cosine collapsed across the differing vocabularies of Google Books and Open Library)
- **Library-based recommendations**: fetches candidates across your library's genres and scores each one against your closest saved book by blending a **genre-overlap score** with a **description-similarity score** — falling back to description-only when a candidate has no genre tags
- **Open Library enrichment**: candidates that arrive without genres have their subjects and full description back-filled from Open Library's work-detail endpoint, so they can be judged on genre, not text alone
- **Language matching**: recommendations are limited to the languages present in your library (language detected from each book's title script) — an all-English library won't surface Russian, Japanese, Korean, or Chinese editions
- **Fiction/nonfiction filter**: when your library is fiction, nonfiction candidates (how-tos, histories, biographies) are dropped so they can't match on shared theme words like "magic" or "combat"
- **Series collapsing**: all volumes of a series fold into one recommendation, shown as its entry point — if only "book 11" ranked, the series is looked up by title and book 1 is swapped in
- **Diversity**: caps how many recommendations come from any single saved book and any single author, so one genre or author can't flood the list
- **Popularity as tiebreaker only**: popularity scales a match's score by at most a few percent, so a hugely popular off-genre book can't outrank a genuine match
- **Detail caching**: a book's genres, description, and language are captured when you save it (sparse Open Library entries are enriched), so recommendations don't re-fetch the same data later

### Accounts & Personal Library
- Register / log in with a username and password (passwords stored salted + PBKDF2-hashed)
- Each account has its own library, persisted in SQLite and tied to a login session — it survives restarts, cleared cookies, and works across devices
- Search and "Find Similar" are open to everyone; saving and library recommendations require logging in
- Save and remove books (genres, description, and language are captured on save); view your collection in a dedicated tab
- Get recommendations based on your full saved collection

### Frontend
- Dark-themed single-page app with category tabs (Title / Author / Genre / My Library)
- Book cards with expandable descriptions, relevance scores (color-coded), and external links (Google Books, Open Library, Google search)
- "Find Similar" button on each result
- Pagination with numbered page buttons
- Loading spinner and error display

---

## How It Works

```
┌─────────────┐  query    ┌───────────────────────┐
│  Frontend   │─────────► │  FastAPI  (/search)   │
└─────────────┘           └───────────────────────┘
                                    │
                          ┌─────────┴─────────┐
                          ▼                   ▼
                   Google Books API    Open Library API
                          │                   │
                          └─────────┬─────────┘
                                    ▼
                          Deduplicate & Score
                                    │
                                    ▼
                          Return paginated results
```

**Library recommendation pipeline:**

```
Library genres + languages ─► Fetch candidates (concurrent: Google Books + Open Library)
                        │
                        ▼
   Keep only the library's languages  →  drop nonfiction if the library is fiction
                        │
                        ▼
   Enrich tagless candidates  (Open Library work detail → genres + description)
                        │
                        ▼
   Score each candidate vs. the closest saved book:
       genre_score (tag overlap)  ⊕  description_score (IDF-weighted token F1)
       └─ blend when tagged, description-only when not; popularity = tiebreaker
                        │
                        ▼
   Collapse series to one entry  →  diversify (cap per saved book + per author)
                        │
                        ▼
   Swap later-volume series for book 1  →  Top-N recommendations
```

---

## Project Structure

```
BookRecommender/
├── frontend/
│   ├── index.html          Single-page app shell
│   ├── app.js              Event handling & API calls
│   └── style.css           Dark theme, responsive layout
├── server/
│   ├── app.py              FastAPI REST API (auth, search, similar, library)
│   ├── models/
│   │   ├── book.py         Book dataclass
│   │   └── library.py      In-memory collection used inside the recommender pipeline
│   ├── storage/
│   │   ├── library_db.py   SQLite per-user saved-library store
│   │   └── users_db.py     SQLite accounts + login sessions (PBKDF2 hashing)
│   ├── fetcher/
│   │   └── fetcher.py      Google Books + Open Library adapters (search + work-detail enrichment)
│   ├── preprocessing/
│   │   └── text_processor.py   HTML/URL stripping, lowercasing, cleanup
│   ├── features/
│   │   └── features.py     TF-IDF vectorizer + tag one-hot encoder
│   └── recommender/
│       ├── recommendation_engine.py   Cosine similarity engine
│       └── recommender.py            Full pipeline orchestrator
├── data/
│   └── library.db          SQLite database (accounts, sessions, libraries) — created on first run
├── scripts/
│   └── demo_query.py       CLI demo
├── tests/
│   ├── test_engine.py
│   ├── test_pipeline.py
│   └── test_recommender_edge.py
├── requirements.txt
└── README.md
```

---

## API Endpoints

| Method | Endpoint | Auth | Purpose |
|--------|----------|------|---------|
| `GET` | `/` | — | Serve frontend |
| `POST` | `/search` | — | Search books by title, author, or genre |
| `POST` | `/similar` | — | Find books similar to a given book |
| `POST` | `/auth/register` | — | Create an account, start a login session |
| `POST` | `/auth/login` | — | Log in, start a login session |
| `POST` | `/auth/logout` | — | End the current session |
| `GET` | `/auth/me` | session | Return the logged-in username |
| `GET` | `/library` | session | List the account's saved books |
| `POST` | `/library/add` | session | Save a book to the account's library |
| `DELETE` | `/library/{book_id}` | session | Remove a book from the library |
| `POST` | `/library/recommend` | session | Recommendations based on the saved library |

Session-gated endpoints require a valid `bookrec_session` cookie (set on register/login) and return `401` otherwise.

---

## Running the App

```bash
# Install dependencies
python -m pip install -r requirements.txt

# Start the server
uvicorn server.app:app --reload

# Open http://localhost:8000 in your browser
```

On first run the server creates a SQLite database at `data/library.db` for accounts and saved libraries. Override the location with the `BOOKREC_DB_PATH` environment variable.

**Optional — Google Books API key.** Without a key, Google Books' unauthenticated per-IP limit is low; firing several genre queries at once can return `429 Too Many Requests`, in which case the app backs off and leans on Open Library. Set a free key to raise the quota and keep Google in the results:

```bash
# bash
export GOOGLE_BOOKS_API_KEY=your_key_here
# PowerShell
$env:GOOGLE_BOOKS_API_KEY = "your_key_here"
```

### CLI Demo

```bash
python -m scripts.demo_query
```

### Run Tests

```bash
python -m pytest
```
