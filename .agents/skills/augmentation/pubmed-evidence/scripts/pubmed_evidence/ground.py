"""Step 4: ground the anatomy the claims name to UBERON terms, and pin UBERON's release.

A claim gives its anatomical context as the abstract names it ("liver", "kidneys"). This step
looks each name up in the RENCI Name Resolver among anatomical entities, then normalizes the
matches with NodeNorm. The name is grounded to the first match whose label or one of whose
synonyms *is* the name, case and whitespace aside, and whose NodeNorm clique has an UBERON term.
So a near match never grounds a name, and neither does a cell type (CL) or a cell line. A name
that isn't grounded stays on its claim as text, for `lit_context_notes`; a grounded one goes on
the claim's statements as `anatomical_context_qualifier`, which can make them narrower than an
edge (`refine.statements_of`).

Groundings are cached by name in the PubMed cache, misses too, so a rerun asks nothing. Names
are cached in their `anatomy_key` form, so "Liver" and "liver " share a row; `groundings` reads
them back for the names as claims give them. A lookup that fails isn't cached, and a rerun
retries it.

The step also makes sure the cache directory has the UBERON release config.UBERON_URL pins,
whose is_a/part_of closure step 6 uses (`uberon.py`), and records its `data-version` in
`label_run.json`. No model calls.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

import httpx

from pubmed_evidence import config, records, uberon
from pubmed_evidence.cache import Grounding, LabelCache, PubMedCache
from pubmed_evidence.graphio import chunks
from pubmed_evidence.pubmed import MAX_FAILED_SHARE, NODENORM_BATCH
from pubmed_evidence.records import PUBMED, PubmedRecord, read_records
from shared import nodenorm as nodenorm_api

# What grounds a name, or fails to (`Grounding.source`).
SOURCE = "nameres+nodenorm"
NAMERES_BATCH = 100  # names per bulk lookup
# Matches per name. The UBERON term can rank below other vocabularies' ("lymph nodes" finds
# four UMLS sets of lymph nodes before UBERON's lymph node).
NAMERES_HITS = 20
ANATOMY_TYPES = ["biolink:AnatomicalEntity"]
UBERON = "UBERON:"
TIMEOUT = 60.0

Lookup = Callable[[list[str]], dict[str, list[dict]]]


def _warn(message: str) -> None:
    print(f"ground: {message}", file=sys.stderr)


def anatomy_key(name: str) -> str:
    """The form a name is cached and compared in: casefolded, with its whitespace collapsed."""
    return " ".join(name.split()).casefold()


def claim_anatomy(claim: dict[str, Any]) -> str | None:
    """The anatomy a stored claim (`abstract_claims`) names, as the abstract writes it."""
    name = (claim.get("context") or {}).get("anatomy")
    return name if isinstance(name, str) and name.strip() else None


def name_lookup(
    names: Sequence[str], *, client: httpx.Client | None = None, url: str = config.NAMERES_URL
) -> dict[str, list[dict]]:
    """The Name Resolver's matches for each name among anatomical entities, best first:
    {name: [{"curie", "label", "synonyms", ...}]}. Raises httpx.HTTPError or ValueError."""
    if not names:
        return {}
    http = client if client is not None else httpx.Client(timeout=TIMEOUT)
    try:
        response = http.post(
            f"{url.rstrip('/')}/bulk-lookup",
            json={
                "strings": list(names),
                "autocomplete": False,
                "limit": NAMERES_HITS,
                "biolink_types": ANATOMY_TYPES,
            },
        )
        response.raise_for_status()
        data = response.json()
    finally:
        if client is None:
            http.close()
    if not isinstance(data, dict):
        raise ValueError(f"unexpected Name Resolver response: {str(data)[:200]}")
    return data


def _names(key: str, match: dict) -> bool:
    """Whether a Name Resolver match's label or a synonym is the name `key`."""
    names = [match.get("label"), *(match.get("synonyms") or [])]
    return any(isinstance(n, str) and anatomy_key(n) == key for n in names)


def _uberon_term(entry: dict | None) -> tuple[str, str | None] | None:
    """(CURIE, label) of the UBERON term in a NodeNorm clique: its preferred id if that is one,
    else the first equivalent that is."""
    entry = entry or {}
    for ident in [entry.get("id") or {}, *(entry.get("equivalent_identifiers") or [])]:
        curie = ident.get("identifier") or ""
        if curie.startswith(UBERON):
            return curie, ident.get("label")
    return None


def _normalize(
    curies: list[str], cache: PubMedCache, nodenorm: Callable[[list[str]], dict]
) -> dict[str, dict | None]:
    """NodeNorm's entries for `curies` (None for unknown ones), through the PubMed cache's
    `nodenorm` table as step 2 uses it. Raises what `nodenorm` raises; batches it answered
    before that stay cached."""
    entries = cache.get_nodenorm(curies)
    for chunk in chunks([c for c in dict.fromkeys(curies) if c not in entries], NODENORM_BATCH):
        response = nodenorm(list(chunk))
        got = {c: response.get(c) or None for c in chunk}
        cache.put_nodenorm(got)
        entries.update(got)
    return entries


def ground_anatomy(
    names: Iterable[str],
    cache: PubMedCache,
    *,
    refresh: bool = False,
    lookup: Lookup | None = None,
    nodenorm: Callable[[list[str]], dict] | None = None,
) -> dict[str, Any]:
    """Ground each name the cache doesn't have yet (every name, with `refresh`), cache the
    answers, and return counts: distinct names, from the cache, looked up, grounded and not
    (of all of them, cached or new), and the names whose lookup failed, which aren't cached."""
    lookup = lookup or name_lookup
    nodenorm = nodenorm or nodenorm_api.normalize
    keys = list(dict.fromkeys(k for k in map(anatomy_key, names) if k))
    cached = {} if refresh else cache.get_groundings("anatomy", keys)
    todo = [k for k in keys if k not in cached]
    failed: list[str] = []
    for chunk in chunks(todo, NAMERES_BATCH):
        try:
            matches = lookup(list(chunk))
            candidates = {
                k: [m["curie"] for m in matches.get(k) or [] if m.get("curie") and _names(k, m)]
                for k in chunk
            }
            entries = _normalize([c for cs in candidates.values() for c in cs], cache, nodenorm)
        except (httpx.HTTPError, ValueError, OSError) as e:
            _warn(f"lookup of {len(chunk)} names failed ({type(e).__name__}: {e})")
            failed += chunk
            continue
        rows = []
        for k in chunk:
            terms = (_uberon_term(entries.get(c)) for c in candidates[k])
            curie, label = next((t for t in terms if t), (None, None))
            rows.append(Grounding(k, "anatomy", curie, label, SOURCE))
        cache.put_groundings(rows)
    got = cache.get_groundings("anatomy", keys)
    return {
        "names": len(keys),
        "cached": len(cached),
        "looked_up": len(todo) - len(failed),
        "grounded": sum(1 for g in got.values() if g.curie),
        "ungrounded": sum(1 for g in got.values() if not g.curie),
        "failed": len(failed),
        "failed_names": failed,
        "terms": sorted({g.curie for g in got.values() if g.curie}),
    }


def groundings(cache: PubMedCache, names: Iterable[str]) -> dict[str, Grounding]:
    """The cached grounding of each name, keyed by the name as given (a claim's spelling);
    names never grounded or looked up are left out."""
    keys = {n: anatomy_key(n) for n in names}
    got = cache.get_groundings("anatomy", set(keys.values()))
    return {n: got[k] for n, k in keys.items() if k in got}


def ground(
    work: Path,
    cache_dir: Path,
    run: records.LabelRun,
    *,
    refresh: bool = False,
    lookup: Lookup | None = None,
    nodenorm: Callable[[list[str]], dict] | None = None,
) -> dict[str, Any]:
    """Ground the anatomy names in the claims of `run` for the pairs in `work/pubmed.jsonl`,
    and return `ground_anatomy`'s counts with the number of claims read."""
    pair_ids = [r.pair_id for r in read_records(work / PUBMED, PubmedRecord)]
    with LabelCache(cache_dir / config.LABEL_CACHE) as labels:
        answers = labels.abstract_claims(run.prompt_version, run.model, pair_ids)
    claims = [c for a in answers for c in a.claims]
    with PubMedCache(cache_dir / config.PUBMED_CACHE) as cache:
        names = filter(None, map(claim_anatomy, claims))
        summary = ground_anatomy(names, cache, refresh=refresh, lookup=lookup, nodenorm=nodenorm)
    return {"claims": len(claims), **summary}


# --- CLI --------------------------------------------------------------------------------------


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="look up names already in the cache again, the ones that weren't grounded too",
    )


def run(args: argparse.Namespace) -> int:
    work: Path = args.work
    if not (work / PUBMED).exists():
        print(f"ground: {work / PUBMED} not found; run `kgb-augment pubmed` first", file=sys.stderr)
        return 1
    label_run = records.read_label_run(work)
    if label_run is None:
        print(
            f"ground: no {work / records.LABEL_RUN}; run `kgb-augment label` first", file=sys.stderr
        )
        return 1
    s = ground(work, args.cache_dir, label_run, refresh=args.refresh)
    print(
        f"ground: {s['names']} anatomy names in {s['claims']} claims of run {label_run.model} "
        f"{label_run.prompt_version}: {s['grounded']} grounded to UBERON, {s['ungrounded']} not "
        f"(they stay notes); {s['cached']} from cache, {s['looked_up']} looked up, "
        f"{s['failed']} failed"
    )
    path = args.cache_dir / config.UBERON_FILE
    try:
        release = uberon.ensure(path)
    except (httpx.HTTPError, ValueError, OSError) as e:
        print(
            f"ground: couldn't fetch UBERON from {config.UBERON_URL} ({type(e).__name__}: {e}); "
            "rerun to retry",
            file=sys.stderr,
        )
        return 1
    records.write_label_run(work, label_run.model_copy(update={"uberon_version": release.version}))
    print(f"ground: UBERON {release.version}, {len(release.ancestors)} terms, from {path}")
    unknown = [t for t in s["terms"] if t not in release.ancestors]
    if unknown:
        _warn(
            f"{len(unknown)} grounded terms aren't live in this UBERON release (e.g. "
            f"{unknown[0]}); each is at or below only itself"
        )
    if s["failed"]:
        _warn(f"{s['failed']} names failed and aren't cached; rerun to retry")
    # As in step 2: a few failures leave those contexts as notes until a rerun, but many mean
    # the Name Resolver or NodeNorm is down, and the graph shouldn't be built on that.
    return 1 if s["failed"] > MAX_FAILED_SHARE * max(s["names"], 1) else 0
