"""Tests for the admin flag (UserStore), the activity log (ActivityStore),
and the /admin/stats gate."""

import sqlite3
from pathlib import Path

import pytest

from server.storage.activity_db import ActivityStore
from server.storage.users_db import UserStore


@pytest.fixture
def users(tmp_path: Path) -> UserStore:
    return UserStore(db_path=tmp_path / "users_test.db")


@pytest.fixture
def activity(tmp_path: Path) -> ActivityStore:
    return ActivityStore(db_path=tmp_path / "activity_test.db")


# ---- admin flag --------------------------------------------------------------

def test_new_accounts_are_not_admin(users: UserStore):
    uid = users.create_user("alice", "password1")
    assert users.is_admin(uid) is False


def test_grant_and_revoke_admin(users: UserStore):
    uid = users.create_user("alice", "password1")
    assert users.set_admin("alice", True) is True
    assert users.is_admin(uid) is True
    assert users.set_admin("alice", False) is True
    assert users.is_admin(uid) is False


def test_set_admin_is_case_insensitive(users: UserStore):
    uid = users.create_user("Ian Kaufman", "password1")
    assert users.set_admin("ian kaufman", True) is True
    assert users.is_admin(uid) is True


def test_set_admin_unknown_user_returns_false(users: UserStore):
    assert users.set_admin("nobody", True) is False


def test_list_accounts_has_no_password_hashes(users: UserStore):
    users.create_user("alice", "password1")
    accounts = users.list_accounts()
    assert accounts[0]["username"] == "alice"
    assert "password_hash" not in accounts[0]


def test_admin_column_migration(tmp_path: Path):
    # A users table created before the is_admin column existed must migrate
    # without losing accounts.
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE users (
            user_id       TEXT PRIMARY KEY,
            username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            created_at    INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
        )
        """
    )
    conn.execute(
        "INSERT INTO users (user_id, username, password_hash) VALUES ('u1', 'old', 'x$y')"
    )
    conn.commit()
    conn.close()

    store = UserStore(db_path=db)
    assert store.is_admin("u1") is False
    assert store.set_admin("old", True) is True
    assert store.is_admin("u1") is True


# ---- activity log ------------------------------------------------------------

def test_counts_by_kind(activity: ActivityStore):
    activity.record("search", None)
    activity.record("search", "u1")
    activity.record("recommend", "u1")
    counts = activity.counts_since(0)
    assert counts == {"search": 2, "recommend": 1}


def test_active_users_distinct(activity: ActivityStore):
    activity.record("search", "u1")
    activity.record("similar", "u1")
    activity.record("search", "u2")
    activity.record("search", None)  # anonymous — not an active *user*
    assert activity.active_users_since(0) == 2


def test_anonymous_events(activity: ActivityStore):
    activity.record("search", None)
    activity.record("search", "u1")
    assert activity.anonymous_events_since(0) == 1


def test_last_seen_by_user(activity: ActivityStore):
    activity.record("search", "u1")
    activity.record("recommend", "u1")
    last = activity.last_seen_by_user()
    assert set(last) == {"u1"}
    assert last["u1"] > 0


def test_since_filters_out_old_events(activity: ActivityStore):
    activity.record("search", "u1")
    future = 2_000_000_000_000  # far past any real timestamp
    assert activity.counts_since(future) == {}
    assert activity.active_users_since(future) == 0


# ---- endpoint gate -----------------------------------------------------------

def test_admin_stats_rejects_non_admin(tmp_path, monkeypatch):
    import server.app as app
    from fastapi import HTTPException

    users = UserStore(db_path=tmp_path / "gate.db")
    monkeypatch.setattr(app, "user_store", users)
    uid = users.create_user("pleb", "password1")

    with pytest.raises(HTTPException) as exc:
        app.get_admin_user_id(user_id=uid)
    assert exc.value.status_code == 403

    users.set_admin("pleb", True)
    assert app.get_admin_user_id(user_id=uid) == uid
