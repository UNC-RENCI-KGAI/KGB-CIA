"""Search PubMed for node pairs, and fetch the abstracts.

One ESearch per pair finds the abstracts that name both nodes, by their names and synonyms in
title or abstract (`[tiab]`) or by MeSH heading (`[mh]`), most relevant first; EFetch then
fetches the abstracts of those PMIDs and of the PMIDs the pair's edges already cite, which are
kept even when the search misses them. NodeNorm supplies the synonyms and MeSH headings the
nodes lack, since a node usually carries one name and recall depends on the query's.

Everything fetched (searches by query string, abstracts by PMID, NodeNorm entries by CURIE) goes
in the PubMed cache, so a rerun makes no requests. Requests are sequential and spaced to stay
under NCBI's rate limit, and NCBI asks that large jobs run off-peak (`is_peak`).

`search_pairs` takes the pairs and their nodes and returns a `PubmedRecord` per pair with counts
of what it did; reading and writing files is the caller's.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from shared import nodenorm as nodenorm_api
from shared.pubmed._util import chunks, today, warn
from shared.pubmed.cache import PubMedCache, fetch_into, needs_search
from shared.pubmed.eutils import EUtils, EUtilsError
from shared.pubmed.records import Entity, FetchedPmid, Pair, PubmedRecord, Search

MAX_ABSTRACTS = 20  # PMIDs per pair from the search, by relevance; cited PMIDs come on top
MAX_SYNONYMS = 8  # [tiab] terms, and [mh] headings, per node
NODENORM_BATCH = 1000
MIN_TERM_CHARS = 3
MAX_TERM_CHARS = 100
OFF_PEAK_SEARCHES = 1000  # warn above this many uncached searches at peak hours
CHUNK = 500  # pairs searched, then fetched, at a time; progress is reported per chunk
MAX_REPORTED_FAILURES = 20
MAX_FAILED_SHARE = 0.05  # more failed pairs than this, and the run should count as failed

# Characters that end or break a quoted PubMed phrase: quotes, field tags, truncation.
_UNSAFE = re.compile(r'["\[\]*]')
# Babel labels UniProtKB entries "ABL1_HUMAN Tyrosine-protein kinase ABL1 (sprot)".
_UNIPROT_MNEMONIC = re.compile(r"^[A-Z0-9]+_[A-Z0-9]+\s+(?=\S)")
_UNIPROT_SUFFIX = re.compile(r"\s*\((?:sprot|trembl)\)$")
# NodeNorm labels that are never how a paper names the thing: cDNA clones, fragments.
_JUNK_LABEL = re.compile(r"\bcDNA\b|\bclone\b|\(Fragment\)|\bfis\b", re.I)
# Suffixes and prefixes on labels that papers leave off: "VWF gene", "SLC26A4 protein, human",
# "endoglin (human)", and MONDO's "obsolete ..." for a retired term.
_LABEL_AFFIX = re.compile(
    r"^obsolete\s+|\s+gene$|\s+protein,\s*human$|,\s*human$|\s*\(human\)$", re.I
)
# Xenopus laevis symbols name the subgenome copy ("rdh10.L", "bmp2.S"); papers write "rdh10".
_XENOPUS_HOMEOLOG = re.compile(r"^([a-z0-9][a-z0-9.\-]*)\.[LS]$")


# --- Terms and query ----------------------------------------------------------------------------


def clean_terms(terms: Iterable[str | None], limit: int = MAX_SYNONYMS) -> list[str]:
    """Usable `[tiab]`/`[mh]` phrases from `terms`, in their order, at most `limit` of them.

    Removes characters that break a quoted phrase and collapses whitespace; drops terms shorter
    than MIN_TERM_CHARS or longer than MAX_TERM_CHARS, terms without a letter (numbers, CAS
    numbers) and case-insensitive duplicates.
    """
    out: list[str] = []
    seen: set[str] = set()
    for term in terms:
        if not term:
            continue
        text = " ".join(_UNSAFE.sub(" ", term).split())
        key = text.casefold()
        if not MIN_TERM_CHARS <= len(text) <= MAX_TERM_CHARS or key in seen:
            continue
        if not any(ch.isalpha() for ch in text):
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def search_forms(term: str | None) -> list[str]:
    """The forms of a name worth searching: none for a junk label, the label without affixes
    papers leave off, and a Xenopus homeolog's base symbol before the symbol itself."""
    if not term or _JUNK_LABEL.search(term):
        return []
    text = term.strip()
    while (tidy := _LABEL_AFFIX.sub("", text).strip()) != text:
        text = tidy
    base = _XENOPUS_HOMEOLOG.match(text)
    return [base.group(1), text] if base else [text]


def _tidy_label(curie: str, label: str) -> str:
    label = label.strip()
    if curie.startswith("UniProtKB:"):
        label = _UNIPROT_SUFFIX.sub("", _UNIPROT_MNEMONIC.sub("", label))
    return label


def entity_terms(
    entity: Entity,
    nodenorm_entries: dict[str, dict | None],
    limit: int = MAX_SYNONYMS,
) -> tuple[list[str], list[str]]:
    """(synonyms, MeSH headings) to search for `entity`.

    Synonyms in priority order: the node's name, its own synonyms, then NodeNorm's preferred
    label and the labels of its equivalent identifiers (looked up by the node's id), each in its
    `search_forms`. MeSH headings are the labels of its `MESH:D...` identifiers: PubMed searches
    `[mh]` by name, not by MeSH UI, so an id without a label is skipped, as are supplementary
    concepts (`MESH:C...`), which `[mh]` doesn't index (their labels can still be synonyms).
    """
    entry = nodenorm_entries.get(entity.id) or {}
    labels: dict[str, str] = {}
    for ident in [entry.get("id") or {}, *(entry.get("equivalent_identifiers") or [])]:
        curie, label = ident.get("identifier"), ident.get("label")
        if curie and label and curie not in labels:
            labels[curie] = _tidy_label(curie, label)
    preferred = labels.get((entry.get("id") or {}).get("identifier", ""))
    candidates = [entity.name, *entity.synonyms, preferred, *labels.values()]
    synonyms = clean_terms([f for c in candidates for f in search_forms(c)], limit=limit)
    mesh_ids = dict.fromkeys(
        c for c in [entity.id, *entity.equivalent_identifiers, *labels] if c.startswith("MESH:D")
    )
    mesh = clean_terms([labels.get(c) for c in mesh_ids], limit=limit)
    return synonyms, mesh


def _group(synonyms: Sequence[str], mesh: Sequence[str]) -> str:
    terms = [f'"{t}"[tiab]' for t in synonyms] + [f'"{t}"[mh]' for t in mesh]
    if not terms:
        raise ValueError("an entity needs at least one synonym or MeSH heading")
    return "(" + " OR ".join(terms) + ")"


def build_query(
    a_synonyms: Sequence[str],
    a_mesh: Sequence[str],
    b_synonyms: Sequence[str],
    b_mesh: Sequence[str],
) -> str:
    """`(A terms) AND (B terms) AND hasabstract`, each group `"syn"[tiab] OR ... "mesh"[mh]`."""
    return f"{_group(a_synonyms, a_mesh)} AND {_group(b_synonyms, b_mesh)} AND hasabstract"


# --- NodeNorm -----------------------------------------------------------------------------------


def nodenorm_entries(
    curies: Sequence[str],
    cache: PubMedCache,
    nodenorm: Callable[[list[str]], dict] | None = None,
    batch: int | None = None,
) -> dict[str, dict | None]:
    """NodeNorm entries for `curies` (None for unknown ones), asking NodeNorm (`shared.nodenorm`
    unless given) only for those not in the cache, NODENORM_BATCH at a time. Raises what
    `nodenorm` raises; batches answered before that stay cached."""
    nodenorm = nodenorm or nodenorm_api.normalize
    entries = cache.get_nodenorm(curies)
    missing = [c for c in dict.fromkeys(curies) if c not in entries]
    for chunk in chunks(missing, batch or NODENORM_BATCH):
        response = nodenorm(list(chunk))
        got = {c: response.get(c) or None for c in chunk}
        cache.put_nodenorm(got)
        entries.update(got)
    return entries


def load_nodenorm(
    curies: Sequence[str],
    cache: PubMedCache,
    nodenorm: Callable[[list[str]], dict] | None = None,
    batch: int | None = None,
) -> dict[str, dict | None]:
    """`nodenorm_entries`, except that if NodeNorm fails it warns once and returns what it has:
    the search then uses the nodes' own names."""
    try:
        return nodenorm_entries(curies, cache, nodenorm, batch)
    except (httpx.HTTPError, ValueError, OSError) as e:
        entries = cache.get_nodenorm(curies)
        left = sum(1 for c in dict.fromkeys(curies) if c not in entries)
        warn(
            f"NodeNorm failed ({type(e).__name__}: {e}); searching {left} nodes by their own "
            "names only"
        )
        return entries


# --- The search ---------------------------------------------------------------------------------


def is_peak(when: datetime | None = None) -> bool:
    """True on weekdays from 5am to 9pm in America/New_York, when NCBI asks large jobs to wait.
    False if the time zone database is missing."""
    try:
        zone = ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError:
        return False
    local = (when or datetime.now(UTC)).astimezone(zone)
    return local.weekday() < 5 and 5 <= local.hour < 21


@dataclass
class PairSearchResult:
    """What `search_pairs` found, and counts of how. Pairs that failed have no record and are
    listed in `failed_pairs`; a rerun retries them, with everything fetched still cached."""

    records: list[PubmedRecord] = field(default_factory=list)
    pairs: int = 0
    searched: int = 0  # pairs searched by ESearch
    cached: int = 0  # pairs whose search came from the cache
    skipped: int = 0  # pairs not searched for lack of a usable name for a node
    skipped_kept: int = 0  # of those, the ones kept for their cited PMIDs
    failed_pairs: list[str] = field(default_factory=list)
    requests: int = 0  # E-utilities requests sent
    fetched: int = 0  # PMIDs fetched by EFetch
    nodenorm_known: int = 0  # nodes NodeNorm knows

    @property
    def failed(self) -> int:
        return len(self.failed_pairs)

    @property
    def too_many_failed(self) -> bool:
        """More than MAX_FAILED_SHARE of the pairs failed: NCBI is likely unreachable, and later
        steps shouldn't run on the result."""
        return self.failed > MAX_FAILED_SHARE * max(self.pairs, 1)

    def summary(self) -> dict[str, Any]:
        """The counts as a dict, with the PMIDs with and without an abstract across records."""
        return {
            "pairs": self.pairs,
            "records": len(self.records),
            "searched": self.searched,
            "cached": self.cached,
            "skipped": self.skipped,
            "skipped_kept": self.skipped_kept,
            "failed": self.failed,
            "failed_pairs": list(self.failed_pairs),
            "requests": self.requests,
            "fetched": self.fetched,
            "abstracts": len({f.pmid for r in self.records for f in r.pmids}),
            "no_abstract": len({p for r in self.records for p in r.no_abstract}),
            "nodenorm_known": self.nodenorm_known,
        }

    def _skip(self, plan: _Plan) -> None:
        if self.skipped < MAX_REPORTED_FAILURES:
            nameless = ", ".join(n for n in plan.pair.nodes if not plan.synonyms[n] + plan.mesh[n])
            kept = "keeping its source PMIDs" if _source_pmids(plan.pair) else "no source PMIDs"
            warn(f"pair {plan.pair.pair_id} not searched: no usable name for {nameless}; {kept}")
        elif self.skipped == MAX_REPORTED_FAILURES:
            warn("more pairs not searched; not listing each")
        self.skipped += 1

    def _fail(self, pair_id: str, reason: str) -> None:
        if len(self.failed_pairs) < MAX_REPORTED_FAILURES:
            warn(f"pair {pair_id} failed: {reason}")
        elif len(self.failed_pairs) == MAX_REPORTED_FAILURES:
            warn("more pairs failed; not listing each")
        self.failed_pairs.append(pair_id)


@dataclass
class _Plan:
    pair: Pair
    synonyms: dict[str, list[str]]
    mesh: dict[str, list[str]]
    query: str | None  # None when a node has no usable term


def _plan(pair: Pair, entities: dict[str, Entity], entries: dict[str, dict | None]) -> _Plan:
    synonyms: dict[str, list[str]] = {}
    mesh: dict[str, list[str]] = {}
    for node in pair.nodes:
        entity = entities.get(node) or Entity(id=node)
        synonyms[node], mesh[node] = entity_terms(entity, entries)
    a, b = pair.nodes
    searchable = all(synonyms[n] or mesh[n] for n in pair.nodes)
    query = build_query(synonyms[a], mesh[a], synonyms[b], mesh[b]) if searchable else None
    return _Plan(pair, synonyms, mesh, query)


def _source_pmids(pair: Pair) -> list[str]:
    return [p for p in dict.fromkeys(pair.source_pmids) if p.isdigit()]


def _record(
    plan: _Plan, search: Search | None, cache: PubMedCache, max_abstracts: int
) -> PubmedRecord | None:
    """The pair's record, or None if some PMID's abstract isn't in the cache (fetch failed)."""
    hits = list(dict.fromkeys(search.pmids[:max_abstracts])) if search else []
    rank = {p: i for i, p in enumerate(hits, 1)}
    source = _source_pmids(plan.pair)
    order = list(dict.fromkeys([*hits, *source]))
    abstracts = cache.get_abstracts(order)
    if any(p not in abstracts for p in order):
        return None
    source_set = set(source)
    fetched: list[FetchedPmid] = []
    no_abstract: list[str] = []
    for p in order:
        if not abstracts[p].has_text:
            no_abstract.append(p)
        elif p in rank:
            origin = "source+search" if p in source_set else "search"
            fetched.append(FetchedPmid(pmid=p, origin=origin, rank=rank[p]))
        else:
            fetched.append(FetchedPmid(pmid=p, origin="source"))
    return PubmedRecord(
        pair_id=plan.pair.pair_id,
        query=plan.query or "",
        retrieved=search.retrieved if search else today(),
        total=search.total if search else 0,
        synonyms=plan.synonyms,
        mesh=plan.mesh,
        pmids=fetched,
        no_abstract=no_abstract,
    )


def _run_chunk(
    plans: Sequence[_Plan],
    cache: PubMedCache,
    client: EUtils,
    max_abstracts: int,
    refresh: bool,
    result: PairSearchResult,
) -> None:
    """Search the chunk's pairs, fetch the abstracts none of them has cached, then build their
    records. Fetching per chunk rather than per pair fills EFetch batches and fetches an
    abstract shared by several pairs once; a batch that fails fails only its own pairs."""
    searches: dict[str, Search | None] = {}
    for plan in plans:
        pid = plan.pair.pair_id
        if plan.query is None:
            result._skip(plan)
            if not _source_pmids(plan.pair):
                continue
            result.skipped_kept += 1
            searches[pid] = None
            continue
        cached = cache.get_search(plan.query)
        if not needs_search(cached, max_abstracts, refresh):
            result.cached += 1
            searches[pid] = cached
            continue
        try:
            total, pmids = client.search(plan.query, max_abstracts)
        except EUtilsError as e:
            result._fail(pid, str(e))
            continue
        search = Search(plan.query, today(), total, pmids)
        cache.put_search(search)
        result.searched += 1
        searches[pid] = search

    wanted: list[str] = []
    for plan in plans:
        if plan.pair.pair_id in searches:
            search = searches[plan.pair.pair_id]
            wanted += search.pmids[:max_abstracts] if search else []
            wanted += _source_pmids(plan.pair)
    result.fetched += fetch_into(cache, client, cache.missing_abstracts(wanted))

    for plan in plans:
        pid = plan.pair.pair_id
        if pid not in searches:
            continue
        record = _record(plan, searches[pid], cache, max_abstracts)
        if record is None:
            result._fail(pid, "some abstracts could not be fetched")
        else:
            result.records.append(record)


def search_pairs(
    pairs: Sequence[Pair],
    entities: Iterable[Entity],
    *,
    cache: PubMedCache | None = None,
    client: EUtils | None = None,
    max_abstracts: int = MAX_ABSTRACTS,
    refresh: bool = False,
    use_nodenorm: bool = True,
    nodenorm: Callable[[list[str]], dict] | None = None,
) -> PairSearchResult:
    """Search PubMed for each pair and fetch its abstracts: up to `max_abstracts` hits by
    relevance, and the pair's `source_pmids` on top. A pair whose node has no usable name isn't
    searched, and keeps a record (with query '') only if it has source PMIDs.

    `entities` are the pairs' nodes (a node without one is searched by NodeNorm's labels
    alone). `refresh` reruns cached searches but still takes abstracts from the cache;
    `use_nodenorm=False` searches by the nodes' own names only; `nodenorm` replaces
    `shared.nodenorm.normalize`. Without `cache` or `client`, opens the default ones and
    closes them after."""
    by_id = {e.id: e for e in entities}
    result = PairSearchResult(pairs=len(pairs))
    nodes = list(dict.fromkeys(n for p in pairs for n in p.nodes))
    with ExitStack() as stack:
        if cache is None:
            cache = stack.enter_context(PubMedCache())
        if client is None:
            client = stack.enter_context(EUtils())
        start = client.requests
        entries = load_nodenorm(nodes, cache, nodenorm) if use_nodenorm else {}
        plans = [_plan(p, by_id, entries) for p in pairs]
        uncached = sum(
            1
            for plan in plans
            if plan.query and needs_search(cache.get_search(plan.query), max_abstracts, refresh)
        )
        if uncached > OFF_PEAK_SEARCHES and is_peak():
            warn(
                f"{uncached} uncached searches: NCBI asks that large jobs run on weekends or "
                "9pm-5am ET on weekdays"
            )
        done = 0
        for chunk in chunks(plans, CHUNK):
            _run_chunk(chunk, cache, client, max_abstracts, refresh, result)
            done += len(chunk)
            if len(plans) > CHUNK:
                warn(
                    f"{done}/{len(plans)} pairs ({result.searched} searched, "
                    f"{result.cached} from cache, {result.failed} failed, "
                    f"{client.requests - start} requests)"
                )
        result.requests = client.requests - start
    result.nodenorm_known = sum(1 for n in nodes if entries.get(n))
    return result
