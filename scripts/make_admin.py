"""Grant or revoke admin on an account. Deliberately CLI-only — there is no
web path to set the admin flag, so a compromised session can't escalate.

Points at ./data/library.db by default (the same file the Docker bind mount
uses, so it works while the container is running); override with
BOOKREC_DB_PATH.

Usage:
    python -m scripts.make_admin "Ian Kaufman"
    python -m scripts.make_admin "Ian Kaufman" --revoke
"""

from __future__ import annotations

import argparse

from server.storage.users_db import UserStore


def main() -> None:
    ap = argparse.ArgumentParser(description="Grant or revoke admin on an account.")
    ap.add_argument("username", help="Account username (case-insensitive)")
    ap.add_argument("--revoke", action="store_true", help="Remove admin instead")
    args = ap.parse_args()

    store = UserStore()
    if store.set_admin(args.username, not args.revoke):
        verb = "revoked from" if args.revoke else "granted to"
        print(f"Admin {verb} {args.username!r}.")
    else:
        raise SystemExit(f"No account named {args.username!r} in {store.db_path}.")


if __name__ == "__main__":
    main()
