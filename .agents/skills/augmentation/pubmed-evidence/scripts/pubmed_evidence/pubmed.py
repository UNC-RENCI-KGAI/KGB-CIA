"""Step 2: search PubMed for each pair and fetch the abstracts, with `shared.pubmed`.

`shared.pubmed.search_pairs` does the work: one ESearch per pair, by the nodes' names, synonyms,
MeSH headings and NodeNorm's labels, then EFetch for the abstracts of its hits and of the PMIDs
the pair's edges already cite, which are labeled even when the search misses them. Everything it
fetches goes in the PubMed cache (`shared.pubmed.default_cache_path()`: KGB_PUBMED_CACHE, or
data/cache/pubmed.sqlite), so a rerun makes no requests and `pubmed.jsonl` is cheap to rewrite
in full. This module reads the pairs and entities step 1 wrote and writes what the search found
to `pubmed.jsonl`.

Without `NCBI_API_KEY` NCBI allows 3 requests a second, not 10, and a big job drags: the Nov 12
2026 run took 42 minutes for 2,957 pairs without a key and 75 seconds for 500 pairs with one,
because the shell that ran it lacked the key. So the step says at its start when the key is
missing, and a job of more than KEYLESS_SEARCHES uncached searches without one stops before
sending anything, unless `--without-key` says to go on.

What a paper's publication types make it is here too: its article type, whether it's retracted,
and the study design they name (`article_type`, `study_design`).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Literal, get_args

from pubmed_evidence import config
from pubmed_evidence.graphio import write_jsonl
from pubmed_evidence.records import ENTITIES, PAIRS, PUBMED, Entity, Pair, read_records
from shared.pubmed import (
    MAX_ABSTRACTS,
    EUtils,
    PairSearchResult,
    PubMedCache,
    build_query,
    entity_terms,
    load_nodenorm,
    search_pairs,
)
from shared.pubmed.cache import needs_search
from shared.pubmed.eutils import NCBI_API_KEY
from shared.schema import ArticleType, StudyDesign

# Without an API key, stop above this many uncached searches unless --without-key is given.
KEYLESS_SEARCHES = 500

# --- Publication types ------------------------------------------------------------------------

# PubMed's publication types for each article type, most specific first: an article with
# several types takes the first class any of them is in, and `other` if none is.
ARTICLE_TYPES: dict[str, frozenset[str]] = {
    "opinion": frozenset({"Editorial", "Comment", "Letter", "News"}),
    "review": frozenset({"Review", "Systematic Review", "Meta-Analysis", "Scoping Review"}),
    "guideline": frozenset({"Practice Guideline", "Guideline", "Consensus Development Conference"}),
    "case_report": frozenset({"Case Reports"}),
    "research": frozenset(
        {
            "Journal Article",
            "Clinical Trial",
            "Clinical Trial, Phase I",
            "Clinical Trial, Phase II",
            "Clinical Trial, Phase III",
            "Clinical Trial, Phase IV",
            "Randomized Controlled Trial",
            "Observational Study",
            "Comparative Study",
            "Evaluation Study",
            "Validation Study",
            "Multicenter Study",
        }
    ),
}
RETRACTED = "Retracted Publication"
STUDY_DESIGNS: tuple[str, ...] = get_args(StudyDesign)


def article_type(publication_types: Iterable[str]) -> tuple[ArticleType, bool]:
    """(article type, retracted) of an article with these PubMed publication types."""
    types = set(publication_types)
    kind = next((k for k, names in ARTICLE_TYPES.items() if types & names), "other")
    return kind, RETRACTED in types


def study_design(publication_types: Iterable[str]) -> StudyDesign | None:
    """The study design an article's PubMed publication types name (config.PUBMED_DESIGNS), or
    None if they name none. Where they name several, the one StudyDesign lists first wins: a
    meta-analysis over an RCT, a cohort over a case report."""
    named = {config.PUBMED_DESIGNS[t] for t in publication_types if t in config.PUBMED_DESIGNS}
    return min(named, key=STUDY_DESIGNS.index, default=None)


def design_of(
    publication_types: Iterable[str], extractor: StudyDesign
) -> tuple[StudyDesign, Literal["pubmed", "extractor"]]:
    """The design an abstract's evidence records (`PublicationEvidence.study_design`), and what
    set it: PubMed's publication types where they name one, else the extractor's answer."""
    named = study_design(publication_types)
    return (named, "pubmed") if named is not None else (extractor, "extractor")


# --- The step ---------------------------------------------------------------------------------


class WithoutKeyError(RuntimeError):
    """More than KEYLESS_SEARCHES uncached searches with no API key; the message is one line."""


def no_key_warning() -> str:
    return (
        f"{NCBI_API_KEY} is not set: NCBI allows 3 requests a second without a key and 10 with "
        "one, so a big search takes several times longer (about 4,000 pairs an hour, not "
        "25,000)"
    )


def uncached_searches(
    pairs: list[Pair],
    entities: Iterable[Entity],
    cache: PubMedCache,
    *,
    max_abstracts: int = MAX_ABSTRACTS,
    refresh: bool = False,
    use_nodenorm: bool = True,
    nodenorm: Callable[[list[str]], dict] | None = None,
) -> int:
    """How many of the pairs `search_pairs` would send an ESearch for: those with a usable
    query that the cache doesn't answer, with the same terms it would use. NodeNorm's entries
    go in the cache, so the search that follows asks it nothing more."""
    by_id = {e.id: e for e in entities}
    nodes = list(dict.fromkeys(n for p in pairs for n in p.nodes))
    entries = load_nodenorm(nodes, cache, nodenorm) if use_nodenorm else {}
    n = 0
    for pair in pairs:
        terms = [entity_terms(by_id.get(node) or Entity(id=node), entries) for node in pair.nodes]
        if not all(synonyms or mesh for synonyms, mesh in terms):
            continue
        (a_synonyms, a_mesh), (b_synonyms, b_mesh) = terms
        query = build_query(a_synonyms, a_mesh, b_synonyms, b_mesh)
        n += needs_search(cache.get_search(query), max_abstracts, refresh)
    return n


def search(
    work: Path,
    *,
    max_abstracts: int = MAX_ABSTRACTS,
    limit: int | None = None,
    refresh: bool = False,
    use_nodenorm: bool = True,
    without_key: bool = False,
    client: EUtils | None = None,
    nodenorm: Callable[[list[str]], dict] | None = None,
) -> PairSearchResult:
    """Search and fetch every pair in `work/pairs.jsonl` (the first `limit` of them) with
    `shared.pubmed.search_pairs`, write `work/pubmed.jsonl`, and return what it found. Pairs that
    fail are left out of the file and listed in the result's `failed_pairs`; a rerun retries
    them, with everything fetched still cached. The PubMed cache is the default one; without
    `client`, the default client is opened too.

    Without an API key (the client's, else NCBI_API_KEY), more than KEYLESS_SEARCHES uncached
    searches raise WithoutKeyError before anything is sent, unless `without_key`."""
    pairs = list(read_records(work / PAIRS, Pair))
    if limit is not None:
        pairs = pairs[:limit]
    entities = list(read_records(work / ENTITIES, Entity))
    has_key = client.api_key if client is not None else os.environ.get(NCBI_API_KEY)
    with PubMedCache() as cache:
        if not has_key and not without_key:
            n = uncached_searches(
                pairs,
                entities,
                cache,
                max_abstracts=max_abstracts,
                refresh=refresh,
                use_nodenorm=use_nodenorm,
                nodenorm=nodenorm,
            )
            if n > KEYLESS_SEARCHES:
                raise WithoutKeyError(
                    f"{n:,} pairs would be searched without {NCBI_API_KEY} set, more than "
                    f"{KEYLESS_SEARCHES:,}; set the key (see SKILL.md), or pass --without-key "
                    "to search at 3 requests a second anyway"
                )
        result = search_pairs(
            pairs,
            entities,
            cache=cache,
            client=client,
            max_abstracts=max_abstracts,
            refresh=refresh,
            use_nodenorm=use_nodenorm,
            nodenorm=nodenorm,
        )
    write_jsonl(work / PUBMED, result.records)
    return result


# --- CLI --------------------------------------------------------------------------------------


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--max-abstracts",
        type=int,
        default=MAX_ABSTRACTS,
        help="PMIDs per pair from the search, by relevance; source PMIDs come on top "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="search only the first N pairs, for a sample"
    )
    parser.add_argument(
        "--refresh", action="store_true", help="rerun searches that are already cached"
    )
    parser.add_argument(
        "--no-nodenorm",
        action="store_true",
        help="search by the nodes' own names only, without NodeNorm's synonyms and MeSH",
    )
    parser.add_argument(
        "--without-key",
        action="store_true",
        help=f"go ahead without {NCBI_API_KEY}, at 3 requests a second, even for more than "
        f"{KEYLESS_SEARCHES:,} uncached searches (the step stops otherwise)",
    )


def run(args: argparse.Namespace) -> int:
    work: Path = args.work
    for name in (PAIRS, ENTITIES):
        if not (work / name).exists():
            print(
                f"pubmed: {work / name} not found; run `kgb-augment select` first", file=sys.stderr
            )
            return 1
    if args.max_abstracts < 0 or (args.limit is not None and args.limit < 0):
        print("pubmed: --max-abstracts and --limit can't be negative", file=sys.stderr)
        return 1
    if not os.environ.get(NCBI_API_KEY):
        print(f"pubmed: {no_key_warning()}", file=sys.stderr)
    try:
        result = search(
            work,
            max_abstracts=args.max_abstracts,
            limit=args.limit,
            refresh=args.refresh,
            use_nodenorm=not args.no_nodenorm,
            without_key=args.without_key,
        )
    except WithoutKeyError as e:
        print(f"pubmed: error: {e}", file=sys.stderr)
        return 1
    s = result.summary()
    print(
        f"pubmed: {s['pairs']} pairs: {s['searched']} searched, {s['cached']} from cache, "
        f"{s['skipped']} skipped for lack of a usable name ({s['skipped_kept']} kept for their "
        f"source PMIDs), {s['failed']} failed"
    )
    print(
        f"pubmed: {s['requests']} requests; {s['fetched']} PMIDs fetched; "
        f"{s['abstracts']} abstracts and {s['no_abstract']} PMIDs without one in "
        f"{s['records']} records; NodeNorm knew {s['nodenorm_known']} nodes"
    )
    print(f"pubmed: wrote {work / PUBMED}")
    if s["failed"]:
        print(
            f"pubmed: {s['failed']} pairs failed and are not in {PUBMED}; rerun to retry; "
            "cached results are kept",
            file=sys.stderr,
        )
    # As in step 3: a few transient failures leave those pairs unlabeled until a rerun, but
    # many mean NCBI is unreachable, and later steps shouldn't run on that.
    return 1 if result.too_many_failed else 0
