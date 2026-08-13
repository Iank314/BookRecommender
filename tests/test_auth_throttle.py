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


def test_many_usernames_do_not_accumulate_forever():
    """The above only prunes the name being checked, which isn't a bound.

    Usernames come from an unauthenticated request body, so they are
    attacker-controlled keys: spraying one bad password across distinct names
    grew the dict one entry per name and nothing ever removed them, because a
    name attacked once is never checked again.
    """
    clock = FakeClock()
    t = LoginThrottle(max_attempts=3, window_seconds=60.0, time_fn=clock)
    for i in range(5_000):
        name = f"victim{i}"
        t.is_allowed(name)
        t.record_failure(name)
    assert len(t._fails) == 5_000  # all still live, within the window

    # One window later, a single unrelated attempt sweeps every aged entry.
    clock.now += 61.0
    t.is_allowed("someone-else")
    assert len(t._fails) == 0


def test_sweep_keeps_live_entries():
    # The sweep must not hand an attacker a reset: names still inside the
    # window survive it, blocked ones stay blocked.
    clock = FakeClock()
    t = LoginThrottle(max_attempts=3, window_seconds=60.0, time_fn=clock)
    for _ in range(3):
        t.record_failure("old")
    clock.now += 61.0
    for _ in range(3):
        t.record_failure("fresh")
    t.is_allowed("trigger")  # sweeps

    assert "old" not in t._fails      # aged out
    assert "fresh" in t._fails        # still live
    assert t.is_allowed("fresh") is False


def test_repeated_failures_on_one_name_stay_bounded():
    # Only the newest `max_attempts` timestamps can change the verdict, so a
    # single name hammered between sweeps must not grow without bound either.
    t = LoginThrottle(max_attempts=3, window_seconds=60.0)
    for _ in range(1_000):
        t.record_failure("alice")
    assert len(t._fails["alice"]) == 3
    assert t.is_allowed("alice") is False
