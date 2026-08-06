"""Generate the public "books like X" pages.

Run offline (locally or on the server), never in a request: each book means a
full live fetch from Google Books + Open Library and takes several seconds.
The finished result sets go into the `seo_pages` table, and the web route just
reads them — which is what keeps a crawler walking 200 URLs from turning into
thousands of provider calls.

    python -m scripts.generate_seo_pages                 # new seeds only
    python -m scripts.generate_seo_pages --force         # regenerate everything
    python -m scripts.generate_seo_pages --limit 10      # a first batch
    python -m scripts.generate_seo_pages --only "Dune"   # one title
    python -m scripts.generate_seo_pages --dry-run       # score, publish nothing

Seeds live in scripts/seo_seeds.txt (one title per line, # for comments). That
path is deliberate: data/ is a mounted volume in production, so a seed file
there would be shadowed at runtime and missing inside the container.

Re-run it periodically (weekly is plenty) so pages pick up new books as the
providers index them. Nothing breaks if it doesn't run — pages just age.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from scripts._lookup import find_source
from server.app import SimilarRequest, _genre_atoms, similar_page_data
from server.seo import (
    MIN_PAGE_RESULTS,
    MIN_TOP_RELEVANCE,
    has_core_genre,
    is_excluded_edition,
    should_publish,
    slugify,
)
from server.storage.seo_db import SeoPageStore

SEEDS_PATH = Path(__file__).resolve().parent / "seo_seeds.txt"

# Providers are rate-limited and this is a batch job with nobody waiting on it,
# so pause between books rather than risk a 429 mid-run.
PAUSE_SECONDS = 1.5


def load_seeds(path: Path) -> list[tuple[str, str | None]]:
    """(title, author) pairs in file order, minus comments/blanks/duplicates.

    Lines are `Title | Author`; the author is optional but strongly advised —
    it's what stops "The Fifth Season" resolving to a detective novel.
    """
    if not path.exists():
        raise SystemExit(f"No seed file at {path}")
    seen: set[str] = set()
    seeds: list[tuple[str, str | None]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#")[0].strip()
        if not line:
            continue
        title, _, author = line.partition("|")
        title, author = title.strip(), author.strip() or None
        if not title:
            continue
        key = title.lower()
        if key not in seen:
            seen.add(key)
            seeds.append((title, author))
    return seeds


def generate_one(
    title: str, author: str | None, top_n: int
) -> tuple[str, dict | None, list[dict], str]:
    """Resolve one seed title and score it. Returns (slug, source, results, note).

    Never raises: a provider hiccup on book 40 of 200 shouldn't lose the other
    160, so failures come back as a note and the caller keeps going.
    """
    try:
        source = find_source(title, author, quiet=True)
    except Exception as exc:
        return "", None, [], f"lookup failed ({exc})"
    if source is None:
        return "", None, [], (
            f"no provider match{f' for author {author}' if author else ''}"
        )

    slug = slugify(source.title)
    if not slug:
        return "", None, [], f"title produced an empty slug ({source.title!r})"

    # Check the source record *before* scoring: it's the expensive step, and a
    # record with no genre or the wrong edition can't produce a good page no
    # matter how well it scores.
    specific, generic = _genre_atoms(source.tags or [])
    atoms = set(specific) | set(generic)
    if is_excluded_edition(atoms):
        return slug, None, [], (
            f"resolved to a study-guide/criticism edition ({sorted(atoms)[:3]})"
        )
    if not has_core_genre(set(specific)):
        return slug, None, [], (
            f"source record carries no genre, only subject facets "
            f"({sorted(atoms)[:4]})"
        )

    req = SimilarRequest(
        id=source.id, title=source.title, authors=source.authors,
        description=source.description, tags=source.tags, top_n=top_n,
    )
    try:
        source_out, results = similar_page_data(req)
    except Exception as exc:
        return slug, None, [], f"scoring failed ({exc})"
    return slug, source_out, results, ""


def page_atoms(source: dict, results: list[dict]) -> tuple[set[str], list[set[str]]]:
    """(source atoms, per-result atoms) for the genre bars in should_publish.

    Works off the same dicts that get stored, so --prune can re-apply the gate
    to pages already in the database without re-fetching anything.
    """
    def atoms(tags: list[str]) -> set[str]:
        specific, generic = _genre_atoms(tags or [])
        return set(specific) | set(generic)

    return atoms(source.get("tags", [])), [atoms(r.get("tags", [])) for r in results]


def prune(store: SeoPageStore, min_results: int, dry_run: bool) -> None:
    """Re-apply the current gate to already-published pages, unpublishing fails.

    The stored payload holds every tag the gate looks at, so tightening the
    gate doesn't mean re-running a multi-hour fetch — worth having, because the
    gate has needed tightening every time a batch got reviewed.
    """
    removed = 0
    for meta in store.list_pages():
        page = store.get(meta["slug"])
        source, results = page["source"], page["results"]

        specific, generic = _genre_atoms(source.get("tags", []))
        atoms = set(specific) | set(generic)
        if is_excluded_edition(atoms):
            why = f"study-guide/criticism edition ({sorted(atoms)[:3]})"
        elif not has_core_genre(set(specific)):
            why = f"source has no genre, only facets ({sorted(atoms)[:4]})"
        else:
            ok, reason = should_publish(
                results, *page_atoms(source, results), min_results=min_results,
            )
            if ok:
                continue
            why = reason

        print(f"  {'would remove' if dry_run else 'removed'} /{meta['slug']}: {why}")
        if not dry_run:
            store.delete(meta["slug"])
        removed += 1

    verb = "Would remove" if dry_run else "Removed"
    remaining = store.count() - removed if dry_run else store.count()
    print(f"\n{verb} {removed} page(s). Pages that pass the gate: {remaining}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate public 'books like X' pages.")
    ap.add_argument("--force", action="store_true",
                    help="Regenerate pages that already exist")
    ap.add_argument("--limit", type=int, help="Stop after this many seeds")
    ap.add_argument("--only", help="Generate a single title (ignores the seed file)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Score and report, but write nothing")
    ap.add_argument("--top-n", type=int, default=20,
                    help="Recommendations per page (default 20)")
    ap.add_argument("--min-results", type=int, default=MIN_PAGE_RESULTS,
                    help=f"Publish gate: minimum results (default {MIN_PAGE_RESULTS})")
    ap.add_argument("--prune", action="store_true",
                    help="Re-apply the gate to existing pages and unpublish "
                         "failures (no fetching). Combine with --dry-run.")
    args = ap.parse_args()

    store = SeoPageStore()
    if args.prune:
        prune(store, args.min_results, args.dry_run)
        return
    if args.only:
        title, _, author = args.only.partition("|")
        seeds = [(title.strip(), author.strip() or None)]
    else:
        seeds = load_seeds(SEEDS_PATH)
    if args.limit:
        seeds = seeds[: args.limit]

    existing = store.published_slugs()
    published = skipped = failed = 0
    started = time.time()

    print(f"{len(seeds)} seed title(s) | gate: >={args.min_results} results, "
          f"top match >={MIN_TOP_RELEVANCE:.0f}%"
          f"{' | DRY RUN' if args.dry_run else ''}\n")

    for i, (title, author) in enumerate(seeds, start=1):
        prefix = f"[{i}/{len(seeds)}] {title}"

        # Cheap pre-check: skip an already-published seed before paying for the
        # lookup. Only approximate (the slug comes from the *resolved* title),
        # so generate_one re-checks below once the real slug is known.
        if not args.force and slugify(title) in existing:
            print(f"{prefix}: already published, skipping")
            skipped += 1
            continue

        slug, source, results, note = generate_one(title, author, args.top_n)
        if note:
            print(f"{prefix}: FAILED — {note}")
            failed += 1
            continue
        if not args.force and slug in existing:
            print(f"{prefix}: already published as /{slug}, skipping")
            skipped += 1
            continue

        top = max((r.get("relevance") or 0) for r in results) if results else 0
        ok, why = should_publish(
            results, *page_atoms(source, results), min_results=args.min_results,
        )
        if not ok:
            print(f"{prefix}: below gate — {why}")
            skipped += 1
            continue

        if args.dry_run:
            print(f"{prefix}: would publish /books-like/{slug} "
                  f"({len(results)} results, top {top:.0f}%)")
        else:
            store.upsert(slug, source, results)
            existing.add(slug)
            print(f"{prefix}: published /books-like/{slug} "
                  f"({len(results)} results, top {top:.0f}%)")
        published += 1

        if i < len(seeds):
            time.sleep(PAUSE_SECONDS)

    elapsed = time.time() - started
    print(f"\n{'Would publish' if args.dry_run else 'Published'}: {published} | "
          f"skipped: {skipped} | failed: {failed} | {elapsed / 60:.1f} min")
    if not args.dry_run:
        print(f"Total pages live: {store.count()}")


if __name__ == "__main__":
    main()
