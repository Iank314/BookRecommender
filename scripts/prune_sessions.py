"""Delete expired login sessions so the table doesn't grow without bound.

Every login inserts a row; only an explicit logout or a password reset ever
removes one. Sessions older than UserStore.SESSION_MAX_AGE_SECONDS are already
rejected at login-check time, so this reclaims disk rather than changing who
can log in — running it can't sign anyone out who wasn't already signed out.

Points at ./data/library.db by default (the same file the Docker bind mount
uses, so it works while the container is running); override with BOOKREC_DB_PATH.
Safe to run live.

Usage:
    python -m scripts.prune_sessions
    python -m scripts.prune_sessions --dry-run

Cron example (weekly, alongside the activity prune):
    0 4 * * 0  cd ~/app && python -m scripts.prune_sessions
"""

from __future__ import annotations

import argparse

from server.storage.users_db import SESSION_MAX_AGE_SECONDS, UserStore


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Delete login sessions past their server-side expiry."
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Report how many sessions would be deleted without deleting them.",
    )
    args = ap.parse_args()

    store = UserStore()
    days = SESSION_MAX_AGE_SECONDS // 86_400

    if args.dry_run:
        would = store.count_expired_sessions()
        print(f"[dry-run] {would} session(s) older than {days}d "
              f"would be pruned from {store.db_path}.")
        return

    removed = store.prune_sessions()
    print(f"Pruned {removed} expired session(s) (older than {days}d) "
          f"from {store.db_path}.")


if __name__ == "__main__":
    main()
