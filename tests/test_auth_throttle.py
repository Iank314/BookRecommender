"""Tests for the in-process login throttle."""

from server.auth_throttle import LoginThrottle


class FakeClock:
    """Manually-advanced clock so window-expiry tests don't sleep."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def test_allows_attempts_under_the_cap():
    t = LoginThrottle(max_attempts=3)
    for _ in range(3):
        assert t.is_allowed("alice") is True
        t.record_failure("alice")


def test_blocks_after_cap_reached():
    t = LoginThrottle(max_attempts=3)
    for _ in range(3):
        t.record_failure("alice")
    assert t.is_allowed("alice") is False


def test_clear_resets_a_user():
    t = LoginThrottle(max_attempts=2)
    t.record_failure("alice")
    t.record_failure("alice")
    assert t.is_allowed("alice") is False
    t.clear("alice")
    assert t.is_allowed("alice") is True


def test_one_user_does_not_affect_another():
    t = LoginThrottle(max_attempts=2)
    t.record_failure("alice")
    t.record_failure("alice")
    assert t.is_allowed("alice") is False
    assert t.is_allowed("bob") is True


def test_failures_outside_the_window_are_pruned():
    clock = FakeClock()
    t = LoginThrottle(max_attempts=3, window_seconds=60.0, time_fn=clock)
    for _ in range(3):
        t.record_failure("alice")
    assert t.is_allowed("alice") is False
    # Advance past the window: the old failures should drop and alice unblocks.
    clock.now += 61.0
    assert t.is_allowed("alice") is True


def test_partial_window_advance_still_blocks():
    clock = FakeClock()
    t = LoginThrottle(max_attempts=3, window_seconds=60.0, time_fn=clock)
    for _ in range(3):
        t.record_failure("alice")
    clock.now += 30.0  # half the window — failures still count
    assert t.is_allowed("alice") is False


def test_pruning_empties_the_internal_entry():
    # Sanity-check: a user whose failures all aged out should be removed
    # from the internal dict, so the dict doesn't grow unboundedly under
    # steady low-rate attack against many random usernames.
    clock = FakeClock()
    t = LoginThrottle(max_attempts=3, window_seconds=60.0, time_fn=clock)
    t.record_failure("attacker")
    clock.now += 61.0
    t.is_allowed("attacker")  # triggers prune
    assert "attacker" not in t._fails
