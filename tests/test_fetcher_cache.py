"""Regression: the fetcher response cache is bounded.

It was an unbounded module-level dict — every distinct (url, params) is a fresh
key, so it grew one entry per unique query for the life of the process (a slow
memory leak on a long-running server). It's now a bounded LRU + TTL cache.
"""

import server.fetcher.fetcher as fetcher
from server.cache.rec_cache import TTLCache


class _FakeResp:
    status_code = 200
    headers: dict = {}

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class _FakeRequests:
    def __init__(self):
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        return _FakeResp({"url": url, "params": params})


def _fresh_cache(monkeypatch, max_entries):
    monkeypatch.setattr(
        fetcher, "_cache",
        TTLCache(max_entries=max_entries, ttl_seconds=fetcher._CACHE_TTL, copier=dict),
    )
    fake = _FakeRequests()
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
