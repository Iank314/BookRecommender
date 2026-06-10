"""Tests for username moderation, including the registration gate."""

import pytest
from fastapi import HTTPException

from server.moderation import username_is_clean


def test_plain_slur_blocked():
    assert username_is_clean("Big_Nigger") is False


def test_leetspeak_blocked():
    assert username_is_clean("B1g_N!gg3r") is False
    assert username_is_clean("f.u.c.k.face") is False
    assert username_is_clean("sh1tlord") is False


def test_separators_dont_evade():
    assert username_is_clean("f_u_c_k") is False
    assert username_is_clean("n-i-g-g-a") is False


def test_embedded_in_longer_name_blocked():
    assert username_is_clean("xXfuckmasterXx") is False


def test_normal_usernames_pass():
    for name in ["Ian Kaufman", "Michellek132", "Max_Ku", "annabanana",
                 "bookworm42", "ThePeener", "racheld", "Sarah.trixy"]:
        assert username_is_clean(name) is True, name


def test_known_collision_words_pass():
    # Deliberately omitted high-collision fragments must not block real words.
    for name in ["Dickens_fan", "raccoon_reader", "classy_bass", "tycoon99"]:
        assert username_is_clean(name) is True, name


def test_register_endpoint_rejects_profane_username(tmp_path, monkeypatch):
    import server.app as app
    from fastapi import Response
    from server.storage.users_db import UserStore

    monkeypatch.setattr(app, "user_store", UserStore(db_path=tmp_path / "u.db"))
    req = app.AuthRequest(username="Big_N1gger", password="password1")
    with pytest.raises(HTTPException) as exc:
        app.auth_register(req, Response())
    assert exc.value.status_code == 422
