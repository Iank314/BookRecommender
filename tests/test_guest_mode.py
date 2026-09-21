"""Guest mode: credential-less accounts, promotion on signup, and expiry.

A guest is deliberately a real `users` row rather than a parallel anonymous
code path, so most of what needs pinning is the ways that could go wrong:
a guest being loggable-into, a guest surviving as a permanent account, a
signup quietly dropping the books somebody collected before making it, and
the pruner reaching past guests into real accounts.
"""

import sqlite3
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import server.app as app_mod
from conftest import make_book as _book
from server.auth_throttle import LoginThrottle
from server.storage.feedback_db import FeedbackStore
from server.storage.library_db import LibraryStore
from server.storage.users_db import (
    GUEST_MAX_AGE_SECONDS,
    GUEST_USERNAME_PREFIX,
    UserStore,
    UsernameTakenError,
)


@pytest.fixture
def users(tmp_path: Path) -> UserStore:
    return UserStore(db_path=tmp_path / "users_test.db")


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> TestClient:
    """A TestClient wired to throwaway stores in one tmp DB.

    The app builds its stores at import time, so the swap has to happen on the
    module globals. One DB file for all of them because that is how production
    runs — which is also what makes the cross-store guest prune testable.
    """
    db = tmp_path / "app_test.db"
    monkeypatch.setattr(app_mod, "user_store", UserStore(db_path=db))
    monkeypatch.setattr(app_mod, "library_store", LibraryStore(db_path=db))
    monkeypatch.setattr(app_mod, "feedback_store", FeedbackStore(db_path=db))
    # Fresh throttle per test: it's an in-process singleton, so without this a
    # test that exhausts the window makes the next one fail.
    monkeypatch.setattr(app_mod, "guest_throttle", LoginThrottle(10, 600.0))
    return TestClient(app_mod.app)


def _age_guests(db: Path, seconds: int) -> None:
    """Backdate every guest row, to reach an expiry without waiting a month."""
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE users SET created_at = ? WHERE is_guest = 1",
            (int(time.time()) - seconds,),
        )


# ---- the guest account itself ------------------------------------------------

def test_guest_is_flagged_and_prefixed(users: UserStore):
    uid, username = users.create_guest()
    assert users.is_guest(uid) is True
    assert username.startswith(GUEST_USERNAME_PREFIX)


def test_real_accounts_are_not_guests(users: UserStore):
    uid = users.create_user("alice", "password1")
    assert users.is_guest(uid) is False


def test_guests_get_distinct_usernames(users: UserStore):
    names = {users.create_guest()[1] for _ in range(20)}
    assert len(names) == 20


def test_a_guest_cannot_be_logged_into(users: UserStore):
    """The whole point of the unusable hash. Knowing the generated username
    must not be enough, and neither must guessing the stored literal."""
    _, username = users.create_guest()
    for attempt in ("", "password1", "!guest-no-login", GUEST_USERNAME_PREFIX):
        assert users.verify_credentials(username, attempt) is None


def test_guest_is_not_admin(users: UserStore):
    uid, _ = users.create_guest()
    assert users.is_admin(uid) is False


def test_list_accounts_reports_the_guest_flag(users: UserStore):
    users.create_user("alice", "password1")
    users.create_guest()
    by_name = {a["username"]: a["is_guest"] for a in users.list_accounts()}
    assert by_name["alice"] is False
    assert sum(by_name.values()) == 1


# ---- promotion ---------------------------------------------------------------

def test_conversion_keeps_the_user_id(users: UserStore):
    """The id is the join key for every library row, so preserving it *is* the
    carry-over — if this changes, the books are orphaned rather than moved."""
    uid, _ = users.create_guest()
    assert users.convert_guest(uid, "alice", "password1") is True
    assert users.verify_credentials("alice", "password1") == uid


def test_conversion_clears_the_guest_flag(users: UserStore):
    uid, _ = users.create_guest()
    users.convert_guest(uid, "alice", "password1")
    assert users.is_guest(uid) is False


def test_conversion_to_a_taken_name_is_rejected(users: UserStore):
    users.create_user("alice", "password1")
    uid, name = users.create_guest()
    with pytest.raises(UsernameTakenError):
        users.convert_guest(uid, "alice", "password2")
    # And the guest is untouched — a failed signup must not strand the session.
    assert users.is_guest(uid) is True
    assert users.get_username(uid) == name


def test_converting_a_real_account_does_nothing(users: UserStore):
    """Guards the promotion path in /auth/register against being handed a
    signed-in user's id: it must not overwrite their credentials."""
    uid = users.create_user("alice", "password1")
    assert users.convert_guest(uid, "mallory", "password2") is False
    assert users.get_username(uid) == "alice"
    assert users.verify_credentials("alice", "password1") == uid


# ---- expiry ------------------------------------------------------------------

def test_a_fresh_guest_is_not_stale(users: UserStore):
    users.create_guest()
    assert users.stale_guest_ids() == []


def test_an_aged_guest_is_stale(users: UserStore):
    uid, _ = users.create_guest()
    _age_guests(users.db_path, GUEST_MAX_AGE_SECONDS + 1)
    assert users.stale_guest_ids() == [uid]


def test_real_accounts_are_never_stale(users: UserStore):
    users.create_user("alice", "password1")
    with sqlite3.connect(users.db_path) as conn:
        conn.execute("UPDATE users SET created_at = 0")
    assert users.stale_guest_ids() == []


def test_a_converted_guest_stops_being_prunable(users: UserStore):
    """Conversion resets created_at, so signing up on day 29 doesn't leave an
    account that the pruner deletes the next morning."""
    uid, _ = users.create_guest()
    _age_guests(users.db_path, GUEST_MAX_AGE_SECONDS + 1)
    users.convert_guest(uid, "alice", "password1")
    assert users.stale_guest_ids() == []


def test_delete_user_removes_the_account_and_its_sessions(users: UserStore):
    uid, _ = users.create_guest()
    token = users.create_session(uid)
    assert users.delete_user(uid) is True
    assert users.user_for_session(token) is None
    assert users.list_accounts() == []


def test_purging_a_user_leaves_other_users_alone(tmp_path: Path):
    db = tmp_path / "shared.db"
    lib, fb = LibraryStore(db_path=db), FeedbackStore(db_path=db)
    for uid in ("guest", "real"):
        lib.add(uid, _book("b1"))
        lib.create_section(uid, "Sci-fi")
        fb.set(uid, _book("b1"), "up")

    assert lib.delete_all_for_user("guest") == 1
    assert fb.delete_all_for_user("guest") == 1
    assert lib.all("guest") == [] and lib.sections("guest") == []
    assert len(lib.all("real")) == 1 and len(lib.sections("real")) == 1
    assert len(fb.all("real")) == 1


# ---- the HTTP surface --------------------------------------------------------

def test_guest_endpoint_unlocks_the_gated_routes(client: TestClient):
    body = client.post("/auth/guest").json()
    assert body["is_guest"] is True

    assert client.post("/library/add", json={"id": "b1", "title": "Dune"}).status_code == 200
    assert client.post("/library/sections", json={"name": "Sci-fi"}).status_code == 200
    assert client.post(
        "/library/feedback", json={"id": "b1", "title": "Dune", "kind": "up"}
    ).status_code == 200
    assert len(client.get("/library").json()) == 1


def test_auth_me_reports_guest_status(client: TestClient):
    client.post("/auth/guest")
    assert client.get("/auth/me").json()["is_guest"] is True


def test_a_second_guest_call_returns_the_same_account(client: TestClient):
    """Idempotence on the cookie: a double-click must not strand the library
    that the first click's guest is already holding."""
    first = client.post("/auth/guest").json()
    client.post("/library/add", json={"id": "b1", "title": "Dune"})
    second = client.post("/auth/guest").json()
    assert second["username"] == first["username"]
    assert len(client.get("/library").json()) == 1


def test_signing_up_as_a_guest_carries_everything_over(client: TestClient):
    client.post("/auth/guest")
    client.post("/library/add", json={"id": "b1", "title": "Dune"})
    client.post("/library/sections", json={"name": "Sci-fi"})
    client.post("/library/feedback", json={"id": "b1", "title": "Dune", "kind": "up"})

    body = client.post(
        "/auth/register", json={"username": "alice", "password": "password1"}
    ).json()
    assert body == {"username": "alice", "is_admin": False, "is_guest": False}

    assert [b["title"] for b in client.get("/library").json()] == ["Dune"]
    assert [s["name"] for s in client.get("/library/sections").json()] == ["Sci-fi"]
    assert len(client.get("/library/feedback?kind=up").json()) == 1


def test_a_promoted_guest_can_log_back_in(client: TestClient):
    client.post("/auth/guest")
    client.post("/library/add", json={"id": "b1", "title": "Dune"})
    client.post("/auth/register", json={"username": "alice", "password": "password1"})
    client.post("/auth/logout")

    assert client.get("/auth/me").status_code == 401
    assert client.post(
        "/auth/login", json={"username": "alice", "password": "password1"}
    ).status_code == 200
    assert len(client.get("/library").json()) == 1


def test_promotion_does_not_leave_the_guest_behind(client: TestClient):
    """One row in, one row out. If conversion ever became a copy, this catches
    the orphan that the pruner would then never collect."""
    client.post("/auth/guest")
    client.post("/auth/register", json={"username": "alice", "password": "password1"})
    assert [a["username"] for a in app_mod.user_store.list_accounts()] == ["alice"]


def test_signing_up_with_a_taken_name_keeps_the_guest_session(client: TestClient):
    app_mod.user_store.create_user("alice", "password1")
    client.post("/auth/guest")
    client.post("/library/add", json={"id": "b1", "title": "Dune"})

    assert client.post(
        "/auth/register", json={"username": "alice", "password": "password2"}
    ).status_code == 409
    # Still a guest, still holding the book — a rejected signup must be a
    # no-op, not a way to lose the library you were trying to save.
    me = client.get("/auth/me").json()
    assert me["is_guest"] is True
    assert len(client.get("/library").json()) == 1


def test_the_guest_prefix_cannot_be_registered(client: TestClient):
    res = client.post(
        "/auth/register",
        json={"username": GUEST_USERNAME_PREFIX + "abcd", "password": "password1"},
    )
    assert res.status_code == 422
    # Case-insensitively, too — usernames are NOCASE, so "Guest-" would
    # otherwise mint a lookalike.
    assert client.post(
        "/auth/register",
        json={"username": "Guest-abcd", "password": "password1"},
    ).status_code == 422


def test_registering_while_signed_in_makes_a_separate_account(client: TestClient):
    """The promotion branch keys on is_guest, so a signed-in user creating a
    second account must not have their own credentials overwritten."""
    client.post("/auth/register", json={"username": "alice", "password": "password1"})
    assert client.post(
        "/auth/register", json={"username": "bob", "password": "password2"}
    ).status_code == 200
    names = {a["username"] for a in app_mod.user_store.list_accounts()}
    assert names == {"alice", "bob"}


def test_a_pruned_guests_cookie_stops_working(client: TestClient):
    """What flips the frontend out of guest mode: the cookie survives the
    account, so every gated route has to 401 rather than resolve to nothing."""
    client.post("/auth/guest")
    client.post("/library/add", json={"id": "b1", "title": "Dune"})
    for uid in app_mod.user_store.stale_guest_ids(now=2 ** 31):
        app_mod.library_store.delete_all_for_user(uid)
        app_mod.user_store.delete_user(uid)

    assert client.get("/auth/me").status_code == 401
    assert client.get("/library").status_code == 401


def test_admin_stats_reports_whether_proxy_headers_are_trusted(
    client: TestClient, monkeypatch
):
    """Misconfiguring this 429s guest creation site-wide for reasons no single
    visitor's behaviour explains, so it has to be inspectable from outside."""
    app_mod.user_store.create_user("alice", "password1")
    app_mod.user_store.set_admin("alice", True)
    client.post("/auth/login", json={"username": "alice", "password": "password1"})

    assert client.get("/admin/stats").json()["trusts_proxy_headers"] is False
    monkeypatch.setattr(app_mod, "TRUST_PROXY_HEADERS", True)
    assert client.get("/admin/stats").json()["trusts_proxy_headers"] is True


def test_admin_stats_counts_guests_separately(client: TestClient):
    """'How many people signed up?' must not drift upward every time a
    stranger clicks 'try it without an account'."""
    app_mod.user_store.create_user("alice", "password1")
    app_mod.user_store.set_admin("alice", True)
    app_mod.user_store.create_guest()

    client.post("/auth/login", json={"username": "alice", "password": "password1"})
    stats = client.get("/admin/stats").json()
    assert stats["accounts_total"] == 1
    assert stats["guests_total"] == 1


# ---- abuse control -----------------------------------------------------------

def test_guest_creation_is_capped_per_client(client: TestClient):
    codes = []
    for _ in range(12):
        c = TestClient(app_mod.app)  # no cookie, so each call mints a guest
        codes.append(c.post("/auth/guest").status_code)
    assert codes[:10] == [200] * 10
    assert codes[10:] == [429, 429]


def test_forwarded_for_is_ignored_without_a_trusted_proxy(client: TestClient):
    """The default. Exposed directly, X-Forwarded-For is written entirely by
    the client, so honouring it would let anyone mint a fresh rate-limit bucket
    per request and defeat the cap completely."""
    codes = []
    for i in range(12):
        c = TestClient(app_mod.app)
        codes.append(c.post(
            "/auth/guest", headers={"X-Forwarded-For": "1.2.3." + str(i)}
        ).status_code)
    assert codes[:10] == [200] * 10
    assert codes[10:] == [429, 429]


def test_a_spoofed_forwarded_for_does_not_reset_the_cap(client: TestClient, monkeypatch):
    """Behind a trusted proxy. Caddy *appends* the real peer, so the last hop
    is the one it vouched for and everything before it is the client's own
    invention. Keying on the conventional first entry would hand an attacker
    an unlimited supply of fresh buckets."""
    monkeypatch.setattr(app_mod, "TRUST_PROXY_HEADERS", True)
    codes = []
    for i in range(12):
        c = TestClient(app_mod.app)
        codes.append(c.post(
            "/auth/guest", headers={"X-Forwarded-For": "1.2.3." + str(i) + ", 9.9.9.9"}
        ).status_code)
    assert 429 in codes


def test_distinct_clients_get_their_own_budget(client: TestClient, monkeypatch):
    """The reason to read the header at all: behind a proxy every request has
    the same socket peer, so without it one busy visitor would 429 everyone."""
    monkeypatch.setattr(app_mod, "TRUST_PROXY_HEADERS", True)
    for _ in range(10):
        TestClient(app_mod.app).post(
            "/auth/guest", headers={"X-Forwarded-For": "9.9.9.9"}
        )
    assert TestClient(app_mod.app).post(
        "/auth/guest", headers={"X-Forwarded-For": "9.9.9.9"}
    ).status_code == 429
    other = TestClient(app_mod.app).post(
        "/auth/guest", headers={"X-Forwarded-For": "8.8.8.8"}
    )
    assert other.status_code == 200


def test_the_shell_is_revalidated_not_heuristically_cached(client: TestClient):
    """Without an explicit Cache-Control, a browser may serve index.html from
    cache for hours on its own judgement — which pins a returning visitor to
    the old frontend, because the ?v=N busts on app.js and style.css only take
    effect once the HTML naming them is itself fresh."""
    res = client.get("/")
    assert "no-cache" in res.headers.get("cache-control", "")
    assert res.headers.get("etag")


def test_revalidating_the_shell_costs_a_304_not_a_resend(client: TestClient):
    """The other half of `no-cache`: Starlette puts conditional handling in
    StaticFiles, not FileResponse, so without an explicit check this route
    would re-send the whole shell on every single page load."""
    first = client.get("/")
    again = client.get("/", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304
    assert again.content == b""
    # A 304 must not promise a body it isn't sending.
    assert "content-length" not in {k.lower() for k in again.headers}
    # The validators have to come back, or the next request can't revalidate.
    assert again.headers["etag"] == first.headers["etag"]
    assert "no-cache" in again.headers.get("cache-control", "")


def test_a_stale_etag_still_gets_the_page(client: TestClient):
    res = client.get("/", headers={"If-None-Match": '"stale"'})
    assert res.status_code == 200 and res.content


def test_etag_matching_handles_lists_and_weak_validators(client: TestClient):
    """Proxies rewrite If-None-Match: they combine entries and may mark a
    validator weak. Missing those forms doesn't break correctness, it just
    silently turns every revalidation back into a full download."""
    etag = client.get("/").headers["etag"]
    for header in ('"other", ' + etag, "W/" + etag, "*"):
        assert client.get("/", headers={"If-None-Match": header}).status_code == 304
