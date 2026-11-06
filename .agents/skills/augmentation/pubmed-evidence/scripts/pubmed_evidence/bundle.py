"""A pair's bundle: the edges between two nodes that the run checks, and the claims of the
abstracts read for the pair, as refinement (`refine.py`) takes them. It is gathered per pair from
the work files and the caches, with no request and no model call.

Steps 5 and 6 read the bundles: the review step sends the claims that would count
(`refine.would_count`), and step 6 refines edges on them (`refine.refine_pair`).

- **Pairs.** A pair is bundled when step 2 searched it (`pubmed.jsonl`), the input graph has an
  edge of it in scope, every abstract the label step asks about (`label.pair_pmids`) has an
  answer of the run, and every claim of those answers has its check (`claim_checks`): the pair
  is fully labeled. A pair with an abstract unanswered, after a failed call, or with a claim
  never checked, after a label run stopped between storing its answers and checking them, isn't
  bundled, and its edges stay as they are until a label run finishes it. Nor is a pair with a
  claim that passed its checks and names anatomy the ground step never looked up (its lookup
  failed, or no ground run came after the label run): that claim would count in every context
  without a word. It is listed apart (`Bundles.not_looked_up`), for step 6 to hold, and the
  next ground run looks the name up. A pair with no abstracts to read is bundled with no
  evidence.
- **Edges.** The input graph's edges whose statement select kept (`statements.jsonl`), each with
  its `id` (`graphio.edge_id`) and its `publications` as its source cited them
  (`graphio.as_cited`), never one an earlier run added (`kgbcia-augmentation:`). An id that two
  different records of the graph give is an error. The pair's other edges, those select left
  out, come too (`others`): they aren't checked, but a proposal that one of them already makes
  links to it instead of adding a duplicate.
- **Evidence.** One `PublicationEvidence` per abstract, by PMID: PubMed's publication types,
  year and retraction from the PubMed cache; the study design PubMed's types name, else the
  extractor's (`pubmed.design_of`), whose own answer is kept either way; and `found_by`
  `search` when step 2's search found it. Whether an edge's own publications cite it (`source`)
  is a matter of the edge, for step 6 to add.
- **Claims.** Each abstract's claims as `ClaimRecord`s, but for those that failed a check other
  than the sentence's (`claim_checks`): they count for nothing. A
  sentence counts as found when its check found it or a repair by the run's repairer did
  (`repair.sentence_repairs`), and is then the repair's. Anatomy is grounded through the ground
  step's cache (`ground.groundings`); a name it looked up and didn't ground stays as
  `anatomy_name`, which goes to `lit_context_notes`. Each
  statement reads the way round Biolink's domain and range put it (`refine.directed`), so
  'gene condition_associated_with_gene disease' is 'gene gene_associated_with_condition
  disease'; the stored claim, which `claim_id` hashes, stays as the model gave it. A claim
  carries the verdict of the reviewer and review prompt version given, if any.

`Bundle.keys` names each claim as the claim tables key it, (pair, PMID, `claim_id`), in the
order of `Bundle.claims`, which is `refine.claims_of`'s, and so that of the claim indices
refinement gives.
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from pubmed_evidence import config, graphio, ground, label, pubmed, records, refine, repair, uberon
from pubmed_evidence.cache import (
    AbstractClaimsRow,
    ClaimCheckRow,
    Grounding,
    LabelCache,
    PubMedCache,
    canonical,
    claim_id,
)
from pubmed_evidence.records import Entity, LabelRun, PubmedRecord
from shared.schema import (
    ClaimContext,
    ClaimRecord,
    Edge,
    PublicationEvidence,
    ReviewVerdict,
    Statement,
)

# A claim as the claim tables key it: (pair_id, PMID as bare digits, claim_id).
ClaimKey = tuple[str, str, str]
# Pairs whose abstracts and claims are read together, so a large run never holds every
# abstract's text at once.
CHUNK = 100

# The step that writes each input, for the error when one is missing.
_WORK_FILES = {
    records.STATEMENTS: "select",
    records.ENTITIES: "select",
    records.PUBMED: "pubmed",
}
_CACHE_FILES = {
    config.LABEL_CACHE: "label",
    config.PUBMED_CACHE: "pubmed",
    config.UBERON_FILE: "ground",
}


@dataclass(frozen=True)
class Bundle:
    """One fully labeled pair: the edges of it the run checks, one evidence record per
    abstract read, each claim's key, in `claims` order, and the pair's other edges, which select
    left out: a proposal links to one that already makes its statement."""

    pair_id: str
    edges: tuple[Edge, ...]
    evidence: tuple[PublicationEvidence, ...]
    keys: tuple[ClaimKey, ...]
    others: tuple[Edge, ...] = ()

    @cached_property
    def claims(self) -> list[refine.Claim]:
        """Every claim of the pair's abstracts, as `refine.refine_pair` takes them."""
        return refine.claims_of(self.evidence)


@dataclass
class Bundles:
    """What `load` read: the fully labeled pairs, by pair id, with what refining them needs."""

    pairs: list[Bundle]
    entities: dict[str, Entity]
    lattice: refine.Lattice
    uberon_version: str | None
    unlabeled: list[str] = field(default_factory=list)  # pairs with an abstract unanswered
    # Pairs with a claim naming anatomy the ground step never looked up.
    not_looked_up: list[str] = field(default_factory=list)
    counts: Counter = field(default_factory=Counter)  # what was left out, and why

    @cached_property
    def categories(self) -> dict[str, list[str]]:
        """Node id -> Biolink categories, for `refine`'s association checks."""
        return {e.id: list(e.category) for e in self.entities.values()}


def claim_record(
    stored: Mapping[str, Any],
    *,
    sentence_found: bool,
    repaired: str | None = None,
    grounding: Grounding | None = None,
    verdict: tuple[ReviewVerdict, str] | None = None,
    answered_by: str | None = None,
    categories: Mapping[str, Sequence[str]] | None = None,
) -> ClaimRecord:
    """A claim as `abstract_claims` stores it, as the shared schema's record: its sentence the
    repair's (`repaired`) when its own wasn't found, its anatomy grounded (`grounding`, the
    cached grounding of the name it gives) and its review (`verdict`, a (verdict, reason)).
    With the nodes' `categories`, each statement is the way round Biolink reads it
    (`refine.directed`), as the label step checked it; one whose predicate fits neither way
    raises ValueError."""
    context = stored.get("context") or {}
    name = ground.claim_anatomy(stored)
    use_repair = not sentence_found and repaired is not None
    review, reason = verdict if verdict is not None else (None, None)
    statements = []
    for entry in [stored["statement"], *(stored.get("also_supports") or ())]:
        statement = Statement.model_validate(entry)
        if categories is not None:
            statement = refine.directed(statement, categories)
            if statement is None:
                raise ValueError(label.misfit(entry, categories))
        statements.append(statement)
    return ClaimRecord(
        polarity=stored["polarity"],
        statement=statements[0],
        also_supports=statements[1:],
        claim_role=stored["claim_role"],
        hedged=bool(stored["hedged"]),
        negative_finding_type=stored.get("negative_finding_type"),
        context=ClaimContext(
            species=context.get("species"),
            anatomy=grounding.curie if grounding is not None else None,
            anatomy_name=name,
            notes=context.get("notes"),
        ),
        evidence_sentence=repaired if use_repair else stored["evidence_sentence"],
        evidence_sentence_valid=sentence_found or use_repair,
        evidence_sentence_repaired=use_repair,
        review=review,
        review_reason=reason,
        answered_by=answered_by,
    )


def edges_of(
    path: Path, statements: set[str], pair_ids: set[str]
) -> tuple[dict[str, list[Edge]], dict[str, list[Edge]]]:
    """The edges of `path` between the pairs in `pair_ids`, each with its id and its
    publications as its source cited them, by pair, in file order; none an earlier run added:
    those whose statement is in `statements`, and the others. Raises ValueError for such an edge
    that isn't a valid Edge, and for an id that two different records of the file give
    (identical records may repeat: they are one edge, refined once), since step 6 writes each
    id's outcome on every edge that has it."""
    out: dict[str, list[Edge]] = defaultdict(list)
    others: dict[str, list[Edge]] = defaultdict(list)
    seen: dict[str, tuple[int, bytes]] = {}  # an id records give -> (first line, its digest)
    for n, row in enumerate(graphio.read_jsonl(path), 1):
        own = row.get("id")
        if isinstance(own, str) and own and not own.startswith(graphio.ID_PREFIX):
            digest = hashlib.sha256(canonical(row).encode("utf-8")).digest()
            first, known = seen.setdefault(own, (n, digest))
            if known != digest:
                raise ValueError(
                    f"edges {first} and {n} of {path} share the id {own!r} but differ; an id "
                    "must name one edge"
                )
        subject, object = row.get("subject"), row.get("object")
        if not isinstance(subject, str) or not isinstance(object, str):
            continue
        pid = graphio.pair_id(subject, object)
        if pid not in pair_ids:
            continue
        eid = graphio.edge_id(row)
        if eid.startswith(graphio.ID_PREFIX):
            continue
        try:
            edge = Edge.model_validate({**graphio.as_cited(row), "id": eid})
        except ValidationError as e:
            problem = e.errors()[0]
            where = ".".join(map(str, problem["loc"])) or "record"
            raise ValueError(
                f"edge {n} of {path} is not a valid Edge: {where}: {problem['msg']}"
            ) from e
        if graphio.edge_statement_id(edge) in statements:
            out[pid].append(edge)
        else:
            others[pid].append(edge)
    return dict(out), dict(others)


def load(
    work: Path,
    cache_dir: Path,
    input_dir: Path,
    run: LabelRun,
    *,
    reviewer: str | None = None,
    review_prompt_version: str | None = None,
    limit: int | None = None,
) -> Bundles:
    """Bundle every pair of the work directory that `run` fully labeled, by pair id, with the
    verdicts of `reviewer` under `review_prompt_version` (none unless both are given), and the
    lattice of the UBERON release in the cache directory. `limit` keeps the first pairs by id.
    Raises FileNotFoundError naming the step to run first, ValueError for an invalid edge."""
    work, cache_dir, input_dir = Path(work), Path(cache_dir), Path(input_dir)
    for name, step in _WORK_FILES.items():
        if not (work / name).exists():
            raise FileNotFoundError(f"{work / name} is missing: run `kgb-augment {step}` first")
    for name, step in _CACHE_FILES.items():
        if not (cache_dir / name).exists():
            raise FileNotFoundError(
                f"{cache_dir / name} is missing: run `kgb-augment {step}` first"
            )
    edges_file = graphio.graph_file(input_dir, graphio.EDGES)
    if not edges_file.exists():
        raise FileNotFoundError(f"{input_dir / graphio.EDGES} is missing: no input graph")
    scope = {
        s.statement_id for s in records.read_records(work / records.STATEMENTS, records.Statement)
    }
    searched = {r.pair_id: r for r in records.read_records(work / records.PUBMED, PubmedRecord)}
    entities = {e.id: e for e in records.read_records(work / records.ENTITIES, Entity)}
    edges, others = edges_of(edges_file, scope, set(searched))
    pair_ids = sorted(p for p in searched if p in edges)
    release = uberon.load(cache_dir / config.UBERON_FILE)
    out = Bundles([], entities, refine.Lattice(anatomy=release.ancestors), release.version)
    if len(searched) > len(pair_ids):
        out.counts["pairs searched with no edge in scope"] = len(searched) - len(pair_ids)
    if limit is not None:
        pair_ids = pair_ids[: max(0, limit)]
    review = (reviewer, review_prompt_version) if reviewer and review_prompt_version else None
    with (
        LabelCache(cache_dir / config.LABEL_CACHE) as labels,
        PubMedCache(cache_dir / config.PUBMED_CACHE) as cache,
    ):
        for chunk in graphio.chunks(pair_ids, CHUNK):
            records_ = {p: searched[p] for p in chunk}
            _load_chunk(list(chunk), records_, edges, others, labels, cache, run, review, out)
    return out


def _load_chunk(
    pair_ids: Sequence[str],
    searched: Mapping[str, PubmedRecord],
    edges: Mapping[str, list[Edge]],
    others: Mapping[str, list[Edge]],
    labels: LabelCache,
    cache: PubMedCache,
    run: LabelRun,
    review: tuple[str, str] | None,
    out: Bundles,
) -> None:
    texts = cache.get_abstracts(p.pmid for r in searched.values() for p in r.pmids)
    asked = {pid: label.pair_pmids(searched[pid], texts) for pid in pair_ids}
    del texts
    pv, model = run.prompt_version, run.model
    answers = {(a.pair_id, a.pmid): a for a in labels.abstract_claims(pv, model, pair_ids)}
    checks = {(c.pair_id, c.pmid, c.claim_id): c for c in labels.claim_checks(pv, model, pair_ids)}
    repairs = repair.sentence_repairs(
        labels, pv, model, run.repairer, run.repair_prompt_version, pair_ids
    )
    verdicts: dict[ClaimKey, tuple[ReviewVerdict, str]] = {}
    if review is not None:
        for r in labels.claim_reviews(pv, model, *review, pair_ids=pair_ids):
            verdicts[(r.pair_id, r.pmid, r.claim_id)] = (r.verdict, r.reason)
    articles = cache.articles(p for pmids in asked.values() for p in pmids)
    names = {n for a in answers.values() for c in a.claims if (n := ground.claim_anatomy(c))}
    grounded = ground.groundings(cache, names)
    for pid in pair_ids:
        if any((pid, p) not in answers for p in asked[pid]):
            out.unlabeled.append(pid)
            continue
        stored_claims = (
            (pid, p, claim_id(c)) for p in asked[pid] for c in answers[(pid, p)].claims
        )
        if any(key not in checks for key in stored_claims):
            out.unlabeled.append(pid)
            out.counts["pairs with claims never checked"] += 1
            continue
        never = {
            name
            for p in asked[pid]
            for c in answers[(pid, p)].claims
            if (name := ground.claim_anatomy(c))
            and name not in grounded
            and checks[(pid, p, claim_id(c))].valid
        }
        if never:
            out.not_looked_up.append(pid)
            out.counts["pairs with anatomy names never grounded"] += 1
            continue
        found = {f.pmid: "search" in f.origin.split("+") for f in searched[pid].pmids}
        evidence: list[PublicationEvidence] = []
        keys: list[ClaimKey] = []
        for pmid in sorted(asked[pid], key=graphio.pmid_key):
            answer = answers[(pid, pmid)]
            claims = []
            for stored in answer.claims:
                key = (pid, pmid, claim_id(stored))
                record = _record(stored, key, answer, checks, repairs, grounded, verdicts, out)
                if record is not None:
                    claims.append(record)
                    keys.append(key)
            article = articles.get(pmid)
            if article is None or article[1] is None:
                out.counts["abstracts without publication types"] += 1
            evidence.append(_evidence(answer, article, found.get(pmid, False), claims))
        outside = tuple(others.get(pid, ()))
        out.pairs.append(Bundle(pid, tuple(edges[pid]), tuple(evidence), tuple(keys), outside))


def _record(
    stored: dict[str, Any],
    key: ClaimKey,
    answer: AbstractClaimsRow,
    checks: Mapping[ClaimKey, ClaimCheckRow],
    repairs: Mapping[ClaimKey, str],
    grounded: Mapping[str, Grounding],
    verdicts: Mapping[ClaimKey, tuple[ReviewVerdict, str]],
    out: Bundles,
) -> ClaimRecord | None:
    """The claim's record, or None for one left out: it failed a check. Every claim of a pair
    bundled has its check."""
    check = checks[key]
    if not check.valid:
        out.counts["claims failing a check"] += 1
        return None
    name = ground.claim_anatomy(stored)
    try:
        return claim_record(
            stored,
            sentence_found=check.sentence_valid,
            repaired=repairs.get(key),
            grounding=grounded.get(name) if name else None,
            verdict=verdicts.get(key),
            answered_by=answer.answered_by,
            categories=out.categories,
        )
    except ValueError:  # a pydantic ValidationError too
        out.counts["claims failing a check"] += 1
        return None


def _evidence(
    answer: AbstractClaimsRow,
    article: tuple[int | None, list[str] | None] | None,
    searched: bool,
    claims: Iterable[ClaimRecord],
) -> PublicationEvidence:
    year, types = article if article is not None else (None, None)
    types = list(types or [])
    kind, retracted = pubmed.article_type(types)
    design, source = pubmed.design_of(types, answer.study_design)
    return PublicationEvidence(
        publication=graphio.publication_of(answer.pmid),
        publication_types=types,
        supporting_document_type=kind,
        supporting_document_year=year,
        retracted=retracted,
        found_by=["search"] if searched else [],
        study_design=design,
        study_design_source=source,
        extractor_study_design=answer.study_design,
        claims=list(claims),
    )
