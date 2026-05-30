# BookRecommender — Content-Based Book Recommender

## Status — what's happening now

**Live deployment.** The app is being put online from a home machine via a Cloudflare Tunnel: Docker Compose runs `bookrec` + `cloudflared`, with the SQLite DB persisting under `./data/library.db` and the tunnel handing out an HTTPS URL. See [Share publicly with Cloudflare Tunnel](#share-publicly-with-cloudflare-tunnel) below for the runbook.

**Active iteration.** Recent work has been on recommendation-quality bug fixes surfaced by real use of the live app — categorization heuristics (facet-prefixed tags, series-name vs. genre ranking, genre derivation from description text), foreign-edition language detection (title script wins over metadata), and sequel detection from descriptions ("the fourth book in...", "the last book in this series"). The pattern lately is: try the app → spot a bad recommendation → add a small targeted fix + a regression test.

**Planned next deployment stage.** Once the self-hosted version has settled, migrate off SQLite to managed Postgres (Neon's free tier) and deploy the container to Koyeb's free Nano tier — the free Koyeb plan has no persistent volumes, so the migration is the blocker, not scale. Scope: rewrite the three stores (`users_db`, `library_db`, `feedback_db`) to use `psycopg`, swap SQL flavors (parameter style, `strftime` → `extract(epoch)`, `ON CONFLICT` syntax), point at a `DATABASE_URL` env var, update tests. Roughly 2–3 hours.

---

## Next Features

**Recommendation quality**
- **Series-name extraction from prose** — today, when a candidate's title carries no volume marker but the description says *"the fourth book in the Hitchhiker's Trilogy"*, the recommender drops the candidate rather than recommend a stranger to start mid-series. Better: extract the series name from phrases like *"in the X Trilogy"* / *"part of the Y series"* and run the existing `_entry_point_book` lookup against it, so book 1 gets swapped in instead of the sequel being dropped. The hard part is keeping false positives low (*"in the tradition of the X series"*, *"like the Y trilogy"*).
- **Smarter nonfiction/homonym filtering** — catch tagless nonfiction and homonym genres (e.g. "magic" the occult topic vs. fantasy magic, "cultivation" the agriculture topic vs. the xianxia genre) by curating nonfiction subject markers and weighting specific genres over broad ones
- **Semantic similarity (embeddings)** — sentence-embed descriptions for better "writing style" matching than token overlap (adds a model dependency, so only if token scoring plateaus)

**Robustness & polish**
- **Verify Google Books end-to-end with an API key** — confirm Google actually contributes results (unauthenticated testing keeps hitting the rate limit and falling back to Open Library)
- **Litestream backup for the self-hosted DB** — replicate `data/library.db` continuously to a free Cloudflare R2 or Backblaze B2 bucket, so a disk failure on the host doesn't lose every account. ~1 hour to wire up; turns the current single-disk SPOF into recoverable state without changing the SQLite story.
- **Stale-cache busting on display-layer changes** — the in-process rec cache keys on `(user, library_signature, top_n)` but not on backend code version, so a deploy that changes `_to_out` or `_clean_tags_for_display` lets stale cached payloads outlive the upgrade. Adding a small `CACHE_VERSION` constant to the signature would force a clean re-score after each release.

**Product**
- **Reading status** — want-to-read / reading / read on library books, and recommend from only the "read" ones
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
- **Similar books**: given a book, fetches candidates matching its genres and ranks them by IDF-weighted token-set similarity (plain cosine collapsed across the differing vocabularies of Google Books and Open Library)
- **Library-based recommendations**: fetches candidates across your library's genres and scores each one against your closest saved book by blending a **genre-overlap score** with a **description-similarity score** — falling back to description-only when a candidate has no genre tags
- **Thumbs up / thumbs down**: any book you 👍 or 👎 becomes a recommendation signal — candidates similar (IDF-weighted token-set F1) to thumbs-up books are nudged up, similar to thumbs-down books are nudged down (multiplicative modifier, clamped so a single signal can't fully override the genre/description match). Thumbs-down books are also removed from candidate pools entirely so they can't slip back via a different edition
- **Open Library enrichment**: candidates that arrive without genres have their subjects and full description back-filled from Open Library's work-detail endpoint, so they can be judged on genre, not text alone
- **Language matching**: recommendations are limited to the language(s) of the source — your library's languages for library recs, the clicked book's language for "Find Similar" (detected from each book's title script) — so an all-English request won't surface Russian, Japanese, Korean, or Chinese editions
- **Fiction/nonfiction filter**: when your library is fiction, nonfiction candidates (how-tos, histories, biographies) are dropped so they can't match on shared theme words like "magic" or "combat"
- **Series collapsing**: across both library recs and "Find Similar", all volumes of a series fold into one recommendation, shown as its entry point — if only "book 11" ranked, the series is looked up by title and book 1 is swapped in
- **Diversity**: caps how many recommendations come from any single saved book and any single author, so one genre or author can't flood the list
- **Popularity as tiebreaker only**: popularity scales a match's score by at most a few percent, so a hugely popular off-genre book can't outrank a genuine match
- **Detail caching**: a book's genres, description, and language are captured when you save it (sparse Open Library entries are enriched), so recommendations don't re-fetch the same data later
- **Recommendation caching**: results for an unchanged library are kept in an in-process LRU cache keyed by a hash spanning saved + liked + disliked book IDs, so repeat calls return instantly without re-fetching or re-scoring; any add/remove/flip in either set produces a fresh key and the user's prior cache entries are evicted eagerly

### Accounts & Personal Library
- Register / log in with a username and password (passwords stored salted + PBKDF2-hashed)
- Each account has its own library, persisted in SQLite and tied to a login session — it survives restarts, cleared cookies, and works across devices
- Search and "Find Similar" are open to everyone; saving, recording feedback, and library recommendations require logging in
- Save and remove books (genres, description, and language are captured on save); view your collection in a dedicated tab
- Three views in the library tab — **Saved** (your collection), **Liked** (thumbs-up signals), **Disliked** (thumbs-down signals) — each independently manageable
- Get recommendations based on your full saved collection, re-ranked by any feedback you've recorded
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
│   └── demo_query.py       CLI demo
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
| `POST` | `/library/recommend` | session | Recommendations based on the saved library + feedback |

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

**Production — `BOOKREC_SECURE_COOKIES`.** When serving over HTTPS (e.g. through a Cloudflare Tunnel — see [Share publicly with Cloudflare Tunnel](#share-publicly-with-cloudflare-tunnel) below), set `BOOKREC_SECURE_COOKIES=true` so the session cookie is only sent on secure connections. Leave it unset for local HTTP development — otherwise the browser refuses to send the cookie back and login appears to silently fail.

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

### Share publicly with Cloudflare Tunnel

The compose file ships with a `cloudflared` service under an opt-in profile, so the app stays local-only by default and only becomes reachable from the open internet when you explicitly bring the tunnel up. Two modes are wired in: **Quick Tunnel** (zero setup, random URL that changes each restart — good for "let me share this for a day") and **Named Tunnel** (stable URL bound to a domain you own — the right mode once you want recruiters to bookmark the link).

#### Going live for the first time (Quick Tunnel)

This is the fastest path — no Cloudflare account, no domain, no token. Roughly 5 minutes once Docker is installed.

1. **Install Docker Desktop** (Windows/Mac) or Docker Engine (Linux) and start it. Verify with `docker version`.
2. **Clone the repo and `cd` into it.** The remaining commands all run from the repo root.
3. **Set the secure-cookies env var.** Required because the browser will be talking to the tunnel over HTTPS — without this, login silently fails.
   ```bash
   # bash
   export BOOKREC_SECURE_COOKIES=true

   # PowerShell
   $env:BOOKREC_SECURE_COOKIES = "true"
   ```
   (Optional but recommended: also `export GOOGLE_BOOKS_API_KEY=...` so the app isn't relying on the unauthenticated Google Books quota under public traffic.)
4. **Bring the stack up with the `public` profile.** First build takes a few minutes for the scipy/scikit-learn wheels; subsequent runs are cached.
   ```bash
   docker compose --profile public up -d --build
   ```
5. **Grab the public URL from the cloudflared logs** and share it.
   ```bash
   docker compose logs cloudflared
   # Look for a line like:
   # https://<random-words>.trycloudflare.com
   ```

That URL is live for as long as the stack stays up. Take it down (and close the public door) with:

```bash
docker compose --profile public down
```

Restart the stack to re-open it — but note the Quick Tunnel will issue a **new** random URL each time, so anyone holding the old one will get a tunnel-offline page.

#### Upgrading to a stable URL (Named Tunnel)

Once you want a link that survives restarts and lives on your own domain, switch to the named profile. Prerequisite: a Cloudflare account with a domain on it (free plan is fine; the domain itself costs ~$10/yr if you don't already have one).

1. **Create the tunnel in the Cloudflare dashboard.** Sign in → **Zero Trust → Networks → Tunnels → Create a tunnel → Cloudflared**. Name it (e.g. `bookrec`).
2. **Copy the connector token** Cloudflare shows you — it's a long `eyJh...` string.
3. **Add a public hostname** in the same dashboard pointing your subdomain (e.g. `bookrec.yourdomain.com`) at `http://bookrec:8000`. (Service = HTTP, URL = `bookrec:8000` — that's the in-network address of the FastAPI container.)
4. **Bring the named profile up** with the token set on the host:
   ```bash
   # bash
   export CLOUDFLARE_TUNNEL_TOKEN=eyJh...
   export BOOKREC_SECURE_COOKIES=true
   docker compose --profile public-named up -d --build

   # PowerShell
   $env:CLOUDFLARE_TUNNEL_TOKEN = "eyJh..."
   $env:BOOKREC_SECURE_COOKIES = "true"
   docker compose --profile public-named up -d --build
   ```

Your subdomain is now live and stays the same across restarts.

#### Caveats worth knowing for either mode

- **Uptime is tied to your machine.** Sleep, reboot, OS update, or stopping the docker daemon takes the URL down until you bring the stack back up. Existing users' accounts, libraries, and feedback are preserved on disk (`./data/library.db`) — they're only locked out, not erased.
- **Disk failure is your single point of failure.** Daily backups of `data/library.db` are the simple answer; [Litestream](https://litestream.io) replicating to a free Cloudflare R2 or Backblaze B2 bucket is the durable one.
- **Residential ISP terms** sometimes technically prohibit serving from a home connection. Rarely enforced, but worth knowing.
- **First-time stranger experience.** The first request after the container spins up takes a few seconds while Python loads scipy / scikit-learn; subsequent requests are fast. Worth keeping in mind when you share the link.

### CLI Demo

```bash
python -m scripts.demo_query
```

### Run Tests

```bash
python -m pytest
```
