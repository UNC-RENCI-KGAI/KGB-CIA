"""Step 2: search PubMed for each pair and fetch the abstracts, with `shared.pubmed`.

`shared.pubmed.search_pairs` does the work: one ESearch per pair, by the nodes' names, synonyms,
MeSH headings and NodeNorm's labels, then EFetch for the abstracts of its hits and of the PMIDs
the pair's edges already cite, which are labeled even when the search misses them. Everything it
fetches goes in the PubMed cache (`shared.pubmed.default_cache_path()`: KGB_PUBMED_CACHE, or
data/cache/pubmed/pubmed.sqlite), so a rerun makes no requests and `pubmed.jsonl` is cheap to
rewrite in full. This module reads the pairs and entities step 1 wrote and writes what the search
found to `pubmed.jsonl`.

What a paper's publication types make it is here too: its article type, whether it's retracted,
and the study design they name (`article_type`, `study_design`).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Literal, get_args

from pubmed_evidence import config
from pubmed_evidence.graphio import write_jsonl
from pubmed_evidence.records import ENTITIES, PAIRS, PUBMED, Entity, Pair, read_records
from shared.pubmed import MAX_ABSTRACTS, EUtils, PairSearchResult, search_pairs
from shared.schema import ArticleType, StudyDesign

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


def search(
    work: Path,
    *,
    max_abstracts: int = MAX_ABSTRACTS,
    limit: int | None = None,
    refresh: bool = False,
    use_nodenorm: bool = True,
    client: EUtils | None = None,
    nodenorm: Callable[[list[str]], dict] | None = None,
) -> PairSearchResult:
    """Search and fetch every pair in `work/pairs.jsonl` (the first `limit` of them) with
    `shared.pubmed.search_pairs`, write `work/pubmed.jsonl`, and return what it found. Pairs that
    fail are left out of the file and listed in the result's `failed_pairs`; a rerun retries
    them, with everything fetched still cached. The PubMed cache is the default one; without
    `client`, the default client is opened too."""
    pairs = list(read_records(work / PAIRS, Pair))
    if limit is not None:
        pairs = pairs[:limit]
    result = search_pairs(
        pairs,
        read_records(work / ENTITIES, Entity),
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
    result = search(
        work,
        max_abstracts=args.max_abstracts,
        limit=args.limit,
        refresh=args.refresh,
        use_nodenorm=not args.no_nodenorm,
    )
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
