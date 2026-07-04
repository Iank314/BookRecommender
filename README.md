# BookRecommender — Content-Based Book Recommender

## Status — what's happening now

**Live in production at [iansbookrecs.com](https://iansbookrecs.com)** (June 2026). The app runs 24/7 on an AWS Lightsail instance ($12/mo plan, first 90 days free): Docker Compose runs the app container behind **Caddy**, which terminates HTTPS with auto-renewing Let's Encrypt certificates for `iansbookrecs.com` + `www`. DNS is Route 53; the instance has a static IP (98.94.245.234). SQLite persists on the instance disk at `~/app/data/library.db` — the full user base was migrated from the earlier home-machine deployment. See [Production deployment](#production-deployment-aws-lightsail) for the runbook.

**Active iteration.** Recent work has been on recommendation-quality bug fixes surfaced by real use of the live app — categorization heuristics (facet-prefixed tags, series-name vs. genre ranking, genre derivation from description text), foreign-edition language detection (title script wins over metadata), sequel detection from descriptions ("the fourth book in...", "the last book in this series"), and **genre-vocabulary normalization**: folding synonyms across the Google Books and Open Library vocabularies (so "sci-fi", "Science-Fiction", and "Science Fiction" score as a match) and dropping non-genre "subject" atoms that were manufacturing cross-genre matches. The latest fix (July 2026) targets Open Library subject facets that aren't genres — marketing labels like "New York Times bestseller", which had scored Jordan Peterson's self-help *Beyond Order* a 0.5 genre overlap with *The Way of Kings* (surfacing it in an epic-fantasy list), and topical facets like "married people", which pulled romances into *Gone Girl*'s recommendations. The pattern lately is: try the app → run `python -m scripts.explain_similar "<title>"` → read the score breakdown to find the bad recommendation's root cause → add a small targeted fix + a regression test.

**Deployment history & next steps.** The app previously ran from a home machine via a Cloudflare Tunnel; that's retired now that it's on Lightsail (the `docker-compose.yml` tunnel profiles remain for local demos only). The old "migrate to Postgres for Koyeb" plan is **obsolete**: Lightsail's persistent disk means SQLite stays. Current operational priorities: enable Lightsail automatic snapshots (backups), set `GOOGLE_BOOKS_API_KEY` on the server, and optionally a GitHub Action for push-to-deploy.

---

## Next Features

**Recommendation quality**
- **Extend the non-genre subject-atom list** — the scorer now drops a seed set of Open Library subject facets that aren't genres (marketing labels like "New York Times bestseller"; person/topic facets "married people" / "husbands" / "wives") so they can't manufacture cross-genre overlap. `explain_similar` sessions surfaced more of the same class — `"crimes against"`, `"kings and rulers"`, `"imaginary places"`, `"artists"`, `"marriage"`, place-names like `"london (england)"` — worth adding to `_GENRE_NOISE_ATOMS` in [app.py](server/app.py) as they show up in bad recs. Low-risk and data-driven: one line each plus a regression assertion.
- **Fold hard-SF genre variants** — `"hard science-fiction"` and `"hard sci-fi"` don't currently match `"hard science fiction"` (hyphen/spacing), so a hard-SF source scores only partial genre overlap with hard-SF candidates (surfaced tuning *Project Hail Mary*). A `_GENRE_SYNONYMS` alias closes the gap.
- **Popularity-weight the `explain_similar` source lookup** — the tuning tool's `_find_source` matches on title only, so a common title can resolve to an obscure edition (querying "Circe" grabbed a 1677 opera instead of Madeline Miller's novel). Preferring a popularity-weighted match would make the debug tool reflect what users actually click. Tooling-only — the live `/similar` endpoint uses the user's clicked book, so it's unaffected.
- **Series-name extraction from prose** — today, when a candidate's title carries no volume marker but the description says *"the fourth book in the Hitchhiker's Trilogy"*, the recommender drops the candidate rather than recommend a stranger to start mid-series. Better: extract the series name from phrases like *"in the X Trilogy"* / *"part of the Y series"* and run the existing `_entry_point_book` lookup against it, so book 1 gets swapped in instead of the sequel being dropped. The hard part is keeping false positives low (*"in the tradition of the X series"*, *"like the Y trilogy"*).
- **Smarter nonfiction/homonym filtering** — catch tagless nonfiction and homonym genres (e.g. "magic" the occult topic vs. fantasy magic, "cultivation" the agriculture topic vs. the xianxia genre) by curating nonfiction subject markers and weighting specific genres over broad ones
- **Semantic similarity (embeddings)** — sentence-embed descriptions for better "writing style" matching than token overlap. Gated: only when `python -m scripts.explain_similar "<title>"` sessions show bad recommendations driven by legitimate story vocabulary meaning different things in context (so far every bad rec has traced to data quality or normalization, all fixable in token land — see CLAUDE.md "Recommendation quality tuning")

**Search coverage & data sources**

The app currently searches **Google Books + Open Library** (see [fetcher.py](server/fetcher/fetcher.py)). Adding a source is a clean extension: an endpoint constant, a `_fetch_X` method, a `_from_X_item` → `Books` mapper, and one wiring change in the `/search` provider loop. Ranked by effort-to-payoff:

- **Set `GOOGLE_BOOKS_API_KEY` first (free, zero code, biggest win).** Unauthenticated, the fetcher caps Google at 3 concurrent requests and trips a 60s cooldown the moment Google 429s — which happens constantly, so Open Library carries most of every search. A free key (Google Cloud Console → enable the Books API) removes that ceiling and is what was starving the webnovel lookups (e.g. Shadow Slave). Do this and measure before adding any new source — more sources just multiply dedup work and latency for marginal recall if Google is still throttled.
- **ISBNdb (paid, ~$15–50/mo) — best single upgrade if budget allows.** Highest-quality metadata and ISBN coverage of any option. Clean REST API. Won't help web serials specifically (see the gap note below).
- **Hardcover (free, GraphQL) — modern/popular titles, Goodreads-like.** Requires a free account token; verify current API access/terms before building (it was early-stage as of early 2026).
- **NYT Books API (free) — a popularity *signal*, not a search backend.** Bestseller lists only; useful to strengthen the popularity tiebreaker in scoring, not to widen raw search recall.
- **Penguin Random House (free key) — high-quality but narrow** (their catalog only).

**Dead ends — don't sink time here:**
- **Goodreads:** API discontinued for new developers in **December 2020**; existing keys stopped working too. There is no supported Goodreads API. (The app uses *Google Books*, not Goodreads — don't confuse the two.)
- **Amazon / Kindle:** the Product Advertising API (PA-API 5.0) requires an Amazon Associates account *with qualifying sales* to keep access, is hard rate-limited (~1 req/sec), and its ToS **prohibits caching results or building a competing catalog** — exactly what this app does. Not viable.

**Structural gap to set expectations:** webnovels and light novels (Shadow Slave, most Royal Road / xianxia serials) have **no metadata in any mainstream book API** until a print/Kindle edition gets cataloged — which Google Books *does* eventually pick up (another reason for the key). ISBNdb won't fix this either; it's a data-availability limit, not an integration one. The `MIN_SIMILAR_SCORE` floor already makes these cases return an honest empty result instead of one wrong book.

**Robustness & polish**
- **Verify Google Books end-to-end with an API key** — confirm Google actually contributes results (unauthenticated testing keeps hitting the rate limit and falling back to Open Library)
- **Litestream backup for the self-hosted DB** — replicate `data/library.db` continuously to a free Cloudflare R2 or Backblaze B2 bucket, so a disk failure on the host doesn't lose every account. ~1 hour to wire up; turns the current single-disk SPOF into recoverable state without changing the SQLite story.

**Product**
- **Account management** — password reset and email verification (currently username + password only)
- **Per-IP rate limiting** — the login throttle is per-username, which targets credential stuffing but lets a single attacker spread guesses across many usernames. A per-IP layer on top, once we're behind Cloudflare's `CF-Connecting-IP` header, closes that gap.

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
- **Similar books**: given a book, fetches candidates matching its genres and scores them with the same blend as library recommendations — genre-tag overlap ⊕ IDF-weighted description similarity — including tagless-candidate enrichment, the fiction/nonfiction filter, and mid-series-sequel dropping (plain cosine collapsed across the differing vocabularies of Google Books and Open Library, so token-set scoring is used instead)
- **Library-based recommendations**: fetches candidates across your library's genres and scores each one against your closest saved book by blending a **genre-overlap score** with a **description-similarity score** — falling back to description-only when a candidate has no genre tags
- **Thumbs up / thumbs down**: any book you 👍 or 👎 becomes a recommendation signal — candidates similar (IDF-weighted token-set F1) to thumbs-up books are nudged up, similar to thumbs-down books are nudged down (multiplicative modifier, clamped so a single signal can't fully override the genre/description match). Both liked and disliked books are removed from candidate pools entirely — a 👍 book re-weights what gets recommended but is never recommended back itself, and neither can slip back in via a different edition
- **Open Library enrichment**: candidates that arrive without genres have their subjects and full description back-filled from Open Library's work-detail endpoint, so they can be judged on genre, not text alone
- **Language matching**: recommendations are limited to the language(s) of the source — your library's languages for library recs, the clicked book's language for "Find Similar" (detected from each book's title script) — so an all-English request won't surface Russian, Japanese, Korean, or Chinese editions
- **Fiction/nonfiction filter**: when your library is fiction, nonfiction candidates (how-tos, histories, biographies) are dropped so they can't match on shared theme words like "magic" or "combat"
- **Series collapsing**: across both library recs and "Find Similar", all volumes of a series fold into one recommendation, shown as its entry point — if only "book 11" ranked, the series is looked up by title and book 1 is swapped in
- **Diversity**: caps how many recommendations come from any single saved book and any single author, so one genre or author can't flood the list
- **Popularity as tiebreaker only**: popularity scales a match's score by at most a few percent, so a hugely popular off-genre book can't outrank a genuine match
- **Detail caching**: a book's genres, description, and language are captured when you save it (sparse Open Library entries are enriched), so recommendations don't re-fetch the same data later
- **Recommendation caching**: results for an unchanged library are kept in an in-process LRU cache keyed by a hash spanning saved + liked + disliked book IDs (plus a `CACHE_VERSION` constant, so payloads cached by older code die on deploy), so repeat calls return instantly without re-fetching or re-scoring; any add/remove/flip in either set produces a fresh key and the user's prior cache entries are evicted eagerly
- **"Find Similar" caching**: similar-book results are kept in a 1-hour TTL LRU cache keyed on the source book's identity, so repeat clicks on the same book — by anyone, it's an anonymous endpoint — skip the fetch + score pipeline entirely

### Accounts & Personal Library
- Register / log in with a username and password (passwords stored salted + PBKDF2-hashed)
- Each account has its own library, persisted in SQLite and tied to a login session — it survives restarts, cleared cookies, and works across devices
- Search and "Find Similar" are open to everyone; saving, recording feedback, and library recommendations require logging in
- Save and remove books (genres, description, and language are captured on save); view your collection in a dedicated tab
- Three views in the library tab — **Saved** (your collection), **Liked** (thumbs-up signals), **Disliked** (thumbs-down signals) — each independently manageable
- **Sections**: organize saved books into named shelves ("Sci-fi favorites", "Cozy reads") — a book can live in any number of sections, and removing it from the library removes it from its sections too
- **Reading status**: mark saved books 📥 want-to-read / 📖 reading / ✅ read — statuses appear as built-in shelves next to your sections, so you can browse just your "Read" books and get recommendations from only those
- Get recommendations based on your full saved collection, **one section**, or an **ad-hoc checkbox selection of books** — scoped runs build their genre/language profile from just those books, while still never recommending back anything you've already saved anywhere; all re-ranked by any feedback you've recorded
- Failed logins are throttled per-username (5 attempts / 60s window) to blunt credential-stuffing attempts
- Session cookies are `HttpOnly`, `SameSite=Lax`, and `Secure` in production (toggle with `BOOKREC_SECURE_COOKIES=true` when serving over HTTPS)

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
│   ├── auth_throttle.py    Per-username failed-login throttle
│   ├── models/
│   │   ├── book.py         Book dataclass
│   │   └── library.py      In-memory collection used inside the recommender pipeline
│   ├── storage/
│   │   ├── feedback_db.py  SQLite per-user thumbs-up / thumbs-down store
│   │   ├── library_db.py   SQLite per-user saved-library store
│   │   └── users_db.py     SQLite accounts + login sessions (PBKDF2 hashing)
│   ├── cache/
│   │   └── rec_cache.py    In-process LRU cache for library recommendations
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
│   ├── demo_query.py       CLI demo
│   └── explain_similar.py  Tuning tool: score breakdown for "Find Similar"
├── tests/
│   ├── test_auth_throttle.py
│   ├── test_engine.py
│   ├── test_feedback_store.py
│   ├── test_pipeline.py
│   ├── test_rec_cache.py
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
| `GET` | `/library/feedback` | session | List the account's thumbs-up / down books (`?kind=up` or `?kind=down` to filter) |
| `POST` | `/library/feedback` | session | Record thumbs-up / down on a book |
| `DELETE` | `/library/feedback/{book_id}` | session | Clear feedback on a book |
| `GET` | `/library/sections` | session | List sections (each with its member book IDs) |
| `POST` | `/library/sections` | session | Create a section (`{"name": ...}`, 409 on duplicate) |
| `PATCH` | `/library/sections/{id}` | session | Rename a section |
| `DELETE` | `/library/sections/{id}` | session | Delete a section (books stay in the library) |
| `POST` | `/library/sections/{id}/books` | session | Add a saved book to a section (`{"book_id": ...}`); include `"from_section_id"` to atomically move it out of another section instead |
| `DELETE` | `/library/sections/{id}/books/{book_id}` | session | Remove a book from a section |
| `POST` | `/library/status` | session | Set or clear a saved book's reading status (`{"book_id": ..., "status": "want_to_read" \| "reading" \| "read" \| null}`) |
| `POST` | `/library/recommend` | session | Recommendations based on the saved library + feedback; optional body `{"section_id": ...}` or `{"book_ids": [...]}` scopes the run to a section or a hand-picked selection |

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

**Production — `BOOKREC_SECURE_COOKIES`.** When serving over HTTPS (as the [production deployment](#production-deployment-aws-lightsail) does, behind Caddy), set `BOOKREC_SECURE_COOKIES=true` so the session cookie is only sent on secure connections. Leave it unset for local HTTP development — otherwise the browser refuses to send the cookie back and login appears to silently fail.

### Run with Docker

The included `Dockerfile` and `docker-compose.yml` build a self-contained image and persist the SQLite DB on the host via a bind-mounted `./data` volume, so accounts and saved libraries survive container restarts and rebuilds.

```bash
# One-shot build + run
docker compose up --build

# Or background it
docker compose up -d --build

# Stop
docker compose down
```

Open http://localhost:8000. To pass through a Google Books API key, set it on the host before bringing the stack up:

```bash
# bash
export GOOGLE_BOOKS_API_KEY=your_key_here && docker compose up
# PowerShell
$env:GOOGLE_BOOKS_API_KEY = "your_key_here"; docker compose up
```

Without Compose:

```bash
docker build -t bookrecommender .
docker run --rm -p 8000:8000 -v "$(pwd)/data:/app/data" bookrecommender
```

### Production deployment (AWS Lightsail)

The live site at **https://iansbookrecs.com** runs on an AWS Lightsail instance (2 GB RAM / 2 vCPU / 60 GB SSD, Amazon Linux 2023, us-east-1) with a static IP. The stack is [`docker-compose.prod.yml`](docker-compose.prod.yml): the app container plus **Caddy** ([`Caddyfile`](Caddyfile)), which handles HTTPS automatically — certificates are fetched and renewed by Caddy with zero maintenance, stored in a Docker volume so restarts don't re-request them. Route 53 hosts the domain with A records (root + `www`) pointing at the static IP. The Lightsail firewall allows 22 (SSH, restricted), 80, and 443.

**Shipping a change to production** — one command after commit+push:

```powershell
.\scripts\deploy.ps1        # tests → checks everything's pushed → server pull+rebuild → verifies the site
```

The script connects via the SSH alias `iansbookrecs`, defined in the operator's local `~/.ssh/config` (host, user, and key path live there — never in this repo). Manual equivalent:

```bash
ssh iansbookrecs "cd app && git pull && sudo docker compose -f docker-compose.prod.yml up -d --build"
```

Frontend changes: remember to bump the `?v=N` query strings in `index.html`. Payload-shape or scoring changes: bump `CACHE_VERSION` in `server/cache/rec_cache.py` (see CLAUDE.md).

**Operational notes:**
- The database is `~/app/data/library.db` **on the server** — the laptop copy is dev/test data now; they diverged at migration. Never deploy by copying a local DB over the server's.
- Useful: `sudo docker compose -f docker-compose.prod.yml logs -f bookrec` (live request log), `logs caddy` (cert issuance), `ps` (status).
- Costs: $12/mo instance (first 90 days free) + ~$13/yr domain. Static IP free while attached.
- Backlog: enable Lightsail automatic snapshots for backups; set `GOOGLE_BOOKS_API_KEY` in an `.env` file or the compose environment.

### CLI Demo

```bash
python -m scripts.demo_query
```

### Run Tests

```bash
python -m pytest
```
