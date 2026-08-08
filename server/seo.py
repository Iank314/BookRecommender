"""Public "books like X" pages: slugs, the publish gate, and sitemap/robots.

These pages are the site's only indexable surface — the SPA is one
client-rendered route, so search engines have nothing else to crawl. Each page
answers "what should I read after <book>" using the same scorer /similar uses,
and is generated offline by `scripts.generate_seo_pages` rather than computed
per request (a crawler walking hundreds of URLs would otherwise mean thousands
of Google Books / Open Library calls and multi-second responses).

Everything here is pure: no app, provider, or storage imports, so the request
path (server.app) and the offline generator can both use it without an import
cycle.
"""

from __future__ import annotations

import re
import unicodedata
from xml.sax.saxutils import escape as xml_escape

# Where the canonical URLs point. Override for staging/local runs.
DEFAULT_BASE_URL = "https://iansbookrecs.com"

# Publish gate. A page with a handful of weak matches is thin content: it
# reads as a doorway page, and a few hundred of those hurt the whole domain's
# standing more than the traffic they'd earn. Better 150 good pages than 500
# padded ones — the generator skips anything that can't clear both bars.
#
# Tuned against SEO_MIN_SCORE and the author cap in app.py, which together cut
# a typical list roughly in half: 6 strong recommendations is a page worth
# reading, and requiring more would reject books the recommender handles well.
MIN_PAGE_RESULTS = 6
MIN_TOP_RELEVANCE = 20.0

# Slugs are the public URL, so keep them short and stable; long ones get
# truncated at a word boundary rather than mid-word.
MAX_SLUG_LENGTH = 80


def slugify(title: str) -> str:
    """URL slug for a book title: "The Name of the Wind" -> "the-name-of-the-wind".

    Accents fold to ASCII (Sr. Zafón -> zafon) so the URL survives copy/paste
    and mixed encodings. Returns "" when nothing usable is left — callers must
    treat that as "not publishable" rather than writing an empty slug.
    """
    folded = unicodedata.normalize("NFKD", title)
    ascii_only = folded.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only).strip("-")
    if len(slug) <= MAX_SLUG_LENGTH:
        return slug
    cut = slug[:MAX_SLUG_LENGTH]
    # Prefer a clean word boundary, but don't return a stub if the first word
    # is itself longer than the limit.
    boundary = cut.rfind("-")
    return (cut[:boundary] if boundary > MAX_SLUG_LENGTH // 2 else cut).strip("-")


# Genre vocabulary, curated from the atoms actually observed across a 124-page
# generation run rather than invented. Open Library's subject tags are catalog
# facets, not genres: a record's tags are as likely to say "united states
# marshals" or "military education" as "thriller". Matching on a facet is how
# Ender's Game came to recommend Army training manuals.
#
# "fiction" and "general" are excluded on purpose — they're the generic bucket
# in _genre_atoms and carry almost no signal. Shutter Island has "fiction" and
# nothing else genre-like, and its page was US Marshals paperwork.
CORE_GENRES = frozenset({
    "fantasy", "high fantasy", "epic fantasy", "epic", "dark fantasy",
    "urban fantasy", "magic realism", "fairy tales", "mythology", "legends",
    "science fiction", "ciencia-ficción", "science fantasy", "space opera",
    "cyberpunk", "dystopian", "post-apocalyptic", "litrpg", "gamelit",
    "mystery", "detective", "crime", "thriller", "suspense", "noir", "spy",
    "horror", "ghost", "gothic",
    "romance", "romantasy", "erotica",
    "historical fiction", "historical", "western", "war",
    "adventure", "action & adventure",
    "young adult fiction", "juvenile fiction", "children's fiction",
    "children's stories", "juvenile literature", "picture books",
    "middle grade", "graphic novel", "comics", "manga", "fairy tales",
    "biography", "autobiography", "memoir", "personal memoirs",
    "history", "philosophy", "psychology", "science", "business", "economics",
    "self-help", "true crime", "travel", "poetry", "drama", "classics",
    "literary", "short stories", "humor", "satire",
})

# Editions that are about a book rather than being it. Frankenstein resolved to
# a study guide, whose author is still Mary Shelley, so the author hint doesn't
# catch this — and its recommendations were Kaplan SAT prep.
EXCLUDED_EDITION_ATOMS = frozenset({
    "study guides", "examinations", "criticism", "interpretation",
    "summary", "summaries", "abridged", "outlines", "notes",
    "questions and answers", "handbooks", "textbooks",
})

# Share of results that must have a core genre in common with the source.
# Below this the page is matched on something incidental — An Ember in the
# Ashes shares "juvenile fiction" with nothing it recommended, having been
# matched on "brothers and sisters".
MIN_GENRE_ECHO = 0.5


def has_core_genre(specific_atoms: set[str]) -> bool:
    """Whether a book's *specific* genre atoms name a real genre.

    Takes the specific bucket from _genre_atoms, not everything: the generic
    bucket is where bare "fiction" lands, and "this is fiction" is not a genre
    a recommendation can be built on.
    """
    return bool(specific_atoms & CORE_GENRES)


def is_excluded_edition(atoms: set[str]) -> bool:
    return bool(atoms & EXCLUDED_EDITION_ATOMS)


def genre_echo(source_atoms: set[str], result_atoms: list[set[str]]) -> float:
    """Share of results sharing at least one core genre with the source."""
    if not result_atoms:
        return 0.0
    source_core = source_atoms & CORE_GENRES
    if not source_core:
        return 0.0
    return sum(1 for a in result_atoms if a & source_core) / len(result_atoms)


def should_publish(
    results: list[dict],
    source_atoms: set[str] | None = None,
    result_atoms: list[set[str]] | None = None,
    *,
    min_results: int = MIN_PAGE_RESULTS,
    min_top_relevance: float = MIN_TOP_RELEVANCE,
    min_genre_echo: float = MIN_GENRE_ECHO,
) -> tuple[bool, str]:
    """Whether a generated result set should become a public page.

    Returns (ok, reason) so the generator can say *why* it skipped — with a
    couple of hundred seeds, "below gate" alone isn't diagnosable.

    Three bars: enough recommendations to be worth the visit, a best match
    that's actually a match, and results that share the source's genre rather
    than an incidental subject facet. The third is the one that separates a
    real page from a confident-sounding wrong one; the first two only measure
    the scorer's own opinion of itself.

    Genre checking is skipped when atoms aren't supplied, which keeps the
    length/score bars usable on their own.
    """
    if len(results) < min_results:
        return False, f"only {len(results)} results (need {min_results})"

    top = max((r.get("relevance") or 0.0) for r in results)
    if top < min_top_relevance:
        return False, f"top match {top:.0f}% (need {min_top_relevance:.0f}%)"

    if source_atoms is not None and result_atoms is not None:
        echo = genre_echo(source_atoms, result_atoms)
        if echo < min_genre_echo:
            shared = sorted(source_atoms & CORE_GENRES)
            return False, (
                f"only {echo:.0%} of results share a genre with the source"
                f" (source genres: {', '.join(shared) or 'none'})"
            )

    return True, ""


def page_url(slug: str, base_url: str = DEFAULT_BASE_URL) -> str:
    return f"{base_url.rstrip('/')}/books-like/{slug}"


def sitemap_xml(entries: list[tuple[str, int]], base_url: str = DEFAULT_BASE_URL) -> str:
    """Sitemap listing the hub page plus every published slug.

    `entries` is (slug, generated_at unix seconds). Submitting this in Google
    Search Console is what gets the whole set crawled — without it discovery
    depends on the internal links alone.
    """
    import time

    root = base_url.rstrip("/")
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        f"  <url><loc>{xml_escape(root)}/</loc><priority>1.0</priority></url>",
        f"  <url><loc>{xml_escape(root)}/books-like</loc>"
        f"<priority>0.9</priority></url>",
    ]
    for slug, generated_at in entries:
        lastmod = time.strftime("%Y-%m-%d", time.gmtime(generated_at))
        lines.append(
            f"  <url><loc>{xml_escape(page_url(slug, base_url))}</loc>"
            f"<lastmod>{lastmod}</lastmod><priority>0.8</priority></url>"
        )
    lines.append("</urlset>")
    return "\n".join(lines)


def robots_txt(base_url: str = DEFAULT_BASE_URL) -> str:
    """Allow everything, point at the sitemap, keep crawlers off the API.

    The POST endpoints aren't crawlable anyway, but /admin and /docs are GETs
    that shouldn't show up in results.
    """
    root = base_url.rstrip("/")
    return (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin\n"
        "Disallow: /docs\n"
        "Disallow: /redoc\n"
        "Disallow: /openapi.json\n"
        f"\nSitemap: {root}/sitemap.xml\n"
    )
