"""Regression: the fetcher response cache is bounded.

It was an unbounded module-level dict — every distinct (url, params) is a fresh
key, so it grew one entry per unique query for the life of the process (a slow
memory leak on a long-running server). It's now a bounded LRU + TTL cache.
"""

import pytest

import server.fetcher.fetcher as fetcher
from server.cache.rec_cache import TTLCache


class _FakeResp:
    status_code = 200
    headers: dict = {}

    def __init__(self, payload, size: int = 0):
        self._payload = payload
        # The cache weighs entries by raw body length, mirroring requests'
        # .content — the fake has to carry one or the size bound is untested.
        self.content = b"x" * size

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class _FakeRequests:
    def __init__(self, body_size: int = 0):
        self.calls = 0
        self.last_headers = None
        self._body_size = body_size

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls += 1
        self.last_headers = headers
        return _FakeResp({"url": url, "params": params}, size=self._body_size)


def _fresh_cache(monkeypatch, max_entries, max_bytes=None, body_size=0):
    monkeypatch.setattr(
        fetcher, "_cache",
        TTLCache(max_entries=max_entries, ttl_seconds=fetcher._CACHE_TTL,
                 copier=dict, max_bytes=max_bytes),
    )
    fake = _FakeRequests(body_size=body_size)
    monkeypatch.setattr(fetcher, "requests", fake)
    return fake


def test_response_cache_is_bounded(monkeypatch):
    cap = fetcher._CACHE_MAX_ENTRIES
    fake = _fresh_cache(monkeypatch, cap)

    # Far more distinct queries than the cap — an unbounded dict would keep them all.
    for i in range(cap * 3):
        fetcher._get_json("http://x", {"page": i})

    assert fetcher.cache_size() <= cap
    assert fake.calls == cap * 3  # every key distinct → no hits, all fetched


def test_response_cache_serves_hits(monkeypatch):
    fake = _fresh_cache(monkeypatch, 16)

    first = fetcher._get_json("http://x", {"q": "fantasy"})
    second = fetcher._get_json("http://x", {"q": "fantasy"})

    assert first == second
    assert fake.calls == 1  # second call served from cache, not re-fetched


def test_cached_dict_mutation_does_not_corrupt(monkeypatch):
    fake = _fresh_cache(monkeypatch, 16)

    got = fetcher._get_json("http://x", {"q": "scifi"})
    got["tampered"] = True  # a caller mutating the returned payload...

    again = fetcher._get_json("http://x", {"q": "scifi"})
    assert "tampered" not in again  # ...must not corrupt the cached copy
    assert fake.calls == 1


def test_cache_is_bounded_by_bytes_not_just_entries(monkeypatch):
    """An entry cap is not a memory bound when entries vary in size.

    Fetcher payloads run from a few KB (a work-detail lookup) to ~780KB (an
    Open Library search at limit=1000), so a 512-entry cap admitted hundreds
    of megabytes while looking like a bound.
    """
    # Room for 100 entries, but only 10 of these will fit by weight.
    fake = _fresh_cache(monkeypatch, max_entries=100, max_bytes=10_000,
                        body_size=1_000)
    for i in range(50):
        fetcher._get_json("http://x", {"page": i})

    assert fetcher.cache_size() == 10
    assert fetcher.cache_bytes() <= 10_000
    assert fake.calls == 50


def test_one_oversized_entry_is_still_cached(monkeypatch):
    # Evicting a single over-budget entry into an empty cache would make it a
    # permanent miss, re-fetched on every request — worse than being over.
    _fresh_cache(monkeypatch, max_entries=100, max_bytes=1_000, body_size=50_000)
    fetcher._get_json("http://x", {"q": "huge"})
    assert fetcher.cache_size() == 1


def test_replacing_a_key_does_not_double_count_bytes(monkeypatch):
    _fresh_cache(monkeypatch, max_entries=10, max_bytes=1_000_000, body_size=100)
    for _ in range(5):
        fetcher._cache.put("same-key", {"a": 1}, weight=100)
    assert fetcher.cache_size() == 1
    assert fetcher.cache_bytes() == 100


# ------------------------------------------------------------ requested fields

def test_open_library_search_asks_for_the_fields_it_reads(monkeypatch):
    """OL's search.json returns a fixed default document that omits `subject`,
    `first_sentence` and the ratings counts — they are opt-in via `fields`.

    Without the parameter every OL search result arrived with tags=[] and a
    description of "First published in 1974. | By Joe Abercrombie.", and the
    scorer read fields the request had never asked for.
    """
    captured = {}

    class _Capturing(_FakeRequests):
        def get(self, url, params=None, timeout=None, headers=None):
            captured.update(params or {})
            return super().get(url, params, timeout, headers)

    monkeypatch.setattr(
        fetcher, "_cache",
        TTLCache(max_entries=8, ttl_seconds=fetcher._CACHE_TTL, copier=dict),
    )
    monkeypatch.setattr(fetcher, "requests", _Capturing())
    fetcher.Fetcher(source=fetcher.OPENLIB_ENDPOINT).fetch_page(
        "fantasy", batch_size=10, category="genre"
    )

    requested = set(captured.get("fields", "").split(","))
    for field in ("subject", "first_sentence", "ratings_count", "language"):
        assert field in requested, f"{field} is read by _from_openlib_doc"


def test_requested_fields_cover_what_the_parser_reads():
    # The parser and the request must not drift: a field read but not asked
    # for silently becomes a default, which is how this broke in the first place.
    read_by_parser = {
        "key", "title", "subtitle", "author_name", "subject", "first_sentence",
        "first_publish_year", "edition_count", "ratings_average",
        "ratings_count", "want_to_read_count", "already_read_count",
        "language", "cover_i",
    }
    assert read_by_parser <= set(fetcher._OL_SEARCH_FIELDS.split(","))


def test_subjects_do_not_leak_into_the_description():
    """A book's blurb must not be its own genre list.

    Regression from adding `fields`: the fallback branch had a "Subjects: ..."
    line that was dead code while OL returned no subjects, and came alive the
    moment the request started asking for them. It double-counted genre into
    the description score, and — because it grew descriptions from ~45 to ~170
    characters — pushed them past the `len(description) < 60` test that
    triggers a real work-detail fetch, so the fuller blurb was never retrieved.
    """
    doc = {
        "key": "/works/OL1W",
        "title": "The Poppy War",
        "author_name": ["R. F. Kuang"],
        "first_publish_year": 2018,
        "subject": ["Fiction", "Fantasy", "Historical", "Epic", "Goddesses"],
        # no first_sentence — this is the fallback path
    }
    book = fetcher.Fetcher._from_openlib_doc(doc)

    assert "Subjects:" not in book.description
    assert "Goddesses" not in book.description
    # The subjects are still captured — in tags, where they belong.
    assert "Fantasy" in book.tags
    # And the description stays short enough that _ensure_details will still
    # fetch a real one (its threshold is 60 characters).
    assert len(book.description) < 60, book.description


def test_first_sentence_still_wins_over_the_fallback():
    doc = {
        "key": "/works/OL2W",
        "title": "A Book",
        "author_name": ["Someone"],
        "first_publish_year": 1999,
        "subject": ["Fantasy"],
        "first_sentence": "It was a bright cold day in April.",
    }
    book = fetcher.Fetcher._from_openlib_doc(doc)
    assert book.description == "It was a bright cold day in April."


# --------------------------------------------------------------- credentials
# The Google Books key is passed as a query parameter, and requests quotes the
# full URL in its exception messages. app.py logs those with exc_info=True, so
# before this an ordinary 503 wrote the key into the container log and into any
# traceback pasted into a terminal or a chat.

import requests as _real_requests  # noqa: E402


def test_redact_masks_credential_query_params():
    url = "https://www.googleapis.com/books/v1/volumes?q=dune&key=not-a-real-key-fixture"
    out = fetcher.redact_secrets(f"503 Server Error for url: {url}")
    assert "not-a-real-key-fixture" not in out
    assert "key=REDACTED" in out
    assert "q=dune" in out  # only credentials are masked


@pytest.mark.parametrize("param", ["key", "api_key", "apikey", "access_token",
                                   "token", "password"])
def test_redact_covers_the_usual_credential_names(param):
    out = fetcher.redact_secrets(f"https://x/y?a=1&{param}=SECRETVALUE")
    assert "SECRETVALUE" not in out


def test_http_error_message_has_no_key(monkeypatch):
    class _Failing(_FakeRequests):
        def get(self, url, params=None, timeout=None, headers=None):
            resp = _FakeResp({}, size=0)
            resp.status_code = 503

            def _raise():
                raise _real_requests.exceptions.HTTPError(
                    f"503 Server Error: Service Unavailable for url: "
                    f"{url}?q=dune&key=not-a-real-key-fixture", response=resp)
            resp.raise_for_status = _raise
            return resp

    monkeypatch.setattr(fetcher, "_cache", TTLCache(max_entries=4, ttl_seconds=60,
                                                    copier=dict))
    monkeypatch.setattr(fetcher, "requests", _Failing())
    with pytest.raises(_real_requests.exceptions.HTTPError) as exc:
        fetcher._get_json("https://www.googleapis.com/books/v1/volumes", {"q": "dune"})
    assert "not-a-real-key-fixture" not in str(exc.value)
    assert "REDACTED" in str(exc.value)


def test_connection_error_message_has_no_key(monkeypatch):
    class _Refusing(_FakeRequests):
        def get(self, url, params=None, timeout=None, headers=None):
            raise _real_requests.exceptions.ConnectionError(
                "HTTPSConnectionPool(host='www.googleapis.com', port=443): "
                "Max retries exceeded with url: /books/v1/volumes?key=not-a-real-key-fixture")

    monkeypatch.setattr(fetcher, "_cache", TTLCache(max_entries=4, ttl_seconds=60,
                                                    copier=dict))
    monkeypatch.setattr(fetcher, "requests", _Refusing())
    with pytest.raises(_real_requests.exceptions.ConnectionError) as exc:
        fetcher._get_json("https://www.googleapis.com/books/v1/volumes", {"q": "x"})
    assert "not-a-real-key-fixture" not in str(exc.value)


def test_redaction_preserves_the_response_the_429_branch_reads():
    # _fetch_google_books inspects HTTPError.response.status_code to trip the
    # cooldown. Redaction rebuilds the exception, so that must survive.
    resp = _FakeResp({}, size=0)
    resp.status_code = 429
    original = _real_requests.exceptions.HTTPError(
        "429 for url: https://x?key=not-a-real-key-fixture", response=resp)
    with pytest.raises(_real_requests.exceptions.HTTPError) as exc:
        fetcher._reraise_redacted(original)
    assert exc.value.response is resp
    assert exc.value.response.status_code == 429
    assert "not-a-real-key-fixture" not in str(exc.value)


def test_redaction_does_not_chain_the_unredacted_original():
    # `raise ... from None` matters: a chained cause prints the original
    # message, key and all, directly beneath the redacted one.
    original = _real_requests.exceptions.HTTPError("boom key=not-a-real-key-fixture")
    try:
        fetcher._reraise_redacted(original)
    except _real_requests.exceptions.HTTPError as raised:
        assert raised.__cause__ is None
        assert raised.__suppress_context__ is True


# ---------------------------------------------------------------- politeness
# Open Library's API policy requires a client to identify itself and give a way
# to be contacted. This app sent the default python-requests agent for its
# whole life, and a burst of heavy querying from production earned an outright
# connection refusal from openlibrary.org — requests from the server died in
# 0.16s with no status while the same URL returned 200 from elsewhere.

def test_requests_identify_the_application(monkeypatch):
    fake = _fresh_cache(monkeypatch, 8)
    fetcher._get_json("https://example.com/x", {"q": "1"})

    ua = (fake.last_headers or {}).get("User-Agent", "")
    assert "BookRecommender" in ua, "requests must name the application"
    assert "iansbookrecs.com" in ua or "@" in ua, \
        "requests must carry a contact route so a block is appealable"


def test_open_library_calls_are_concurrency_capped():
    # Google Books has had a semaphore since it was written; Open Library had
    # none, despite fetching 700-1000 records per query against Google's 40.
    assert fetcher._OL_SEMAPHORE is not None


def test_transient_server_errors_are_retried_not_discarded():
    # A single 503 used to throw away that query's entire candidate pool.
    for status in (429, 500, 502, 503, 504):
        assert status in fetcher._RETRY_STATUSES
    for status in (400, 403, 404):
        assert status not in fetcher._RETRY_STATUSES, \
            "retrying a client error just repeats the same rejection"
