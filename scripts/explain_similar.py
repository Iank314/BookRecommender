"""Explain why "Find Similar" ranks what it ranks for a given book.

This is the tuning loop for recommendation quality: run it on a book that gave
a bad recommendation, read the breakdown — genre overlap vs description F1 vs
popularity, and which shared tokens drove each match — then adjust the right
lever (stopwords, genre synonyms, blend weights, candidate queries). It calls
the exact pipeline the /similar endpoint uses, so what it shows is what users
get. See CLAUDE.md "Recommendation quality tuning" for the lever list.

Usage:
    python -m scripts.explain_similar "Dungeon Crawler Carl"
    python -m scripts.explain_similar "Mistborn" --top 10
"""

from __future__ import annotations

import argparse
import math
import sys

# Windows consoles default to cp1252; book titles and the breakdown output
# use em-dashes and Unicode regularly.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from scripts._lookup import find_source as _find_source
from server.app import (
    SimilarRequest,
    _book_popularity,
    _compute_token_idf,
    _gather_similar_candidates,
    _genre_atoms,
    _genre_score,
    _idf_weighted_f1,
    _score_similar_candidates,
    _text_tokens,
)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Explain Find Similar rankings for a book.")
    ap.add_argument("title", help="Book title to explain (best match is used)")
    ap.add_argument("--top", type=int, default=15, help="How many results to show")
    args = ap.parse_args()

    print(f"Searching for {args.title!r}...")
    src = _find_source(args.title)
    if not src:
        raise SystemExit(f"No book found for {args.title!r}.")

    print(f'\nSource: "{src.title}" — {", ".join(src.authors) or "unknown author"}')
    print(f"  raw tags:     {src.tags or '(none)'}")
    print(f"  description:  {len(src.description)} chars")

    req = SimilarRequest(
        id=src.id, title=src.title, authors=src.authors,
        description=src.description, tags=src.tags,
    )
    print("\nGathering candidates (live fetch, takes a few seconds)...")
    source_book, src_lang, candidates = _gather_similar_candidates(req)
    # The gather step may have enriched a sparse source — show what the scorer
    # actually sees, not what the search result carried.
    src_spec = set(_genre_atoms(source_book.tags)[0])
    print(f"  after enrichment: genre atoms {sorted(src_spec) or '(none)'}, "
          f"description {len(source_book.description)} chars")
    print(f"  language: {src_lang or '?'} | candidates after filters: {len(candidates)}")
    if not candidates:
        raise SystemExit("No candidates survived the filters.")

    scored = _score_similar_candidates(source_book, candidates)
    if not scored:
        raise SystemExit("No candidate scored above zero.")

    # Recompute the scorer's internals so each line can show its breakdown.
    idf = _compute_token_idf(candidates)
    default_idf = math.log(len(candidates) + 1) + 1
    src_text = _text_tokens(source_book)

    for rank, (cand, final) in enumerate(scored[: args.top], start=1):
        cand_text = _text_tokens(cand)
        desc = _idf_weighted_f1(src_text, cand_text, idf, default_idf)
        cand_genres = set(_genre_atoms(cand.tags)[0])
        genre = (
            f"{_genre_score(cand_genres, src_spec):.3f}"
            if cand_genres and src_spec else "n/a"
        )
        shared = sorted(
            src_text & cand_text, key=lambda t: -idf.get(t, default_idf),
        )[:8]
        print(f"\n{rank:2}. {cand.title} — {', '.join(cand.authors) or '?'}")
        print(f"    final {final:.3f} | desc F1 {desc:.3f} | genre {genre} "
              f"| pop {_book_popularity(cand):.2f}")
        print(f"    genres: {sorted(cand_genres) or '(tagless)'}")
        print(f"    top shared tokens: {', '.join(shared) or '(none)'}")


if __name__ == "__main__":
    main()
