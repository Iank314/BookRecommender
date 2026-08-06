"""SQLite-backed store for the generated "books like X" pages.

One row per published slug, holding the finished result set as JSON. The web
route only reads from here — no scoring, no provider calls in the request path
— so a crawler hitting hundreds of URLs costs nothing beyond a SELECT each.
Rows are written by `scripts.generate_seo_pages` and replaced wholesale on
regeneration, so a bad run is fixed by re-running rather than by repair.
"""

from __future__ import annotations

import json
import time

from server.storage._base import SQLiteStore


class SeoPageStore(SQLiteStore):
    """Read-mostly store: written by the offline generator, read per request."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS seo_pages (
        slug         TEXT PRIMARY KEY,
        source_title TEXT NOT NULL,       -- resolved book title, for display
        source       TEXT NOT NULL,       -- JSON: the enriched source book
        results      TEXT NOT NULL,       -- JSON: [{...book, relevance, reason}]
        generated_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_seo_generated ON seo_pages(generated_at);
    """

    def upsert(self, slug: str, source: dict, results: list[dict]) -> None:
        """Publish (or republish) a page. Replaces any existing row for the slug."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO seo_pages "
                "(slug, source_title, source, results, generated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(slug) DO UPDATE SET "
                "source_title=excluded.source_title, source=excluded.source, "
                "results=excluded.results, generated_at=excluded.generated_at",
                (
                    slug,
                    source.get("title", ""),
                    json.dumps(source),
                    json.dumps(results),
                    int(time.time()),
                ),
            )

    def get(self, slug: str) -> dict | None:
        """Full page payload, or None if the slug was never published."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT slug, source_title, source, results, generated_at "
                "FROM seo_pages WHERE slug = ?",
                (slug,),
            ).fetchone()
        if row is None:
            return None
        return {
            "slug": row["slug"],
            "source_title": row["source_title"],
            "source": json.loads(row["source"]),
            "results": json.loads(row["results"]),
            "generated_at": row["generated_at"],
        }

    def list_pages(self) -> list[dict]:
        """(slug, title, generated_at) for every published page, title-sorted.

        Backs both the hub page and the sitemap, so the two can't disagree
        about what exists.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT slug, source_title, generated_at FROM seo_pages "
                "ORDER BY source_title COLLATE NOCASE"
            ).fetchall()
        return [
            {
                "slug": r["slug"],
                "title": r["source_title"],
                "generated_at": r["generated_at"],
            }
            for r in rows
        ]

    def published_slugs(self) -> set[str]:
        """Slug set, for cheap "does this result already have a page?" lookups
        when rendering internal links."""
        with self._connect() as conn:
            rows = conn.execute("SELECT slug FROM seo_pages").fetchall()
        return {r["slug"] for r in rows}

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM seo_pages").fetchone()[0]

    def delete(self, slug: str) -> bool:
        """Unpublish a page. Returns whether a row was actually removed."""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM seo_pages WHERE slug = ?", (slug,))
            return cur.rowcount > 0
