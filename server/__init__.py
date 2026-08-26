"""BookRecommender server package.

Loading `.env` here, rather than in app.py, is deliberate: it lands before ANY
server module reads os.environ at import time (app.py's `_env_flag` block) or
at call time (the fetcher's `GOOGLE_BOOKS_API_KEY` lookup), and it covers
import paths that never touch app.py at all — a script importing only
`server.fetcher.fetcher` still gets the key.

That makes the ordering structural instead of a convention an import-sort
could quietly break. See server/env.py for the parsing rules and for why an
existing environment variable always wins.
"""

from server.env import load_env_file

ENV_FROM_FILE = load_env_file()
