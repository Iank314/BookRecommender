"""Public "books like X" pages: slugs, the publish gate, storage, rendering.

Follows the suite's convention of testing pure helpers rather than live
endpoints — nothing here touches a provider. The rendering tests call the
Jinja templates directly (no TestClient/httpx dependency) which is enough to
catch the failures that matter for these pages: a missing canonical tag, an
unescaped title, or structured data that disagrees with the visible list.
"""

from pathlib import Path

import pytest

from server.seo import (
    MIN_PAGE_RESULTS,
    genre_echo,
    has_core_genre,
    is_excluded_edition,
    page_url,
    robots_txt,
    should_publish,
    sitemap_xml,
    slugify,
)
from server.storage.seo_db import SeoPageStore


# ---------------------------------------------------------------- slugify

@pytest.mark.parametrize("title,expected", [
    ("The Name of the Wind", "the-name-of-the-wind"),
    ("Mistborn: The Final Empire", "mistborn-the-final-empire"),
    ("Ender's Game", "ender-s-game"),
    ("  Dune  ", "dune"),
    ("Slaughterhouse-Five", "slaughterhouse-five"),
    ("1984", "1984"),
])
def test_slugify_basic(title, expected):
    assert slugify(title) == expected


def test_slugify_folds_accents_to_ascii():
    # Provider titles carry accents; the URL has to survive copy/paste.
    assert slugify("La Sombra del Viento — Zafón") == "la-sombra-del-viento-zafon"


def test_slugify_returns_empty_for_unusable_titles():
    # Callers treat "" as not-publishable rather than writing an empty slug.
    assert slugify("日本語") == ""
    assert slugify("!!!") == ""
    assert slugify("") == ""


def test_slugify_truncates_at_a_word_boundary():
    slug = slugify("word " * 40)
    assert len(slug) <= 80
    assert not slug.endswith("-")
    assert slug.split("-")[-1] == "word"  # no half-word tail


def test_slugify_truncates_a_single_overlong_word():
    # The boundary-seeking branch must not return a stub when there's no dash
    # to break on.
    slug = slugify("x" * 200)
    assert len(slug) == 80


# ------------------------------------------------------------ publish gate

def _results(n, top=90.0):
    """n results whose best match is `top` — the rest trail below it."""
    return [
        {"title": f"B{i}", "relevance": top if i == 0 else top * 0.5}
        for i in range(n)
    ]


def test_gate_accepts_a_strong_page():
    ok, why = should_publish(_results(MIN_PAGE_RESULTS))
    assert ok is True and why == ""


def test_gate_rejects_too_few_results():
    ok, why = should_publish(_results(MIN_PAGE_RESULTS - 1))
    assert ok is False and "results" in why


def test_gate_rejects_a_full_list_of_weak_matches():
    # The failure mode this exists for: a sparse-metadata book returns a full
    # list of near-ties, none of which is actually a match.
    ok, why = should_publish(_results(20, top=8.0))
    assert ok is False and "top match" in why


def test_gate_tolerates_missing_relevance():
    assert should_publish([{"title": "B"} for _ in range(20)])[0] is False


# ------------------------------------------------------ genre bars

def test_source_with_only_subject_facets_has_no_core_genre():
    # Ender's Game resolved to ['end of the world', 'military education',
    # 'prize:nebula'] and recommended Army training manuals.
    assert has_core_genre({"end of the world", "military education"}) is False
    assert has_core_genre({"fantasy", "assassins"}) is True


def test_bare_fiction_does_not_count_as_a_genre():
    # Shutter Island's only genre-ish atom was "fiction"; its page was US
    # Marshals paperwork. "fiction" lives in the generic bucket for a reason.
    assert has_core_genre({"fiction"}) is False
    assert has_core_genre({"general"}) is False


def test_study_guide_editions_are_excluded():
    # Frankenstein resolved to a study guide — same author, so the author hint
    # can't catch it — and recommended Kaplan SAT prep.
    assert is_excluded_edition({"examinations", "study guides"}) is True
    assert is_excluded_edition({"fantasy"}) is False


def test_genre_echo_measures_shared_core_genres_only():
    source = {"fantasy", "brothers and sisters"}
    # Results share the facet, not the genre — An Ember in the Ashes' failure.
    assert genre_echo(source, [{"brothers and sisters"}] * 4) == 0.0
    assert genre_echo(source, [{"fantasy"}] * 4) == 1.0
    assert genre_echo(source, [{"fantasy"}, {"brothers and sisters"}]) == 0.5


def test_genre_echo_is_zero_when_the_source_has_no_genre():
    assert genre_echo({"military education"}, [{"military education"}] * 4) == 0.0


def test_gate_rejects_a_page_matched_on_a_facet():
    ok, why = should_publish(
        _results(20), {"fantasy", "brothers and sisters"},
        [{"brothers and sisters"}] * 20,
    )
    assert ok is False and "share a genre" in why


def test_gate_accepts_a_page_matched_on_a_real_genre():
    ok, why = should_publish(_results(20), {"fantasy"}, [{"fantasy"}] * 20)
    assert ok is True and why == ""


def test_gate_skips_genre_checks_when_atoms_are_absent():
    # The length/score bars stay usable on their own.
    assert should_publish(_results(20))[0] is True


# ----------------------------------------------------------------- storage

@pytest.fixture
def seo_store(tmp_path: Path) -> SeoPageStore:
    return SeoPageStore(db_path=tmp_path / "seo_test.db")


def test_store_roundtrip(seo_store):
    source = {"title": "Dune", "authors": ["Frank Herbert"], "metadata": {}}
    results = [{"title": "Hyperion", "relevance": 61.0, "reason": "Both are sci-fi."}]
    seo_store.upsert("dune", source, results)

    page = seo_store.get("dune")
    assert page["source_title"] == "Dune"
    assert page["source"]["authors"] == ["Frank Herbert"]
    assert page["results"][0]["reason"] == "Both are sci-fi."
    assert page["generated_at"] > 0


def test_store_get_returns_none_for_unpublished(seo_store):
    assert seo_store.get("never-generated") is None


def test_store_upsert_replaces_wholesale(seo_store):
    # Regeneration must not merge old results into new ones — a page that has
    # dropped a bad recommendation should actually lose it.
    seo_store.upsert("dune", {"title": "Dune"}, [{"title": "Old"}])
    seo_store.upsert("dune", {"title": "Dune"}, [{"title": "New"}])

    page = seo_store.get("dune")
    assert [r["title"] for r in page["results"]] == ["New"]
    assert seo_store.count() == 1


def test_store_lists_and_deletes(seo_store):
    seo_store.upsert("zeta", {"title": "Zeta"}, [{"title": "x"}])
    seo_store.upsert("alpha", {"title": "Alpha"}, [{"title": "y"}])

    assert [p["title"] for p in seo_store.list_pages()] == ["Alpha", "Zeta"]
    assert seo_store.published_slugs() == {"alpha", "zeta"}
    assert seo_store.delete("alpha") is True
    assert seo_store.delete("alpha") is False
    assert seo_store.count() == 1


# ------------------------------------------------------- sitemap / robots

def test_sitemap_lists_every_page_with_absolute_urls():
    xml = sitemap_xml([("dune", 1_700_000_000), ("mistborn", 1_700_000_000)],
                      "https://example.com")
    assert xml.startswith("<?xml")
    assert "https://example.com/books-like/dune" in xml
    assert "https://example.com/books-like/mistborn" in xml
    assert "https://example.com/books-like<" in xml  # the hub page
    assert xml.count("<url>") == 4                   # home + hub + 2 pages


def test_sitemap_is_valid_xml_and_survives_a_trailing_slash_base():
    import xml.etree.ElementTree as ET

    xml = sitemap_xml([("dune", 1_700_000_000)], "https://example.com/")
    ET.fromstring(xml)                     # raises if malformed
    assert "example.com//" not in xml


def test_robots_points_at_the_sitemap_and_hides_the_api():
    txt = robots_txt("https://example.com")
    assert "Sitemap: https://example.com/sitemap.xml" in txt
    assert "Disallow: /admin" in txt
    assert "Allow: /" in txt


def test_page_url_builds_a_canonical_link():
    assert page_url("dune", "https://example.com/") == "https://example.com/books-like/dune"


# --------------------------------------------------------------- rendering

def _render(name, **ctx):
    from server.app import SEO_ASSET_VERSION, _templates

    return _templates.get_template(name).render(asset_version=SEO_ASSET_VERSION, **ctx)


def _page_ctx(**overrides):
    ctx = {
        "source": {
            "title": "Dune",
            "authors": ["Frank Herbert"],
            "description": "A desert planet.",
            "metadata": {"thumbnail": "http://books.google.com/cover.jpg"},
        },
        "source_title": "Dune",
        "results": [
            {
                "title": "Hyperion", "authors": ["Dan Simmons"],
                "description": "Pilgrims travel.", "relevance": 61.4,
                "reason": "Both are science fiction.", "metadata": {}, "slug": "hyperion",
            },
            {
                "title": "Leviathan Wakes", "authors": ["James S. A. Corey"],
                "description": "A missing girl.", "relevance": 44.0,
                "reason": "", "metadata": {}, "slug": None,
            },
        ],
        "canonical": "https://example.com/books-like/dune",
        "generated_at": 1_700_000_000,
        "jsonld": {"@type": "ItemList", "numberOfItems": 2},
    }
    ctx.update(overrides)
    return ctx


def test_page_renders_seo_essentials():
    html = _render("books_like.html", **_page_ctx())
    assert "<title>Books Like Dune — 2 Recommendations</title>" in html
    assert '<link rel="canonical" href="https://example.com/books-like/dune" />' in html
    assert 'property="og:title"' in html
    assert 'application/ld+json' in html
    assert "Hyperion" in html and "Leviathan Wakes" in html


def test_page_links_onward_only_when_the_target_page_exists():
    # A link to an unpublished slug would be a crawlable 404.
    html = _render("books_like.html", **_page_ctx())
    assert '/books-like/hyperion' in html
    assert '/books-like/None' not in html


def test_page_upgrades_provider_covers_to_https():
    # Google Books hands back http:// thumbnails; on an https page the browser
    # blocks them as mixed content and the covers silently vanish.
    html = _render("books_like.html", **_page_ctx())
    assert "http://books.google.com" not in html
    assert "https://books.google.com/cover.jpg" in html


def test_page_escapes_titles_from_providers():
    ctx = _page_ctx(source_title='Dune <script>alert(1)</script>')
    html = _render("books_like.html", **ctx)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_page_renders_a_result_with_no_reason_or_cover():
    # Both fields are optional in the stored payload; a sparse result must not
    # blow up the whole page.
    html = _render("books_like.html", **_page_ctx())
    assert "Leviathan Wakes" in html


def test_index_page_lists_every_published_page():
    html = _render(
        "books_like_index.html",
        pages=[{"slug": "dune", "title": "Dune", "generated_at": 1},
               {"slug": "hyperion", "title": "Hyperion", "generated_at": 1}],
        canonical="https://example.com/books-like",
    )
    assert "/books-like/dune" in html and "/books-like/hyperion" in html
    assert "2 reading lists" in html


def test_index_page_handles_an_empty_site():
    html = _render("books_like_index.html", pages=[], canonical="x")
    assert "No lists published yet" in html


def test_not_found_template_renders():
    assert "No list for that book" in _render("not_found.html")


# ------------------------------------------------------------- structured data

def test_jsonld_mirrors_the_rendered_list():
    from server.app import _books_like_jsonld

    data = _books_like_jsonld("Dune", _page_ctx()["results"])
    assert data["@type"] == "ItemList"
    assert data["numberOfItems"] == 2
    assert [i["position"] for i in data["itemListElement"]] == [1, 2]
    assert data["itemListElement"][0]["item"]["name"] == "Hyperion"
    # Only the published one carries a URL.
    assert "url" in data["itemListElement"][0]["item"]
    assert "url" not in data["itemListElement"][1]["item"]


# ------------------------------------------------------------------- seeds

def test_seed_file_is_loadable_and_slugs_cleanly():
    from scripts.generate_seo_pages import SEEDS_PATH, load_seeds

    seeds = load_seeds(SEEDS_PATH)
    assert len(seeds) > 100
    assert all(slugify(t) for t, _ in seeds), "every seed must produce a usable slug"
    lowered = [t.lower() for t, _ in seeds]
    assert len(lowered) == len(set(lowered)), "seed titles must be unique"


def test_every_seed_carries_an_author():
    # A bare title resolves to whatever the provider ranks first, which is how
    # "The Fifth Season" became a detective novel. Authors are the guard.
    from scripts.generate_seo_pages import SEEDS_PATH, load_seeds

    missing = [t for t, author in load_seeds(SEEDS_PATH) if not author]
    assert not missing, f"seeds without an author: {missing}"


def test_seed_loader_parses_authors_and_strips_comments(tmp_path):
    path = tmp_path / "seeds.txt"
    path.write_text(
        "# a comment\n\nDune | Frank Herbert\n"
        "Mistborn|Brandon Sanderson  # inline comment\n"
        "Untitled\ndune | someone else\n",
        encoding="utf-8",
    )
    from scripts.generate_seo_pages import load_seeds

    assert load_seeds(path) == [
        ("Dune", "Frank Herbert"),
        ("Mistborn", "Brandon Sanderson"),
        ("Untitled", None),
    ]


# --------------------------------------------------------- review heuristics

def _page(source_tags, result_tags, reasons=True):
    return {
        "slug": "s",
        "source": {"title": "Src", "authors": ["A"], "tags": source_tags},
        "results": [
            {"title": f"R{i}", "authors": ["B"], "tags": tags,
             "reason": "because" if reasons else ""}
            for i, tags in enumerate(result_tags)
        ],
    }


def test_review_flags_a_page_matched_only_on_a_niche_facet():
    # The Fifth Season failure: results share the source's tags, but the tag
    # doing the work is an incidental facet rather than the book's genre.
    from scripts.review_seo_pages import assess

    page = _page(["Fantasy", "Mothers and daughters"],
                 [["Mothers and daughters"]] * 6)
    verdict = assess(page, core={"fantasy"})
    assert verdict["echo"] == 0.0
    assert verdict["suspicion"] > 0.5
    assert any("(niche)" in s for s in verdict["shared"])


def test_review_passes_a_page_matched_on_a_real_genre():
    from scripts.review_seo_pages import assess

    page = _page(["Fantasy"], [["Fantasy"]] * 6)
    verdict = assess(page, core={"fantasy"})
    assert verdict["echo"] == 1.0
    assert verdict["suspicion"] == 0.0


def test_review_is_not_circular():
    # Guard against the first version of this heuristic, which asked "do the
    # results share the source's tags" — the exact thing the scorer maximises,
    # so every page passed including the broken ones.
    from scripts.review_seo_pages import assess

    broken = _page(["Niche Facet"], [["Niche Facet"]] * 6)
    assert assess(broken, core={"fantasy"})["echo"] == 0.0


def test_review_core_atoms_need_several_pages_each():
    from scripts.review_seo_pages import CORE_ATOM_MIN_PAGES, core_atoms

    common = [_page(["Fantasy"], [["Fantasy"]]) for _ in range(CORE_ATOM_MIN_PAGES)]
    rare = [_page(["Oddity"], [["Oddity"]])]
    assert core_atoms(common + rare) == {"fantasy"}


def test_review_flags_pages_with_no_explanations():
    from scripts.review_seo_pages import assess

    page = _page(["Fantasy"], [["Fantasy"]] * 6, reasons=False)
    assert assess(page, core={"fantasy"})["reasons"] == 0.0
    assert assess(page, core={"fantasy"})["suspicion"] > 0


def test_review_handles_an_empty_result_set():
    from scripts.review_seo_pages import assess

    assert assess(_page(["Fantasy"], []), core=set())["suspicion"] == 1.0


@pytest.mark.parametrize("authors,wanted,expected", [
    (["J. K. Rowling"], "J.K. Rowling", True),
    (["Rowling, J. K."], "J. K. Rowling", True),
    (["N. K. Jemisin"], "N. K. Jemisin", True),
    (["Don Bredes"], "N. K. Jemisin", False),
    ([], "Frank Herbert", False),
    (["Brandon Sanderson"], "sanderson", True),
])
def test_author_matching_survives_provider_formatting(authors, wanted, expected):
    from conftest import make_book
    from scripts._lookup import _author_matches

    book = make_book("id", "T")
    book.authors = authors
    assert _author_matches(book, wanted) is expected
