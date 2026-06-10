# CLAUDE.md — BookRecommender

Working notes for Claude Code sessions. The README covers features, deployment, and
runbooks in depth; this file is the quick orientation + current direction.

## What this is

Full-stack content-based book recommender. FastAPI backend + vanilla-JS single-page
frontend. Searches Google Books + Open Library, users register accounts, save books to
a personal library, thumbs-up/down books, and get recommendations scored against their
saved collection. SQLite storage, deployed from a home machine via Docker Compose +
Cloudflare Tunnel.

## Product vision (as of June 2026)

Users should be able to:
1. Search books and view results (works today)
2. Save books to their personal library (works today)
3. Get accurate recommendations from the similarity recommender (works today, quality
   is under active iteration)
4. Organize their library into user-defined **sections** (e.g. "Sci-fi favorites")
   and get recommendations scoped to one section or an ad-hoc selection of books
   (**shipped June 2026** — see below; needs real-world use to shake out UX issues)

### How sections work (shipped, not yet battle-tested)

- **Storage** (`server/storage/library_db.py`): `library_sections` +
  `section_books` tables, many-to-many. Removing a library book cascades out of its
  sections; deleting a section keeps the books. Duplicate names per user raise
  `SectionNameTakenError` (→ HTTP 409).
- **API** (server/app.py): `GET/POST /library/sections`,
  `PATCH/DELETE /library/sections/{id}`, `POST /library/sections/{id}/books`,
  `DELETE /library/sections/{id}/books/{book_id}`. `POST /library/recommend` now takes
  an **optional** JSON body `{section_id}` or `{book_ids: [...]}` (no body = whole
  library; `section_id` wins if both are sent).
- **Scoping rule:** genre profile, language gating, scoring, and diversity caps run on
  the scoped books only; **exclusion sets span the full library + dislikes** so a
  section run never recommends back a book the user already has elsewhere.
- **Cache:** `RecommendationCache.signature()` grew a `scope` bucket so scoped and
  full-library runs cache under distinct keys.
- **Frontend** (`frontend/app.js`): section chip bar in the saved view (All Books /
  per-section / + New / Rename / Delete), an "Add to section…" dropdown per card in
  All Books, a "Move to section…" dropdown + "Remove from section" button when a
  section is open (move = `from_section_id` on the add endpoint, atomic server-side),
  per-card checkboxes feeding a "Get Recommendations from N Selected Books" bar.
  Name input uses `prompt()` — candidate for a nicer modal later. Static assets are
  cache-busted via `?v=N` query strings in index.html — bump N on frontend changes.

## Critical architecture facts (non-obvious)

- **The live recommendation logic lives entirely in `server/app.py`** (~1660 lines):
  search scoring, `/similar` scoring, `/library/recommend` pipeline, genre/language/
  series/fiction heuristics, display-tag cleanup. The `server/recommender/` package
  (TF-IDF + cosine `RecommendationEngine`) is **only** used by the legacy `/build`
  endpoint and the CLI demo — do not confuse the two.
- Both `/similar` and `/library/recommend` score candidates with **IDF-weighted
  token-set F1** (`_idf_weighted_f1` in app.py), not TF-IDF cosine. The README's
  "How It Works" section describes the real pipeline.
- `/library/recommend` is the gold-standard scoring path (genre/description blend,
  tagless-candidate enrichment, fiction/nonfiction filter, sequel handling, diversity
  caps). `/similar` shares its helpers and should stay behaviorally aligned with it —
  historically `/similar` lagged behind and gave worse results.
- Heuristics encode many hard-won edge cases (facet-prefixed OL tags, foreign-edition
  language detection via title script, sequel detection from description prose,
  series collapsing to entry points). Read the comments before "simplifying" them.
- Development pattern: use the live app → spot a bad recommendation → add a small
  targeted fix + a regression test in `tests/`.

## Recommendation quality tuning

**The tuning loop:** spot a bad recommendation → run
`python -m scripts.explain_similar "<book title>"` → read the breakdown (genre
overlap vs description F1 vs popularity, top shared tokens per result) → pull the
right lever → add a regression test. The script calls `_gather_similar_candidates` +
`_score_similar_candidates`, i.e. the exact live pipeline.

**Levers already pulled** (June 2026): token plural folding (`_fold_token`),
genre-synonym folding across GB/OL vocabularies (`_GENRE_SYNONYMS`), publication-
boilerplate stopwords ("published", "york", ...), trailing-period stripping on genre
atoms, and source enrichment in `/similar` (a sparse OL source gets its genres +
full description back-filled before scoring — this alone took "Mistborn" from
romance-novel noise to Sanderson/Jordan epic fantasy). Also: title-lookup
enrichment (`_enrich_source_by_title_lookup` — a still-sparse source borrows
tags/description from the same book on the other provider, author-matched with
spelling squashed), derived-genre queries when no tags survive (reuses
`_derive_genre_from_text` before falling back to "fiction"), and a
`MIN_SIMILAR_SCORE` floor (0.05) so a single-shared-token "match" returns an
honest empty list instead of one wrong book.

**Known data wall:** webnovel serials (e.g. Guiltythree's "Shadow Slave") simply
have no usable metadata in either provider — stub OL records, nothing from GB
unauthenticated. The floor makes these honest empties. A `GOOGLE_BOOKS_API_KEY`
in production improves the odds (GB print editions of webnovels usually carry
categories); beyond that the fix is another data source, not scoring.

**Levers still available before embeddings:** more `_GENRE_SYNONYMS` aliases (the
explain tool prints each book's atoms — add as bad recs surface them); more
boilerplate stopwords; bigram/phrase tokens ("space opera", "dungeon core");
weighting title tokens above description tokens; tuning the `W_GENRE`/`W_DESC`
blend; author-name tokens (currently they boost same-author books — decide if
that's desired).

**Embeddings decision gate:** only reach for semantic embeddings when explain-tool
sessions show bad recs whose shared tokens are *legitimate story vocabulary that
means something different in context* — i.e. token scoring is failing on meaning,
not on data quality, normalization, or boilerplate. Every bad rec so far has been
the latter. If the gate is reached: precompute embeddings at save/enrichment time
(store in SQLite keyed by book id), blend cosine with the genre score, and mind the
deployment budget — Koyeb's free Nano is 512MB RAM, so prefer ONNX MiniLM
(fastembed / onnxruntime, no torch) or an embeddings API over sentence-transformers.

## Admin & activity tracking

- `users.is_admin` column (migrated on startup). Granted ONLY via
  `python -m scripts.make_admin "<username>"` — deliberately no web path, so a
  compromised session can't self-escalate. `GET /admin/stats` is gated by
  `get_admin_user_id` (403 for non-admins); the frontend shows a 📊 Stats button
  when `/auth/me` returns `is_admin: true`.
- `activity_log` table (`server/storage/activity_db.py`): one row per tracked
  event — kind (`search` page-1-only / `similar` / `recommend`), optional user_id
  (anonymous endpoints resolve the session cookie softly via `_soft_user_id`),
  timestamp. No queries/titles/IPs stored. Recording is best-effort
  (`_record_activity`) and must never fail a request.
- CLI stats: `python -m scripts.stats` (read-only, safe against the live DB).

## Caching (three layers, all in `server/cache/rec_cache.py`)

- `RecommendationCache` — per-user LRU for /library/recommend, keyed on a signature
  hash of saved+liked+disliked IDs (+ scope bucket for section/selection runs).
- `TTLCache` (`similar_cache` in app.py) — 1-hour TTL LRU for /similar, keyed on a
  hash of the source book's title/authors/tags/description + top_n. Anonymous
  endpoint, so entries age out instead of being invalidated. Empty results are NOT
  cached (usually transient provider failures).
- `CACHE_VERSION` constant — mixed into both keys. **Bump it whenever the cached
  payload shape changes** (_to_out fields, display-tag cleanup, scoring output).

## Reading status

Saved books carry an exclusive `reading_status` column (`want_to_read` / `reading` /
`read` / NULL) on `library_entries` — a column, not a section, because it's exclusive
where sections are many-to-many. Set via `POST /library/status`; surfaced as
`reading_status` on `GET /library` items. The frontend renders the three statuses as
built-in chips (amber tint, `section-builtin` class) in the same bar as user sections;
status-scoped recommendations reuse the existing `book_ids` scope, so the backend
recommend pipeline knows nothing about statuses. `LibraryStore.add()`'s upsert
deliberately doesn't touch the column, so enrichment re-saves preserve status.
Schema migration: `_init_schema` ALTERs old tables to add the column.

## Known issues / open concerns

1. **Recommendation cache is all-or-nothing.** `RecommendationCache` (in-process LRU,
   `server/cache/rec_cache.py`) keys on a hash of saved+liked+disliked IDs; adding or
   removing one book invalidates everything and forces a full re-fetch + re-score.
   Acknowledged as suboptimal but acceptable for now (cache still helps the common
   "re-check recs / repeat a recent search without changing the library" case).
   Improvement ideas if it becomes a pain point:
   - Cache the **candidate pool** keyed by the library's genre-query set (genre
     profiles change far less often than the book list) and re-run only the cheap
     scoring loop on library changes.
   - Cache per-book enrichment (OL work-detail fetches) separately — those are the
     expensive part and are book-identity-keyed, so they never need invalidating.
   - Sections multiply cache keys (one per scope); the per-user `invalidate()` and
     the signature's scope bucket already handle correctness.
2. **Postgres migration planned** (Neon + Koyeb) — rewrite the three stores in
   `server/storage/` for psycopg. See README "Planned next deployment stage".
   Remember the `reading_status` column and the sections tables when porting.
3. Other backlog: series-name extraction from prose, per-IP rate limiting,
   Litestream backups. See README "Next Features".

## Commands

```bash
# Run locally (creates data/library.db on first run)
uvicorn server.app:app --reload

# Tests
python -m pytest

# Docker (local)
docker compose up --build

# Public via Cloudflare Quick Tunnel (needs BOOKREC_SECURE_COOKIES=true)
docker compose --profile public up -d --build
```

Env vars: `GOOGLE_BOOKS_API_KEY` (raises GB quota), `BOOKREC_DB_PATH`,
`BOOKREC_SECURE_COOKIES=true` when behind HTTPS.

## Test layout

`tests/` — pytest. `test_pipeline.py` and `test_recommender_edge.py` cover the
recommendation heuristics (series splitting, language detection, sequel regexes,
genre atoms); `test_similar_scoring.py` the "Find Similar" scoring blend;
`test_sections.py` section CRUD/membership + the cache scope bucket;
`test_reading_status.py` the status column + schema migration;
`test_rec_cache.py` / `test_ttl_cache.py` the caches (TTL tests use an injectable
fake clock); `test_display_helpers.py` the display-tag
cleanup; plus auth throttle, feedback store, and engine tests. Network calls in the
live endpoints are not exercised — tests target the pure helper functions, so new
heuristics should be written as testable module-level functions in app.py.
