"""Load `key=value` pairs from a repo-root `.env` into os.environ.

Docker Compose already reads `.env` for `${VAR}` interpolation, so containers
have always picked these up. Nothing else did: `uvicorn server.app:app` and
`python -m scripts.explain_similar` read the raw process environment, so a key
sitting in `.env` was invisible to exactly the local runs that most needed it.
That is one half of why every local measurement was Open-Library-only — see
`google_books_status` in the fetcher for the other half.

Deliberately dependency-free. `python-dotenv` would do this, but the whole
behaviour is twenty lines and requirements.txt is short on purpose.

Existing environment variables always win. A shell `export` or a Compose
`environment:` block is a more specific statement of intent than a file
checked into nobody's repo (`.env` is gitignored), and silently overriding
the real production environment from a stray file would be a nasty surprise.
"""

from __future__ import annotations

import os
from pathlib import Path

# Repo root: server/env.py -> server/ -> <root>/
DEFAULT_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def parse_env_text(text: str) -> dict[str, str]:
    """Parse .env contents into a dict. Tolerant of the usual hand-written forms.

    Accepts `export FOO=bar`, surrounding single/double quotes, blank lines and
    `#` comments. A line with no `=` is skipped rather than raising: this runs
    at import time on a file people edit by hand, so a typo must not stop the
    server from starting.
    """
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, sep, value = line.partition("=")
        if not sep:
            continue
        name = name.strip()
        if not name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[name] = value
    return values


def load_env_file(path: Path | str | None = None) -> list[str]:
    """Apply `.env` to os.environ without overriding what's already set.

    Returns the names it set, so a caller can log *which* variables came from
    the file — never the values, which are secrets by definition.
    """
    env_path = Path(path) if path is not None else DEFAULT_ENV_PATH
    try:
        if not env_path.is_file():
            return []
        text = env_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    applied: list[str] = []
    for name, value in parse_env_text(text).items():
        if name not in os.environ:
            os.environ[name] = value
            applied.append(name)
    return applied
