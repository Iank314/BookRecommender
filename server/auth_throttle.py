"""In-process per-username throttle for failed login attempts.

Tracks recent failure timestamps per username and rejects further attempts
once the username has hit `max_attempts` failures within `window_seconds`.

Per-username (not per-IP) because behind a reverse proxy the client IP isn't
trivially reliable, and the attack we actually care about is credential
stuffing — many password guesses against a known username. Tradeoff: a
hostile party can deliberately lock a legitimate user out for the window by
spamming bad passwords against their account. That's an acceptable
mitigation versus unbounded brute force; the cure is to add per-IP or
CAPTCHA on top later if it becomes a real problem.

Thread-safe (FastAPI runs sync handlers in a thread pool, so /auth/login
calls overlap). Lost on restart — fine; window is short and an attacker
gains nothing from a restart since the rate ceiling is per-window.

Memory: the failure map is swept once per window (see `_sweep_locked`), so
what it holds is names with *live* failures, not names ever seen. The residual
bound is therefore distinct usernames attempted within one window — self-
limiting, since that's capped by how fast the server can answer, and it drains
on the next sweep. Deliberately not capped by evicting live entries: dropping
a tracked name to make room is a throttle bypass, since an attacker could
flush their target's counter by spraying other names.
"""

from __future__ import annotations

import time
from threading import Lock
from typing import Callable


class LoginThrottle:
    """Sliding-window failure counter, keyed by username."""

    def __init__(
        self,
        max_attempts: int = 5,
        window_seconds: float = 60.0,
        time_fn: Callable[[], float] = time.time,
    ) -> None:
        self._fails: dict[str, list[float]] = {}
        self._lock = Lock()
        self._max = max_attempts
        self._window = window_seconds
        self._now = time_fn
        self._last_sweep = time_fn()

    def _sweep_locked(self, cutoff: float) -> None:
        """Drop every username whose failures have all aged out.

        Per-username pruning alone doesn't bound this dict: it only touches
        the name being checked, so a name attacked once and never again keeps
        its entry forever. Usernames come straight from an unauthenticated
        request body, which makes that an attacker-controlled key — spraying
        one bad password across distinct names grew the dict one entry per
        name, without ever tripping the per-name limit. Caller holds the lock.
        """
        for name in [n for n, ts in self._fails.items() if not any(t > cutoff for t in ts)]:
            del self._fails[name]
        self._last_sweep = self._now()

    def is_allowed(self, username: str) -> bool:
        """Return True if `username` may still attempt a login right now.

        Also prunes any failure timestamps that have fallen out of the
        window, so the per-username list stays bounded even under steady
        sub-threshold attack, and sweeps the whole dict once a window.
        """
        now = self._now()
        cutoff = now - self._window
        with self._lock:
            # One full sweep per window: bounded work, and it keeps the dict
            # proportional to names seen *recently* rather than names seen ever.
            if now - self._last_sweep >= self._window:
                self._sweep_locked(cutoff)
            recents = [t for t in self._fails.get(username, []) if t > cutoff]
            if recents:
                self._fails[username] = recents
            else:
                self._fails.pop(username, None)
            return len(recents) < self._max

    def record_failure(self, username: str) -> None:
        """Note a failed login attempt against `username`."""
        with self._lock:
            # Cap the per-name list too. Only the newest `_max` timestamps can
            # affect the verdict, so an attacker hammering one name can't grow
            # a single entry without bound between sweeps.
            attempts = self._fails.setdefault(username, [])
            attempts.append(self._now())
            if len(attempts) > self._max:
                del attempts[:-self._max]

    def clear(self, username: str) -> None:
        """Forget any prior failures for `username` — call on successful login."""
        with self._lock:
            self._fails.pop(username, None)
