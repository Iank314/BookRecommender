"""Regression: a Google Books refusal is reported, not swallowed.

Keyless Google Books does not "return 0 records" — it returns 429 "Quota
exceeded ... for consumer 'project_number:<google's own project>'", because
requests without a key share one global anonymous pool the whole internet
exhausts daily. `_fetch_google_books` caught that, tripped the cooldown and
returned an empty list with no log, so the two states were indistinguishable
from outside:

  * Google Books searched and found nothing for this query
  * Google Books refused to answer at all

Every local measurement was silently the second one, which is how thresholds
came to be calibrated against Open-Library-only pools.
"""

import logging

import pytest

import server.fetcher.fetcher as fetcher


class _Resp429:
    status_code = 429
    headers: dict = {}
    content = b"{}"

    def json(self):
        return {"error": {"message": (
            "Quota exceeded for quota metric 'Queries' and limit 'Queries per "
            "day' of service 'books.googleapis.com' for consumer "
            "'project_number:624717413613'.")}}

    def raise_for_status(self):
        raise fetcher.requests.exceptions.HTTPError(
            "429 Client Error: Too Many Requests", response=self)


class _FakeRequests:
    exceptions = None  # replaced in the fixture with the real module

    def get(self, url, params=None, timeout=None, headers=None):
        return _Resp429()


@pytest.fixture
def gb_429(monkeypatch):
    import requests as real_requests

    fake = _FakeRequests()
    fake.exceptions = real_requests.exceptions
    monkeypatch.setattr(fetcher, "requests", fake)
    monkeypatch.setattr(fetcher, "_requests_exc", real_requests.exceptions)
    # A fresh cache, or a previous test's cached body answers instead.
    from server.cache.rec_cache import TTLCache
    monkeypatch.setattr(fetcher, "_cache", TTLCache(
        max_entries=8, ttl_seconds=600, copier=dict))
    # Reset the breaker so each test observes the transition itself.
    monkeypatch.setattr(fetcher, "_gb_cooldown_until", 0.0)
    monkeypatch.setattr(fetcher, "_gb_last_refusal", None)
    monkeypatch.setattr(fetcher, "_gb_last_failure_at", 0.0)
    return fake


def test_a_quota_refusal_is_recorded(gb_429):
    f = fetcher.Fetcher(source=fetcher.GOOGLE_ENDPOINT)
    books, total = f.fetch_google_page("Mistborn", category="title")

    assert (books, total) == ([], 0)   # the caller still degrades gracefully...
    status = fetcher.google_books_status()
    assert status["available"] is False          # ...but it is no longer silent
    assert status["last_refusal"]
    assert "GOOGLE_BOOKS_API_KEY" in status["last_refusal"]


def test_the_refusal_names_the_missing_key_not_just_rate_limiting(gb_429, monkeypatch):
    # The two causes look identical in the status code and are fixed by
    # opposite actions, so the message has to distinguish them.
    monkeypatch.delenv("GOOGLE_BOOKS_API_KEY", raising=False)
    keyless = fetcher._describe_gb_refusal(_Resp429(), has_key=False)
    keyed = fetcher._describe_gb_refusal(_Resp429(), has_key=True)

    assert "no GOOGLE_BOOKS_API_KEY is set" in keyless
    assert "anonymous quota pool" in keyless
    assert "configured API key" in keyed
    assert "no GOOGLE_BOOKS_API_KEY" not in keyed


def test_it_warns_once_per_outage_not_once_per_query(gb_429, caplog):
    f = fetcher.Fetcher(source=fetcher.GOOGLE_ENDPOINT)
    with caplog.at_level(logging.WARNING, logger=fetcher.__name__):
        for i in range(4):
            # Distinct queries so the response cache can't absorb them.
            f.fetch_google_page(f"query-{i}", category="title")

    warnings = [r for r in caplog.records if "Google Books unavailable" in r.message]
    assert len(warnings) == 1, "three concurrent genre queries must not log three times"


def test_status_reports_whether_a_key_is_configured(monkeypatch):
    monkeypatch.setenv("GOOGLE_BOOKS_API_KEY", "not-a-real-key")
    assert fetcher.google_books_status()["key_configured"] is True
    monkeypatch.delenv("GOOGLE_BOOKS_API_KEY")
    assert fetcher.google_books_status()["key_configured"] is False


def test_a_refusal_message_never_leaks_a_credential():
    class _KeyInMessage(_Resp429):
        def json(self):
            return {"error": {"message":
                    "503 for url: https://books.googleapis.com/v1?key=SECRETVALUE"}}

    described = fetcher._describe_gb_refusal(_KeyInMessage(), has_key=True)
    assert "SECRETVALUE" not in described
    assert "REDACTED" in described


class _Resp503(_Resp429):
    status_code = 503

    def json(self):
        return {"error": {"message": "Service temporarily unavailable."}}

    def raise_for_status(self):
        raise fetcher.requests.exceptions.HTTPError(
            "503 Server Error: Service Unavailable", response=self)


def test_a_503_is_recorded_but_does_not_trip_the_breaker(gb_429, monkeypatch):
    # Measured in production: 1 of 3 requests 503'd on an otherwise healthy
    # day, and the failing one was a `subject:` query -- what both
    # recommendation paths are built from. Backing off for a minute on
    # someone else's server fault would discard the two that still work, but
    # reporting "available" while genre queries fail is the blind spot this
    # status exists to close.
    monkeypatch.setattr(gb_429, "get",
                        lambda url, params=None, timeout=None, headers=None: _Resp503())

    f = fetcher.Fetcher(source=fetcher.GOOGLE_ENDPOINT)
    with pytest.raises(fetcher.requests.exceptions.HTTPError):
        f.fetch_google_page("subject:fantasy", category="genre")

    status = fetcher.google_books_status()
    assert status["available"] is True, "a server fault must not trip the breaker"
    assert "503" in status["last_refusal"]
    assert status["seconds_since_last_failure"] is not None
