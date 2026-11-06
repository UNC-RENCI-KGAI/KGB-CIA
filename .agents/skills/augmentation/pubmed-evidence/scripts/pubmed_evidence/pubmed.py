"""Step 2: search PubMed for each pair and fetch the abstracts.

One ESearch per pair finds the abstracts that name both nodes, by their names and synonyms in
title or abstract (`[tiab]`) or by MeSH heading (`[mh]`), most relevant first; EFetch then
fetches the abstracts of those PMIDs and of the PMIDs the pair's edges already cite, which are
labeled even when the search misses them. NodeNorm supplies the synonyms and MeSH headings the
graph's nodes lack, since a node usually carries one name and step 3's recall depends on the
query's.

Everything fetched (searches by query string, abstracts by PMID, NodeNorm entries by CURIE) goes
in the PubMed cache, so a rerun makes no requests and `pubmed.jsonl` is cheap to rewrite in full.
NCBI allows 3 requests a second without an API key and 10 with one, and asks that large jobs
run off-peak; requests here are sequential and spaced to stay under the limit.

What a paper's publication types make it is here too: its article type, whether it's retracted,
and the study design they name (`article_type`, `study_design`).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from pubmed_evidence import config
from pubmed_evidence.cache import Abstract, PubMedCache, Search, today
from pubmed_evidence.graphio import chunks, write_jsonl
from pubmed_evidence.records import (
    ENTITIES,
    PAIRS,
    PUBMED,
    Entity,
    FetchedPmid,
    Pair,
    PubmedRecord,
    read_records,
)
from shared import nodenorm as nodenorm_api
from shared.schema import ArticleType, StudyDesign

ESEARCH_MAX = 10_000  # ESearch returns at most this many ids per query
EFETCH_BATCH = 200
NODENORM_BATCH = 1000
MIN_TERM_CHARS = 3
MAX_TERM_CHARS = 100
OFF_PEAK_SEARCHES = 1000  # warn above this many uncached searches at peak hours
CHUNK = 500  # pairs searched, then fetched, at a time; progress is reported per chunk
GIVE_UP_AFTER = 10  # consecutive failed calls (after retries) before NCBI counts as down
MAX_REPORTED_FAILURES = 20
MAX_FAILED_SHARE = 0.05  # more failed pairs than this makes the step fail

# Characters that end or break a quoted PubMed phrase: quotes, field tags, truncation.
_UNSAFE = re.compile(r'["\[\]*]')
# Babel labels UniProtKB entries "ABL1_HUMAN Tyrosine-protein kinase ABL1 (sprot)".
_UNIPROT_MNEMONIC = re.compile(r"^[A-Z0-9]+_[A-Z0-9]+\s+(?=\S)")
_UNIPROT_SUFFIX = re.compile(r"\s*\((?:sprot|trembl)\)$")
_YEAR = re.compile(r"\d{4}")
# NodeNorm labels that are never how a paper names the thing: cDNA clones, fragments.
_JUNK_LABEL = re.compile(r"\bcDNA\b|\bclone\b|\(Fragment\)|\bfis\b", re.I)
# Suffixes and prefixes on labels that papers leave off: "VWF gene", "SLC26A4 protein, human",
# "endoglin (human)", and MONDO's "obsolete ..." for a retired term.
_LABEL_AFFIX = re.compile(
    r"^obsolete\s+|\s+gene$|\s+protein,\s*human$|,\s*human$|\s*\(human\)$", re.I
)
# Xenopus laevis symbols name the subgenome copy ("rdh10.L", "bmp2.S"); papers write "rdh10".
_XENOPUS_HOMEOLOG = re.compile(r"^([a-z0-9][a-z0-9.\-]*)\.[LS]$")


def _warn(message: str) -> None:
    print(f"pubmed: {message}", file=sys.stderr)


# --- Terms and query --------------------------------------------------------------------------


def clean_terms(terms: Iterable[str | None], limit: int = config.MAX_SYNONYMS) -> list[str]:
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
    limit: int = config.MAX_SYNONYMS,
) -> tuple[list[str], list[str]]:
    """(synonyms, MeSH headings) to search for `entity`.

    Synonyms in priority order: the node's name, its own synonyms, then NodeNorm's preferred
    label and the labels of its equivalent identifiers (looked up by the node's id), each in
    its `search_forms`. MeSH
    headings are the labels of its `MESH:D...` identifiers: PubMed searches `[mh]` by name, not
    by MeSH UI, so an id without a label is skipped, as are supplementary concepts (`MESH:C...`),
    which `[mh]` doesn't index (their labels can still be synonyms).
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


# --- E-utilities ------------------------------------------------------------------------------


class EUtilsError(RuntimeError):
    """A request that failed after retries, or was refused."""


class _Transient(Exception):
    """A response worth retrying: NCBI's rate-limit JSON, a backend error, a cut-off body."""


class RateLimiter:
    """At most `rate` calls a second, spaced evenly and shared by threads; `rate <= 0` disables
    it. Each caller reserves the next slot under the lock and sleeps outside it."""

    def __init__(
        self,
        rate: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.interval = 1.0 / rate if rate > 0 else 0.0
        self.clock = clock
        self.sleep = sleep
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if not self.interval:
            return
        with self._lock:
            now = self.clock()
            delay = self._next - now
            self._next = max(now, self._next) + self.interval
        if delay > 0:
            self.sleep(delay)


def parse_esearch(data: Any) -> tuple[int, list[str]]:
    """(total hits, PMIDs) from ESearch's JSON. Raises `_Transient` for NCBI's error responses
    (`{"error": "API rate limit exceeded"}`, `{"esearchresult": {"ERROR": ...}}`)."""
    if not isinstance(data, dict) or "error" in data:
        raise _Transient(str(data.get("error") if isinstance(data, dict) else data)[:200])
    result = data.get("esearchresult")
    if not isinstance(result, dict):
        raise _Transient("no esearchresult in response")
    if "ERROR" in result:
        raise _Transient(str(result["ERROR"])[:200])
    return int(result.get("count", 0)), [str(p) for p in result.get("idlist", [])]


def _text(element: ET.Element | None) -> str:
    """All text under `element` (titles and abstracts hold <i>, <sup> and the like), on one
    line."""
    return " ".join("".join(element.itertext()).split()) if element is not None else ""


def _year(pub_date: ET.Element | None) -> int | None:
    if pub_date is None:
        return None
    match = _YEAR.search(pub_date.findtext("Year") or pub_date.findtext("MedlineDate") or "")
    return int(match.group()) if match else None


def _abstract_text(abstract: ET.Element | None) -> str:
    """Paragraphs of the abstract, one per line; a structured abstract's as 'LABEL: text'."""
    if abstract is None:
        return ""
    paragraphs = []
    for part in abstract.findall("AbstractText"):
        text = _text(part)
        if text:
            label = (part.get("Label") or "").strip()
            paragraphs.append(f"{label}: {text}" if label else text)
    return "\n".join(paragraphs)


def _types(elements: list[ET.Element]) -> list[str]:
    """PubMed's publication types, in its order, without repeats."""
    return list(dict.fromkeys(t for t in map(_text, elements) if t))


def parse_efetch(xml_text: str) -> list[Abstract]:
    """Abstracts in an EFetch `PubmedArticleSet` (journal and book articles), with their
    publication types. An article without abstract text comes back with `abstract=''`. Raises
    ET.ParseError on malformed XML and ValueError on any other document, such as NCBI's
    `<eFetchResult><ERROR>`."""
    root = ET.fromstring(xml_text)
    if root.tag != "PubmedArticleSet":
        raise ValueError(f"unexpected EFetch response <{root.tag}>: {_text(root)[:200]}")
    out = []
    for article in root.findall("PubmedArticle"):
        citation = article.find("MedlineCitation")
        pmid = (citation.findtext("PMID") or "").strip() if citation is not None else ""
        if not pmid:
            continue
        art = citation.find("Article")
        if art is None:
            out.append(Abstract(pmid, "", publication_types=[]))
            continue
        year = _year(art.find("Journal/JournalIssue/PubDate")) or _year(art.find("ArticleDate"))
        title = _text(art.find("ArticleTitle"))
        types = _types(art.findall("PublicationTypeList/PublicationType"))
        out.append(Abstract(pmid, title, _abstract_text(art.find("Abstract")), year, "", types))
    for book in root.findall("PubmedBookArticle/BookDocument"):
        pmid = (book.findtext("PMID") or "").strip()
        if not pmid:
            continue
        title = _text(book.find("ArticleTitle")) or _text(book.find("Book/BookTitle"))
        year = _year(book.find("Book/PubDate"))
        types = _types(book.findall("PublicationType"))
        out.append(Abstract(pmid, title, _abstract_text(book.find("Abstract")), year, "", types))
    return out


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as e:
        raise _Transient(f"response is not JSON: {response.text[:200]!r}") from e


def _efetch_abstracts(response: httpx.Response) -> list[Abstract]:
    text = response.text
    if text.lstrip().startswith("{"):  # NCBI's JSON error, e.g. the rate limit
        raise _Transient(text[:200])
    try:
        return parse_efetch(text)
    except (ET.ParseError, ValueError) as e:
        raise _Transient(str(e)[:200]) from e


class EUtils:
    """NCBI E-utilities client for ESearch and EFetch, by POST (long queries exceed URL limits).

    Sends `tool`, and `email` and `api_key` from NCBI_EMAIL and NCBI_API_KEY when set. Retries
    timeouts, connection errors, 429, 5xx and NCBI's error-in-a-200 responses with exponential
    backoff. After GIVE_UP_AFTER calls in a row fail, it stops sending requests and fails every
    later call at once, so a run against an unreachable NCBI ends instead of retrying each pair.
    `rate` (requests a second) defaults to 10 with an API key and 3 without; 0 disables the
    limiter. Tests pass a client with `httpx.MockTransport`, `rate=0` and a no-op `sleep`.
    """

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        base_url: str = config.EUTILS_URL,
        api_key: str | None = None,
        email: str | None = None,
        rate: float | None = None,
        tries: int = 4,
        backoff: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 60.0,
    ):
        self.api_key = api_key or os.environ.get(config.NCBI_API_KEY) or None
        self.email = email or os.environ.get(config.NCBI_EMAIL) or None
        self.base_url = base_url.rstrip("/")
        self._own_client = client is None
        self.client = client if client is not None else httpx.Client(timeout=timeout)
        if rate is None:
            rate = 10.0 if self.api_key else 3.0
        self.limiter = RateLimiter(rate, sleep=sleep)
        self.tries = max(1, tries)
        self.backoff = backoff
        self.sleep = sleep
        self.requests = 0
        self.consecutive_failures = 0
        self._warned_email = False

    def close(self) -> None:
        if self._own_client:
            self.client.close()

    def __enter__(self) -> EUtils:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _post(self, endpoint: str, params: dict[str, str], parse: Callable[[httpx.Response], Any]):
        if self.consecutive_failures >= GIVE_UP_AFTER:
            raise EUtilsError(f"not sent: the last {GIVE_UP_AFTER} E-utilities calls failed")
        if not self.email and not self._warned_email:
            self._warned_email = True
            _warn(f"{config.NCBI_EMAIL} is not set; NCBI asks every client to send an email")
        data = {**params, "tool": config.NCBI_TOOL}
        if self.email:
            data["email"] = self.email
        if self.api_key:
            data["api_key"] = self.api_key
        url = f"{self.base_url}/{endpoint}"
        last = ""
        delay = 0.0
        for attempt in range(self.tries):
            if attempt:
                self.sleep(max(delay, self.backoff * 2 ** (attempt - 1)))
            delay = 0.0
            self.limiter.wait()
            self.requests += 1
            try:
                response = self.client.post(url, data=data)
            except httpx.TransportError as e:  # timeouts, refused and reset connections
                last = f"{type(e).__name__}: {e}"
                continue
            if response.status_code == 429 or response.status_code >= 500:
                last = f"HTTP {response.status_code}"
                retry_after = response.headers.get("Retry-After", "")
                delay = float(retry_after) if retry_after.isdigit() else 0.0
                continue
            if response.status_code >= 400:
                self.consecutive_failures += 1
                raise EUtilsError(f"{endpoint}: HTTP {response.status_code}: {response.text[:200]}")
            try:
                result = parse(response)
            except _Transient as e:
                last = str(e)
                continue
            self.consecutive_failures = 0
            return result
        self.consecutive_failures += 1
        raise EUtilsError(f"{endpoint} failed after {self.tries} tries: {last}")

    def esearch(self, query: str, retmax: int = config.MAX_ABSTRACTS) -> tuple[int, list[str]]:
        """(total hits, up to `retmax` PMIDs by relevance) for a PubMed query."""
        params = {
            "db": "pubmed",
            "term": query,
            "sort": "relevance",
            "retmax": str(max(0, min(retmax, ESEARCH_MAX))),
            "retmode": "json",
        }
        return self._post("esearch.fcgi", params, lambda r: parse_esearch(_json(r)))

    def efetch(self, pmids: Iterable[str]) -> list[Abstract]:
        """An `Abstract` for every PMID, in one request: callers send at most EFETCH_BATCH. A
        PMID PubMed doesn't return comes back with an empty title and abstract and no
        publication types, so the cache stops asking for it."""
        wanted = list(dict.fromkeys(pmids))
        if not wanted:
            return []
        if len(wanted) > EFETCH_BATCH:
            raise ValueError(f"{len(wanted)} PMIDs in one EFetch request; at most {EFETCH_BATCH}")
        params = {"db": "pubmed", "id": ",".join(wanted), "rettype": "abstract", "retmode": "xml"}
        got = {a.pmid: a for a in self._post("efetch.fcgi", params, _efetch_abstracts)}
        return [got.get(p) or Abstract(p, "", publication_types=[]) for p in wanted]


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


def is_peak(when: datetime | None = None) -> bool:
    """True on weekdays from 5am to 9pm in America/New_York, when NCBI asks large jobs to wait.
    False if the time zone database is missing."""
    try:
        zone = ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError:
        return False
    local = (when or datetime.now(UTC)).astimezone(zone)
    return local.weekday() < 5 and 5 <= local.hour < 21


def load_nodenorm(
    curies: Sequence[str],
    cache: PubMedCache,
    nodenorm: Callable[[list[str]], dict] | None = None,
    batch: int | None = None,
) -> dict[str, dict | None]:
    """NodeNorm entries for `curies` (None for unknown ones), asking NodeNorm only for those not
    in the cache. If NodeNorm fails, warns once and returns what it has: the search then uses
    the nodes' own names."""
    nodenorm = nodenorm or nodenorm_api.normalize
    entries = cache.get_nodenorm(curies)
    missing = [c for c in dict.fromkeys(curies) if c not in entries]
    left = len(missing)
    for chunk in chunks(missing, batch or NODENORM_BATCH):
        try:
            response = nodenorm(list(chunk))
        except (httpx.HTTPError, ValueError, OSError) as e:
            _warn(
                f"NodeNorm failed ({type(e).__name__}: {e}); searching {left} nodes by their "
                "own names only"
            )
            break
        got = {c: response.get(c) or None for c in chunk}
        cache.put_nodenorm(got)
        entries.update(got)
        left -= len(chunk)
    return entries


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


def _needs_search(cached: Search | None, retmax: int, refresh: bool) -> bool:
    """A cached search serves unless it has fewer PMIDs than asked for and PubMed had more."""
    if refresh or cached is None:
        return True
    return len(cached.pmids) < retmax and cached.total > len(cached.pmids)


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


@dataclass
class _Tally:
    searched: int = 0
    cached: int = 0
    skipped: int = 0
    skipped_kept: int = 0
    fetched: int = 0
    failures: list[str] = field(default_factory=list)

    def skip(self, plan: _Plan) -> None:
        if self.skipped < MAX_REPORTED_FAILURES:
            nameless = ", ".join(n for n in plan.pair.nodes if not plan.synonyms[n] + plan.mesh[n])
            kept = "keeping its source PMIDs" if _source_pmids(plan.pair) else "no source PMIDs"
            _warn(f"pair {plan.pair.pair_id} not searched: no usable name for {nameless}; {kept}")
        elif self.skipped == MAX_REPORTED_FAILURES:
            _warn("more pairs not searched; not listing each")
        self.skipped += 1

    def fail(self, pair_id: str, reason: str) -> None:
        if len(self.failures) < MAX_REPORTED_FAILURES:
            _warn(f"pair {pair_id} failed: {reason}")
        elif len(self.failures) == MAX_REPORTED_FAILURES:
            _warn("more pairs failed; not listing each")
        self.failures.append(pair_id)


def search_pairs(
    work: Path,
    cache_dir: Path,
    *,
    max_abstracts: int = config.MAX_ABSTRACTS,
    limit: int | None = None,
    refresh: bool = False,
    use_nodenorm: bool = True,
    eutils: EUtils | None = None,
    nodenorm: Callable[[list[str]], dict] | None = None,
) -> dict[str, Any]:
    """Search and fetch every pair in `work/pairs.jsonl` (the first `limit` of them), write
    `work/pubmed.jsonl`, and return counts. Pairs that fail are left out of the file and listed
    in `summary['failed_pairs']`; a rerun retries them, with everything fetched still cached."""
    pairs = list(read_records(work / PAIRS, Pair))
    if limit is not None:
        pairs = pairs[:limit]
    entities = {e.id: e for e in read_records(work / ENTITIES, Entity)}
    client = eutils if eutils is not None else EUtils()
    tally = _Tally()
    out: list[PubmedRecord] = []
    nodes = list(dict.fromkeys(n for p in pairs for n in p.nodes))
    try:
        with PubMedCache(cache_dir / config.PUBMED_CACHE) as cache:
            entries = load_nodenorm(nodes, cache, nodenorm) if use_nodenorm else {}
            plans = [_plan(p, entities, entries) for p in pairs]
            uncached = sum(
                1
                for plan in plans
                if plan.query
                and _needs_search(cache.get_search(plan.query), max_abstracts, refresh)
            )
            if uncached > OFF_PEAK_SEARCHES and is_peak():
                _warn(
                    f"{uncached} uncached searches: NCBI asks that large jobs run on weekends "
                    "or 9pm-5am ET on weekdays"
                )
            done = 0
            for chunk in chunks(plans, CHUNK):
                _run_chunk(chunk, cache, client, max_abstracts, refresh, tally, out)
                done += len(chunk)
                if len(plans) > CHUNK:
                    _warn(
                        f"{done}/{len(plans)} pairs ({tally.searched} searched, "
                        f"{tally.cached} from cache, {len(tally.failures)} failed, "
                        f"{client.requests} requests)"
                    )
    finally:
        if eutils is None:
            client.close()
    write_jsonl(work / PUBMED, out)
    with_text = {f.pmid for r in out for f in r.pmids}
    without = {p for r in out for p in r.no_abstract}
    return {
        "pairs": len(pairs),
        "records": len(out),
        "searched": tally.searched,
        "cached": tally.cached,
        "skipped": tally.skipped,
        "skipped_kept": tally.skipped_kept,
        "failed": len(tally.failures),
        "failed_pairs": tally.failures,
        "requests": client.requests,
        "fetched": tally.fetched,
        "abstracts": len(with_text),
        "no_abstract": len(without),
        "nodenorm_known": sum(1 for n in nodes if entries.get(n)),
    }


def _keep_cached(fetched: list[Abstract], cached: dict[str, Abstract]) -> list[Abstract]:
    """The fetched abstracts, except that a PMID PubMed now returns nothing for keeps what the
    cache had, marked as fetched: fetching it again for its publication types mustn't lose its
    text."""
    return [
        replace(cached[a.pmid], publication_types=[])
        if not a.title and not a.has_text and a.pmid in cached
        else a
        for a in fetched
    ]


def _run_chunk(
    plans: Sequence[_Plan],
    cache: PubMedCache,
    client: EUtils,
    max_abstracts: int,
    refresh: bool,
    tally: _Tally,
    out: list[PubmedRecord],
) -> None:
    """Search the chunk's pairs, fetch the abstracts none of them has cached, then build their
    records. Fetching per chunk rather than per pair fills EFetch batches and fetches an
    abstract shared by several pairs once; a batch that fails fails only its own pairs."""
    searches: dict[str, Search | None] = {}
    for plan in plans:
        pid = plan.pair.pair_id
        if plan.query is None:
            tally.skip(plan)
            if not _source_pmids(plan.pair):
                continue
            tally.skipped_kept += 1
            searches[pid] = None
            continue
        cached = cache.get_search(plan.query)
        if not _needs_search(cached, max_abstracts, refresh):
            tally.cached += 1
            searches[pid] = cached
            continue
        try:
            total, pmids = client.esearch(plan.query, max_abstracts)
        except EUtilsError as e:
            tally.fail(pid, str(e))
            continue
        search = Search(plan.query, today(), total, pmids)
        cache.put_search(search)
        tally.searched += 1
        searches[pid] = search

    wanted: list[str] = []
    for plan in plans:
        if plan.pair.pair_id in searches:
            search = searches[plan.pair.pair_id]
            wanted += search.pmids[:max_abstracts] if search else []
            wanted += _source_pmids(plan.pair)
    for batch in chunks(cache.missing_abstracts(wanted), EFETCH_BATCH):
        try:
            fetched = client.efetch(batch)
        except EUtilsError as e:
            _warn(f"EFetch of {len(batch)} PMIDs failed: {e}")
            continue
        cache.put_abstracts(_keep_cached(fetched, cache.get_abstracts(batch)))
        tally.fetched += len(batch)

    for plan in plans:
        pid = plan.pair.pair_id
        if pid not in searches:
            continue
        record = _record(plan, searches[pid], cache, max_abstracts)
        if record is None:
            tally.fail(pid, "some abstracts could not be fetched")
        else:
            out.append(record)


# --- CLI --------------------------------------------------------------------------------------


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--max-abstracts",
        type=int,
        default=config.MAX_ABSTRACTS,
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
    s = search_pairs(
        work,
        args.cache_dir,
        max_abstracts=args.max_abstracts,
        limit=args.limit,
        refresh=args.refresh,
        use_nodenorm=not args.no_nodenorm,
    )
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
    return 1 if s["failed"] > MAX_FAILED_SHARE * max(s["pairs"], 1) else 0
