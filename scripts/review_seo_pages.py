"""Flag generated pages whose recommendations look wrong.

The publish gate in server/seo.py checks *scores* — enough results, strong
enough top match. It can't see that a page is confidently about the wrong
thing: The Fifth Season scores fine while recommending books that match N. K.
Jemisin's Open Library facet tags ("mothers and daughters", "lgbtq") instead of
her fantasy. That failure only shows up by comparing the source's genre
identity against what the results actually share.

This is a review aid, not a gate — it ranks pages by how suspicious they look
so a human can skim the top of the list instead of all 200. Read-only.

    python -m scripts.review_seo_pages            # flagged pages only
    python -m scripts.review_seo_pages --all      # every page, worst first
    python -m scripts.review_seo_pages --show 5   # results shown per page
"""

from __future__ import annotations

import argparse
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from server.app import _genre_atoms
from server.storage.seo_db import SeoPageStore

# A page is suspicious when few of its results share a *mainstream* genre with
# the source. "Does the page share the source's tags" is the wrong question —
# that's precisely what the scorer maximises, so every page scores well on it,
# including the broken ones.
GENRE_ECHO_THRESHOLD = 0.5
# ...or when most results carry no explanation at all, which means the scorer
# found nothing nameable in common.
REASON_COVERAGE_THRESHOLD = 0.5

# What counts as "mainstream" is derived from the corpus rather than hardcoded:
# an atom that shows up as the connective tissue on many different pages is a
# genre, one that appears on a single page is an incidental facet. Real genres
# recur ("fantasy" links dozens of pages); "mothers and daughters" links one.
# Needs a decent page count to calibrate — below this it falls back to treating
# every atom as core, i.e. flags nothing on genre grounds.
CORE_ATOM_MIN_PAGES = 4
MIN_PAGES_TO_CALIBRATE = 25


def _atoms(tags: list[str]) -> set[str]:
    specific, generic = _genre_atoms(tags or [])
    return set(specific) | set(generic)


def shared_atoms(page: dict) -> list[set[str]]:
    """Per result, the genre atoms it has in common with the source."""
    source_atoms = _atoms(page["source"].get("tags", []))
    return [source_atoms & _atoms(item.get("tags", [])) for item in page["results"]]


def core_atoms(pages: list[dict]) -> set[str]:
    """Atoms that connect several different pages — the real genre vocabulary."""
    page_count: dict[str, int] = {}
    for page in pages:
        for atom in set().union(*shared_atoms(page)) if page["results"] else set():
            page_count[atom] = page_count.get(atom, 0) + 1
    return {a for a, n in page_count.items() if n >= CORE_ATOM_MIN_PAGES}


def assess(page: dict, core: set[str] | None) -> dict:
    """Score one page's coherence. Higher `suspicion` = review it."""
    results = page["results"]
    if not results:
        return {"suspicion": 1.0, "echo": 0.0, "reasons": 0.0, "shared": []}

    overlaps = shared_atoms(page)
    # With too few pages to calibrate, every atom counts as core and this
    # reduces to "did the result share any genre at all".
    echoes = sum(
        1 for ov in overlaps if (ov & core if core is not None else ov)
    )
    shared_counts: dict[str, int] = {}
    for ov in overlaps:
        for atom in ov:
            shared_counts[atom] = shared_counts.get(atom, 0) + 1

    echo = echoes / len(results)
    reasons = sum(1 for r in results if r.get("reason")) / len(results)
    # Weighted toward genre echo: a missing reason line is cosmetic, a page
    # whose results don't share the source's genre is about the wrong thing.
    suspicion = (1 - echo) * 0.7 + (1 - reasons) * 0.3
    top_shared = sorted(shared_counts.items(), key=lambda kv: -kv[1])[:4]
    return {
        "suspicion": suspicion,
        "echo": echo,
        "reasons": reasons,
        "shared": [
            a + ("" if core is None or a in core else " (niche)")
            for a, _ in top_shared
        ],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Flag suspicious generated pages.")
    ap.add_argument("--all", action="store_true", help="Show every page, not just flagged")
    ap.add_argument("--show", type=int, default=3, help="Results to print per page")
    args = ap.parse_args()

    store = SeoPageStore()
    pages = store.list_pages()
    if not pages:
        raise SystemExit("No pages generated yet.")

    full = [store.get(meta["slug"]) for meta in pages]
    core = core_atoms(full) if len(full) >= MIN_PAGES_TO_CALIBRATE else None
    assessed = sorted(
        ((assess(page, core), page) for page in full),
        key=lambda pair: -pair[0]["suspicion"],
    )

    flagged = [
        (a, p) for a, p in assessed
        if a["echo"] < GENRE_ECHO_THRESHOLD or a["reasons"] < REASON_COVERAGE_THRESHOLD
    ]
    shown = assessed if args.all else flagged

    if core is None:
        print(f"(only {len(full)} pages — too few to tell genres from facet "
              f"tags, so genre flagging is disabled)")
    print(f"{len(pages)} pages | {len(flagged)} flagged for review "
          f"(genre echo <{GENRE_ECHO_THRESHOLD:.0%} or explained <"
          f"{REASON_COVERAGE_THRESHOLD:.0%})\n")

    for a, page in shown:
        src = page["source"]
        print(f"/{page['slug']}  ({a['echo']:.0%} share a genre, "
              f"{a['reasons']:.0%} explained)")
        print(f"    source: {src.get('title')} — {', '.join(src.get('authors') or []) or '?'}")
        print(f"    source genres: {sorted(_atoms(src.get('tags', [])))[:6] or '(none)'}")
        print(f"    matched on:    {a['shared'] or '(nothing shared)'}")
        for item in page["results"][: args.show]:
            authors = ", ".join(item.get("authors") or []) or "?"
            print(f"      {item.get('relevance', 0):>4.0f}%  {item.get('title', '')[:44]} — {authors[:24]}")
        print()

    if not args.all and not flagged:
        print("Nothing flagged. Spot-check a few by hand anyway — this only "
              "catches pages that disagree with their own source.")


if __name__ == "__main__":
    main()
