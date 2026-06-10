"""Route-matching regression tests for slash-bearing book ids.

Open Library ids look like "ol_/works/OL36730729W". The frontend URL-encodes
the slashes, but the ASGI spec decodes %2F back to "/" before routing, so any
book-id path segment must use the :path converter — a plain {book_id} 404s on
every OL book. These tests match scopes against the real app router, pinning
both the converters and the registration order (the {book_id:path} catch-all
must not swallow /library/feedback/... or /library/sections/...).
"""

from starlette.routing import Match

from server.app import app

OL_ID = "ol_/works/OL36730729W"


def _match(method: str, path: str):
    scope = {"type": "http", "method": method, "path": path, "root_path": ""}
    for route in app.routes:
        match, child = route.matches(scope)
        if match == Match.FULL:
            return route, child.get("path_params", {})
    return None, {}


def test_library_delete_matches_ol_id():
    route, params = _match("DELETE", f"/library/{OL_ID}")
    assert route is not None and route.name == "library_remove"
    assert params["book_id"] == OL_ID


def test_feedback_delete_matches_ol_id():
    route, params = _match("DELETE", f"/library/feedback/{OL_ID}")
    assert route is not None and route.name == "feedback_remove"
    assert params["book_id"] == OL_ID


def test_section_book_delete_matches_ol_id():
    route, params = _match("DELETE", f"/library/sections/3/books/{OL_ID}")
    assert route is not None and route.name == "sections_remove_book"
    assert params["book_id"] == OL_ID
    assert params["section_id"] == "3"


def test_catch_all_does_not_swallow_feedback_or_sections():
    # Registration order matters: feedback/section deletes must win over the
    # /library/{book_id:path} catch-all.
    route, _ = _match("DELETE", "/library/feedback/gb_abc")
    assert route.name == "feedback_remove"
    route, _ = _match("DELETE", "/library/sections/7")
    assert route.name == "sections_delete"


def test_plain_google_id_still_matches():
    route, params = _match("DELETE", "/library/gb_abc123")
    assert route is not None and route.name == "library_remove"
    assert params["book_id"] == "gb_abc123"
