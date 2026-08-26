"""Regression: a key in `.env` reaches the process, and a shell var still wins.

Docker Compose reads `.env` for `${VAR}` interpolation, so containers always
had these. `uvicorn server.app:app` and `python -m scripts.*` did not, so a
GOOGLE_BOOKS_API_KEY sitting in `.env` was invisible to exactly the local runs
that needed it — half the reason every local measurement was Open-Library-only
(the other half is that keyless Google Books 429s; see test_provider_status).
"""

import os

from server.env import load_env_file, parse_env_text


def test_parses_the_hand_written_forms():
    parsed = parse_env_text(
        "\n".join([
            "# a comment",
            "",
            'export GOOGLE_BOOKS_API_KEY = "spaced-and-quoted"',
            "BOOKREC_SLOW_REQUEST_MS=3000",
            "SINGLE='quoted'",
            "EMPTY=",
        ])
    )
    assert parsed == {
        "GOOGLE_BOOKS_API_KEY": "spaced-and-quoted",
        "BOOKREC_SLOW_REQUEST_MS": "3000",
        "SINGLE": "quoted",
        "EMPTY": "",
    }


def test_a_malformed_line_is_skipped_not_raised():
    # This runs at import time on a hand-edited file. A typo must not stop the
    # server from starting.
    parsed = parse_env_text("this line has no equals sign\nGOOD=1\n=novalue\n")
    assert parsed == {"GOOD": "1"}


def test_the_real_environment_always_wins(tmp_path, monkeypatch):
    # A shell export or a Compose `environment:` block is a more specific
    # statement of intent than a gitignored file, and silently overriding the
    # production environment from a stray .env would be a nasty surprise.
    monkeypatch.setenv("BOOKREC_TEST_VAR", "from-shell")
    env = tmp_path / ".env"
    env.write_text("BOOKREC_TEST_VAR=from-file\nBOOKREC_OTHER_VAR=from-file\n",
                   encoding="utf-8")

    applied = load_env_file(env)

    assert os.environ["BOOKREC_TEST_VAR"] == "from-shell"
    assert os.environ["BOOKREC_OTHER_VAR"] == "from-file"
    # Only names it actually set, so a caller can log them without claiming
    # credit for the shell's variables.
    assert applied == ["BOOKREC_OTHER_VAR"]
    monkeypatch.delenv("BOOKREC_OTHER_VAR", raising=False)


def test_a_missing_env_file_is_a_no_op(tmp_path):
    assert load_env_file(tmp_path / "nope" / ".env") == []
