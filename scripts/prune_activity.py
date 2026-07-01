"""Prune old activity_log rows so the table doesn't grow without bound.

The activity log gains one row per tracked request (search / similar / recommend)
and is never trimmed by the app, so on a long-lived server it grows forever.
This deletes rows older than a retention window. The admin stats windows top out
at all-time counts, so pick a window you're happy to lose history beyond.

Points at ./data/library.db by default (the same file the Docker bind mount
uses, so it works while the container is running); override with BOOKREC_DB_PATH.
Safe to run live.

Usage:
    python -m scripts.prune_activity                # default: older than 365 days
    python -m scripts.prune_activity --days 180
    python -m scripts.prune_activity --days 90 --dry-run

Cron example (weekly, keep a year):
    0 4 * * 0  cd ~/app && python -m scripts.prune_activity --days 365
"""

from __future__ import annotations

import argparse
import time

from server.storage.activity_db import ActivityStore


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Delete activity_log rows older than N days."
    )
    ap.add_argument(
        "--days", type=int, default=365,
        help="Retention window in days; rows older than this are deleted (default 365).",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Report how many rows would be deleted without deleting them.",
    )
    args = ap.parse_args()
    if args.days < 1:
        raise SystemExit("--days must be >= 1")

    store = ActivityStore()
    cutoff = int(time.time()) - args.days * 86_400

    if args.dry_run:
        would = store.count_before(cutoff)
        print(f"[dry-run] {would} activity_log row(s) older than {args.days}d "
              f"would be pruned from {store.db_path}.")
        return

    removed = store.prune_before(cutoff)
    print(f"Pruned {removed} activity_log row(s) older than {args.days}d "
          f"from {store.db_path}.")


if __name__ == "__main__":
    main()
