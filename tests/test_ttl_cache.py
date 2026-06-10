"""Tests for TTLCache (backs /similar) and the CACHE_VERSION signature stamp."""

from server.cache.rec_cache import CACHE_VERSION, RecommendationCache, TTLCache


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_put_then_get_within_ttl():
    clock = FakeClock()
    cache = TTLCache(ttl_seconds=60, clock=clock)
    cache.put("k", [{"id": "a"}])
    clock.advance(59)
    assert cache.get("k") == [{"id": "a"}]


def test_entry_expires_after_ttl():
    clock = FakeClock()
    cache = TTLCache(ttl_seconds=60, clock=clock)
    cache.put("k", [{"id": "a"}])
    clock.advance(60)
    assert cache.get("k") is None
    # Expired entry was evicted, not just hidden.
    assert len(cache._store) == 0


def test_put_refreshes_ttl():
    clock = FakeClock()
    cache = TTLCache(ttl_seconds=60, clock=clock)
    cache.put("k", [{"id": "old"}])
    clock.advance(50)
    cache.put("k", [{"id": "new"}])
    clock.advance(50)  # 100s after first put, 50s after second
    assert cache.get("k") == [{"id": "new"}]


def test_lru_eviction_at_max_entries():
    clock = FakeClock()
    cache = TTLCache(max_entries=2, ttl_seconds=60, clock=clock)
    cache.put("a", [1])
    cache.put("b", [2])
    cache.get("a")          # bump a to most-recently-used
    cache.put("c", [3])     # evicts b
    assert cache.get("a") == [1]
    assert cache.get("b") is None
    assert cache.get("c") == [3]


def test_external_mutation_does_not_corrupt_cache():
    clock = FakeClock()
    cache = TTLCache(ttl_seconds=60, clock=clock)
    payload = [{"id": "a"}]
    cache.put("k", payload)
    payload.append({"id": "tampered"})
    got = cache.get("k")
    assert got == [{"id": "a"}]
    got.append({"id": "also tampered"})
    assert cache.get("k") == [{"id": "a"}]


def test_signature_embeds_cache_version(monkeypatch):
    # The whole point of CACHE_VERSION: same inputs, different version →
    # different signature, so pre-deploy payloads can't be served post-deploy.
    sig_now = RecommendationCache.signature(["b1"])
    monkeypatch.setattr("server.cache.rec_cache.CACHE_VERSION", CACHE_VERSION + 1)
    sig_bumped = RecommendationCache.signature(["b1"])
    assert sig_now != sig_bumped
