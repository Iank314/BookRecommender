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

    def is_allowed(self, username: str) -> bool:
        """Return True if `username` may still attempt a login right now.

        Also prunes any failure timestamps that have fallen out of the
        window, so the per-username list stays bounded even under steady
        sub-threshold attack.
        """
        cutoff = self._now() - self._window
        with self._lock:
            recents = [t for t in self._fails.get(username, []) if t > cutoff]
            if recents:
                self._fails[username] = recents
            else:
                self._fails.pop(username, None)
            return len(recents) < self._max

    def record_failure(self, username: str) -> None:
        """Note a failed login attempt against `username`."""
        with self._lock:
            self._fails.setdefault(username, []).append(self._now())

    def clear(self, username: str) -> None:
        """Forget any prior failures for `username` — call on successful login."""
        with self._lock:
            self._fails.pop(username, None)
