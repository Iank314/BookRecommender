"""Admin-assisted password reset. CLI-only, like make_admin — there is no
web path, so this can't be abused remotely. The support flow: a user emails
asking for a reset → run this on the server → send them the new password →
they log in and (ideally) you tell them to treat it as temporary.

Resetting also revokes all of the account's login sessions.

Usage (on the production server):
    docker exec bookrec python -m scripts.reset_password "<username>"
    docker exec bookrec python -m scripts.reset_password "<username>" --password "<new-password>"
"""

from __future__ import annotations

import argparse
import secrets

from server.storage.users_db import UserStore

# Unambiguous characters only (no 0/O, 1/l/I) — these get read over email.
_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _generate_password() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(12))


def main() -> None:
    ap = argparse.ArgumentParser(description="Reset a user's password.")
    ap.add_argument("username", help="Account username (case-insensitive)")
    ap.add_argument("--password", help="Explicit new password (min 6 chars); "
                                       "omit to generate a random one")
    args = ap.parse_args()

    new_password = args.password or _generate_password()
    if len(new_password) < 6:
        raise SystemExit("Password must be at least 6 characters.")

    store = UserStore()
    if not store.set_password(args.username, new_password):
        raise SystemExit(f"No account named {args.username!r} in {store.db_path}.")

    print(f"Password for {args.username!r} reset. All their sessions were revoked.")
    print(f"New password: {new_password}")


if __name__ == "__main__":
    main()
