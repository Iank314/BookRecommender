"""
server/fetcher/fetcher.py
Fetch book data from Google Books or Open Library and return them as
`Books` instances.
"""

from __future__ import annotations

import logging
import os
import random
import re
import threading
import time
from typing import List, Optional

logger = logging.getLogger(__name__)

try:
    import requests  # type: ignore
    # Bound at import so redaction still works when a test monkeypatches
    # `requests` on this module with a stub that has no `.exceptions`.
    from requests import exceptions as _requests_exc  # type: ignore
except ImportError:  # pragma: no cover
    requests = None
    _requests_exc = None

from server.cache.rec_cache import TTLCache
from server.models.book import Books

GOOGLE_ENDPOINT  = "https://www.googleapis.com/books/v1/volumes"
OPENLIB_ENDPOINT = "https://openlibrary.org/search.json"
OPENLIB_BASE     = "https://openlibrary.org"

# (connect, read) timeouts — fail fast instead of stalling ~60s on a dead socket.
_HTTP_TIMEOUT = (5, 15)

# Open Library's API policy asks every client to identify itself and provide a
# way to be contacted; unidentified bulk traffic gets throttled and then
# blocked outright at the connection level. This app sent the default
# python-requests agent for its whole life, and a burst of heavy querying from
# the production host was enough to earn a refusal (curl to openlibrary.org
# returned instantly with no status from the server while working fine
# elsewhere). Identifying the app is both the policy requirement and the thing
# that makes a block appealable rather than mysterious.
_USER_AGENT = (
    "BookRecommender/1.0 (+https://iansbookrecs.com; kaufmanian49@gmail.com)"
)
_HEADERS = {"User-Agent": _USER_AGENT, "Accept": "application/json"}

# Open Library gets a concurrency cap of its own. Google Books has had one
# (_GB_SEMAPHORE, 3) since it was written; OL had none at all, despite being
# the source that fetches 700-1000 records per query against Google's 40. Five
# genre queries fire concurrently per request, and nothing bounded how many of
# those could be in flight across simultaneous requests.
_OL_SEMAPHORE = threading.Semaphore(3)

# Statuses worth another attempt: rate limiting and transient server faults.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

# Retry counts are per-provider, because the two pose opposite risks.
#
# Google Books sheds load hard. Measured from the production container over 10
# `subject:` genre queries — the shape both recommendation paths issue — only
# 5/10 succeeded on the first attempt. With the old single retry that is 7/10;
# with three retries on the 0.5/1/2s curve it is 9/10. A flat 0.2s delay
# recovered just 7/10 over a comparable sample, so the waiting is doing real
# work rather than the failures being a pure per-request lottery.
#
# Open Library stays at one retry deliberately. It blocked this app's
# production IP once already, and retrying harder against a service that has
# refused us is exactly the wrong direction — its failures should thin the
# pool, not escalate.
_GB_RETRIES = 3
_OL_RETRIES = 1


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with equal jitter.

    The undelayed curve is 0.5 * 2**attempt, which is what the production
    measurement above validated. Jitter matters because up to five genre
    queries fire concurrently: on a 503 they would otherwise all sleep exactly
    the same interval and retry in the same instant, which is a synchronised
    burst aimed at a service that just said it was overloaded. Spreading them
    over [base/2, base] decorrelates the retries without materially changing
    how long we wait.
    """
    base = 0.5 * (2 ** attempt)
    return base / 2 + random.random() * (base / 2)

# In-memory response cache so repeated genre queries (litrpg, fantasy, ...) across
# /search, /similar and /library/recommend don't re-hit the APIs within the TTL.
# Bounded LRU + TTL: every distinct (url, params) is a fresh key, so an unbounded
# dict would grow one entry per unique query for the life of the process — a slow
# leak on a long-running server. The cap makes it evict instead. `copier=dict`
# shallow-copies the decoded JSON on the way in/out.
#
# The entry cap is NOT a memory bound on its own: entries here range from a few
# KB (a work-detail lookup) to ~780KB (an OL search at limit=1000 — measured at
# ~780 bytes/doc), so 512 of the large kind is ~400MB on a box with 512MB-2GB.
# `max_bytes` is the ceiling that actually holds; the entry cap stays as a cheap
# guard on small responses.
_CACHE_TTL = 600.0  # seconds
_CACHE_MAX_ENTRIES = 512
_CACHE_MAX_BYTES = 64 * 1024 * 1024  # 64 MiB of raw response bodies
_cache = TTLCache(
    max_entries=_CACHE_MAX_ENTRIES,
    ttl_seconds=_CACHE_TTL,
    copier=dict,
    max_bytes=_CACHE_MAX_BYTES,
)

# Exactly the Open Library search fields _from_openlib_doc reads.
#
# This is a correctness fix, not an optimisation. OL's search.json returns a
# fixed 14-field default document that does NOT include `subject`,
# `first_sentence`, or any of the ratings/reading counts — they are opt-in via
# `fields`, and asking costs nothing (measured: 748 bytes/doc default vs 783
# with subjects and first sentences). Without this parameter every one of the
# 700-1000 records a genre query returns arrived with tags=[] and a
# description of "First published in 1974. | By Joe Abercrombie." — which is
# precisely the "sparse OL record" the enrichment paths, the genre-inference
# fallbacks and the per-book fetch_work_detail lookups all exist to work
# around. The scorer was reading fields the request never asked for.
#
# Consequence to remember: this materially changes what /library/recommend and
# /similar score against, and the popularity signals (ratings_count,
# want_to_read_count) go from a constant 0 for every OL book to real values.
# Keep in step with _from_openlib_doc.
_OL_SEARCH_FIELDS = ",".join((
    "key", "title", "subtitle", "author_name", "subject", "first_sentence",
    "first_publish_year", "edition_count", "ratings_average", "ratings_count",
    "want_to_read_count", "already_read_count", "language", "cover_i",
))


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


def cache_bytes() -> int:
    """Raw response bytes held by the cache — surfaced in /admin/stats.

    The count above can sit flat while this climbs, because entry sizes here
    span three orders of magnitude. This is the one to watch against RSS.
    """
    return _cache.nbytes()

# Cap concurrent Google Books requests. Unauthenticated Google has a very low
# per-IP limit, and we fan genre queries out across a thread pool; without this
# they all fire at once and trip 429. An API key raises the ceiling but this
# keeps us polite regardless.
_GB_SEMAPHORE = threading.Semaphore(3)

# Circuit breaker: once Google Books 429s, stop calling it for a cooldown window
# so we fail fast (and politely) instead of retry-sleeping on every later query.
_GB_COOLDOWN_SECONDS = 60.0
_gb_cooldown_until = 0.0
_gb_last_refusal: str | None = None
_gb_last_failure_at = 0.0
_gb_state_lock = threading.Lock()


# How many Google Books requests must fail *back to back* before we stop
# retrying and fail fast for a cooldown window.
#
# This is the counterweight to _GB_RETRIES. Measured, roughly half of genre
# queries 503 on the first attempt and nine in ten recover within four, so a
# lone failure is a load-shedding lottery ticket and must not cost anyone a
# cooldown. A sustained outage is a different animal: without a bound, every
# request would pay 5 queries x 4 attempts x up to 3.5s of backoff, for
# nothing. Consecutive failures separate the two, and any success resets the
# count — so the threshold is only reached when Google is genuinely down for
# us rather than merely flaky.
_GB_MAX_CONSECUTIVE_FAILURES = 5
_gb_consecutive_failures = 0


def _record_gb_failure(reason: str) -> bool:
    """Note a Google Books failure. Returns True if the breaker should trip.

    A single 503 deliberately does NOT start a cooldown: the breaker is a
    politeness response to being told to slow down (429), and a server fault
    is not that — backing off for a minute on one shed request would throw
    away the majority that still succeed.

    Recording is still necessary either way, or google_books_status() reports
    "available" while every genre query is failing, which is precisely the
    blind spot that status exists to close.
    """
    global _gb_last_refusal, _gb_last_failure_at, _gb_consecutive_failures
    with _gb_state_lock:
        _gb_last_refusal = reason
        _gb_last_failure_at = time.time()
        _gb_consecutive_failures += 1
        return _gb_consecutive_failures >= _GB_MAX_CONSECUTIVE_FAILURES


def _record_gb_success() -> None:
    """Reset the consecutive-failure count. Flakiness must not accumulate into
    an outage verdict across an otherwise healthy hour."""
    global _gb_consecutive_failures
    with _gb_state_lock:
        _gb_consecutive_failures = 0


def _gb_in_cooldown() -> bool:
    with _gb_state_lock:
        return time.time() < _gb_cooldown_until


def _set_gb_cooldown(reason: str) -> bool:
    """Trip the breaker. Returns True if this *started* a cooldown window.

    The return value exists so the caller logs once per outage rather than
    once per refused query: three concurrent genre queries all 429 together,
    and three identical warnings per minute reads as a storm rather than a
    state change.
    """
    global _gb_cooldown_until, _gb_last_refusal, _gb_last_failure_at
    with _gb_state_lock:
        now = time.time()
        was_cooling = now < _gb_cooldown_until
        _gb_cooldown_until = now + _GB_COOLDOWN_SECONDS
        _gb_last_refusal = reason
        _gb_last_failure_at = now
        return not was_cooling


def _describe_gb_refusal(resp, has_key: bool) -> str:
    """Turn a Google Books 429 into a sentence that names the actual cause.

    The two causes look identical in the status code and are fixed by opposite
    actions, so collapsing them into "rate limited" is what let local dev spend
    months believing unauthenticated Google Books "returns 0 records". It does
    not: it returns 429 "Quota exceeded ... for consumer
    'project_number:<google's own project>'", because keyless requests share
    one global anonymous pool that the whole internet exhausts daily.
    """
    detail = ""
    try:
        detail = ((resp.json() or {}).get("error") or {}).get("message", "") or ""
    except Exception:
        detail = ""
    detail = redact_secrets(detail.strip())
    if has_key:
        return f"the configured API key's quota is exhausted or throttled ({detail})"
    return (
        "no GOOGLE_BOOKS_API_KEY is set, so requests fall into Google's shared "
        f"anonymous quota pool, which is routinely already exhausted ({detail})"
    )


def google_books_status() -> dict:
    """Whether Google Books is currently answering — for /admin/stats and tools.

    `available` means only "the breaker is not tripped", not "Google is
    healthy"; nothing here proves the next request succeeds. It is enough to
    answer the question that matters, which is whether a given run's results
    came from both providers or from Open Library alone.
    """
    with _gb_state_lock:
        now = time.time()
        remaining = max(0.0, _gb_cooldown_until - now)
        return {
            "key_configured": bool(
                os.environ.get("GOOGLE_BOOKS_API_KEY")),
            "available": remaining <= 0.0,
            "cooldown_seconds_remaining": round(remaining, 1),
            "last_refusal": _gb_last_refusal,
            # Read this alongside `available`: a 503 storm leaves the breaker
            # untripped (available=True) while nothing actually succeeds, so a
            # recent timestamp here with available=True is the signal that
            # Google is faulting rather than throttling.
            "seconds_since_last_failure": (
                round(now - _gb_last_failure_at, 1) if _gb_last_failure_at else None
            ),
            # Distance to the outage verdict. A steady 1-2 here is the normal
            # flaky-but-working state; climbing toward
            # _GB_MAX_CONSECUTIVE_FAILURES means a real outage is forming.
            "consecutive_failures": _gb_consecutive_failures,
        }


# Credentials in a query string. requests quotes the full request URL in its
# exception messages -- "503 Server Error: ... for url: https://...&key=AIza..."
# for an HTTPError, and "Max retries exceeded with url: /v1?key=AIza..." for a
# ConnectionError. app.py logs those with exc_info=True, so one provider outage
# writes the Google Books API key into the container log, and into any
# traceback that gets pasted into a terminal, an issue or a chat.
_SECRET_QS_RE = re.compile(
    r"(?i)([?&](?:key|api[_-]?key|access[_-]?token|token|password)=)[^&\s\"']+"
)


def redact_secrets(text: str) -> str:
    """Mask credential query parameters in an arbitrary string."""
    return _SECRET_QS_RE.sub(r"\1REDACTED", text)


def _reraise_redacted(exc: Exception):
    """Re-raise a requests exception with credentials stripped from its message.

    Keeps the original type where it can: callers branch on it, notably
    _fetch_google_books checking HTTPError.response.status_code for 429.
    `from None` drops the original from the chain -- leaving it would print the
    unredacted message directly beneath the redacted one.
    """
    message = redact_secrets(str(exc))
    response = getattr(exc, "response", None)
    request = getattr(exc, "request", None)
    try:
        clean = type(exc)(message, response=response, request=request)
    except TypeError:  # an exception with a different constructor
        clean = _requests_exc.RequestException(message)
        clean.response, clean.request = response, request
    raise clean from None


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
            resp = requests.get(url, params=params, timeout=_HTTP_TIMEOUT,
                                headers=_HEADERS)
        except _requests_exc.RequestException as exc:
            # Connection/timeout errors quote the URL too, not just HTTP errors.
            _reraise_redacted(exc)
        finally:
            if semaphore is not None:
                semaphore.release()
        # 429 is the polite refusal; 5xx is the provider stumbling. Both are
        # worth one more try, and neither was retried before — a single 503
        # discarded that query's entire candidate pool, up to 1000 books.
        if resp.status_code in _RETRY_STATUSES and attempt < retries:
            retry_after = resp.headers.get("Retry-After")
            wait = (float(retry_after) if (retry_after or "").isdigit()
                    else _backoff_delay(attempt))
            time.sleep(min(wait, 5.0))
            attempt += 1
            continue
        try:
            resp.raise_for_status()
        except _requests_exc.HTTPError as exc:
            _reraise_redacted(exc)
        data = resp.json()
        # Weigh the entry by the raw body length: exact, already measured by
        # requests, and far cheaper than re-serialising the decoded object.
        _cache.put(key, data, weight=len(resp.content))
        return data


class Fetcher:
    """
    Parameters
    ----------
    source : str
        One of the endpoint constants above (Google Books / Open Library).
    api_key : str | None
        Optional Google Books API key.  Unused for Open Library.
    """

    def __init__(self, source: str, api_key: Optional[str] = None):
        self.source  = source
        self.api_key = api_key

    # ------------------------------------------------------------------ #
    # Public unified entrypoints
    # ------------------------------------------------------------------ #

    def fetch(self, query: str | None = None, max_results: int = 40,
              category: str = "general") -> List[Books]:
        if self.source not in (GOOGLE_ENDPOINT, OPENLIB_ENDPOINT):
            raise ValueError(f"Unknown fetch source: {self.source!r}")
        if not query:
            raise ValueError("`query` is required when fetching remotely.")
        if self.source == GOOGLE_ENDPOINT:
            return self._fetch_google_books(query, max_results, category=category)
        books, _ = self._fetch_open_library(query, max_results, category=category)
        return books

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
            data = _get_json(f"{OPENLIB_BASE}{key}.json", None,
                             semaphore=_OL_SEMAPHORE, retries=_OL_RETRIES)
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
                             semaphore=_GB_SEMAPHORE, retries=_GB_RETRIES)
        except requests.exceptions.HTTPError as exc:
            resp = getattr(exc, "response", None)
            if resp is not None and resp.status_code == 429:
                # Back off Google for a while and lean on Open Library — but
                # say so. Returning an empty list silently is what made every
                # keyless run look like "Google Books has no results for this
                # query" instead of "Google Books refused to answer", and a
                # measurement built on that reads as a scoring result.
                reason = _describe_gb_refusal(resp, bool(key))
                if _set_gb_cooldown(reason):
                    logger.warning(
                        "Google Books unavailable for the next %.0fs: %s. "
                        "Results now come from Open Library alone, which "
                        "understates coverage — do not calibrate thresholds "
                        "against them.",
                        _GB_COOLDOWN_SECONDS, reason,
                    )
                return ([], 0) if return_total else []
            # Not a quota refusal — 503 "Service temporarily unavailable" is
            # the common one, and it re-raises for the caller to handle. Record
            # it first: without this the status would report Google Books
            # available while every genre query was failing.
            #
            # It has already exhausted _GB_RETRIES by this point, so reaching
            # here means ~4 attempts failed. Enough of those back to back and
            # this stops being flakiness and starts being an outage, at which
            # point retrying every query is pure latency for no recall.
            status_code = getattr(resp, "status_code", "?")
            if _record_gb_failure(f"HTTP {status_code} from Google Books"):
                if _set_gb_cooldown(
                        f"{_GB_MAX_CONSECUTIVE_FAILURES} consecutive failures "
                        f"(last: HTTP {status_code})"):
                    logger.warning(
                        "Google Books has failed %d times in a row (last: HTTP "
                        "%s); backing off for %.0fs and serving Open Library "
                        "alone. A single 503 is normal here — this many is an "
                        "outage.",
                        _GB_MAX_CONSECUTIVE_FAILURES, status_code,
                        _GB_COOLDOWN_SECONDS,
                    )
            raise
        # Any success clears the outage count. Without this, a long healthy
        # run peppered with the normal ~50% first-attempt 503 rate would creep
        # to the threshold and trip a cooldown on a provider that is working.
        _record_gb_success()
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

        field = {"title": "title", "author": "author", "genre": "subject"}.get(
            category, "q"
        )
        params = {
            field: query,
            "limit": max_results,
            "offset": offset,
            "fields": _OL_SEARCH_FIELDS,
        }

        data = _get_json(OPENLIB_ENDPOINT, params,
                         semaphore=_OL_SEMAPHORE, retries=_OL_RETRIES)
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

        # Build a description from available fields if first_sentence is empty.
        #
        # Subjects deliberately do NOT go in here. They used to, because before
        # the `fields` parameter existed this parser never received any and the
        # line was dead code that came alive the moment it did — putting a
        # book's own genre list into its blurb. Two ways that hurts: the
        # description score then re-measures genre overlap the genre score has
        # already counted, and the padding pushed descriptions from ~45 to
        # ~170 characters, past the `len(description) < 60` test that triggers
        # a work-detail fetch. Measured over 50 epic-fantasy results: real
        # description enrichment fired for 6 books with the subject list in
        # place and 31 without it. Subjects belong in `tags`, which is where
        # they now go.
        if not raw:
            parts = []
            subtitle = doc.get("subtitle", "")
            if subtitle:
                parts.append(subtitle)
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
