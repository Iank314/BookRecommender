"""Tests for the in-process recommendation cache."""

from server.cache.rec_cache import RecommendationCache


def test_signature_is_order_independent():
    sig_a = RecommendationCache.signature(["b1", "b2", "b3"])
    sig_b = RecommendationCache.signature(["b3", "b1", "b2"])
    assert sig_a == sig_b


def test_signature_changes_with_books():
    sig_a = RecommendationCache.signature(["b1", "b2"])
    sig_b = RecommendationCache.signature(["b1", "b2", "b3"])
    assert sig_a != sig_b


def test_get_miss_returns_none():
    cache = RecommendationCache()
    assert cache.get("u1", "s1", 20) is None


def test_put_then_get_round_trips():
    cache = RecommendationCache()
    payload = [{"id": "b1"}, {"id": "b2"}]
    cache.put("u1", "s1", 20, payload)
    assert cache.get("u1", "s1", 20) == payload


def test_different_top_n_distinct_keys():
    cache = RecommendationCache()
    cache.put("u", "s", 10, [{"id": "a"}])
    cache.put("u", "s", 20, [{"id": "b"}])
    assert cache.get("u", "s", 10) == [{"id": "a"}]
    assert cache.get("u", "s", 20) == [{"id": "b"}]


def test_lru_eviction_at_max_entries():
    cache = RecommendationCache(max_entries=2)
    cache.put("u", "s1", 20, [{"id": "1"}])
    cache.put("u", "s2", 20, [{"id": "2"}])
    cache.put("u", "s3", 20, [{"id": "3"}])
    assert cache.get("u", "s1", 20) is None
    assert cache.get("u", "s2", 20) == [{"id": "2"}]
    assert cache.get("u", "s3", 20) == [{"id": "3"}]


def test_get_marks_as_recently_used():
    cache = RecommendationCache(max_entries=2)
    cache.put("u", "s1", 20, [{"id": "1"}])
    cache.put("u", "s2", 20, [{"id": "2"}])
    cache.get("u", "s1", 20)  # bump s1 to most-recently-used
    cache.put("u", "s3", 20, [{"id": "3"}])  # should evict s2, not s1
    assert cache.get("u", "s1", 20) == [{"id": "1"}]
    assert cache.get("u", "s2", 20) is None
    assert cache.get("u", "s3", 20) == [{"id": "3"}]


def test_invalidate_clears_only_target_user():
    cache = RecommendationCache()
    cache.put("u1", "s", 20, [{"id": "1"}])
    cache.put("u2", "s", 20, [{"id": "2"}])
    cache.invalidate("u1")
    assert cache.get("u1", "s", 20) is None
    assert cache.get("u2", "s", 20) == [{"id": "2"}]


def test_invalidate_clears_all_top_n_variants_for_user():
    cache = RecommendationCache()
    cache.put("u", "s", 10, [{"id": "a"}])
    cache.put("u", "s", 20, [{"id": "b"}])
    cache.invalidate("u")
    assert cache.get("u", "s", 10) is None
    assert cache.get("u", "s", 20) is None


def test_put_overwrites_existing_entry():
    cache = RecommendationCache()
    cache.put("u", "s", 20, [{"id": "old"}])
    cache.put("u", "s", 20, [{"id": "new"}])
    assert cache.get("u", "s", 20) == [{"id": "new"}]


def test_external_mutation_does_not_corrupt_cache():
    cache = RecommendationCache()
    payload = [{"id": "b1"}]
    cache.put("u", "s", 20, payload)
    payload.append({"id": "tampered"})  # mutate after put
    assert cache.get("u", "s", 20) == [{"id": "b1"}]
    retrieved = cache.get("u", "s", 20)
    retrieved.append({"id": "also tampered"})  # mutate after get
    assert cache.get("u", "s", 20) == [{"id": "b1"}]
