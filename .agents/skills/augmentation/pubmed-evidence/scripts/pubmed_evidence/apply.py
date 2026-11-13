"""Step 6: refine every checked edge from its pair's claims, and write the graph, in code.

Step 6 reads the input graph, the run's work files and both caches through `bundle.py`, and
refines each pair (`refine.refine_pair`) by the rules DESIGN.md's step 6 gives, cited here by
their numbers ("rule 4"). No model is called, so the graph can be rebuilt from the caches, as
"agents decide, code transforms" asks.

- Every input edge gets an `id`: its own, or a hash of the record (`graphio.edge_id`).
- **Checked edges.** An edge is checked when select kept its statement and its pair is fully
  labeled (every abstract step 3 asks about has an answer of the run) and not held. A pair with
  no abstracts is checked too, and its edges are unsupported. A checked edge comes out as an
  `AugmentedEdge`: the record as read, every slot keeping its exact value, with `augmentation:
  checked`. The counting claims with a statement narrower than or equal to its own support it
  (rules 1 to 3): their PMIDs are its `lit_supporting_publications`, and their number its
  `lit_support_score`. Those its source didn't cite are appended to its `publications` and
  listed in `lit_added_publications`. `lit_refined_by` names the more specific edges its
  supporting claims make, and `lit_evidence` lists every abstract read for the pair, each claim
  marked with whether it `supports` the edge. An unsupported edge is written the same way, with
  score 0, so that "checked, nothing found" reads apart from "never checked".
- **Proposed edges** (rule 4). A statement strictly narrower than an original, that counting
  claims make, is one edge, whose id is `kgbcia-augmentation:` plus a hash of the statement's
  key (`refine.statement_key`, the same for a symmetric statement either way round) and whose
  `original_edge_ids` name every original it refines. Its provenance is this stage's:
  config.INFORES, with `supporting_data_source` [infores:pubmed], a `knowledge_assertion` by a
  `text_mining_agent`, and as `publications` the PMIDs of the claims that make it. A statement an
  edge of the pair already makes, whether select kept that edge or left it out, isn't added: the
  originals it refines name that edge in `lit_refined_by` instead (a link, rule 5).
- **Species in proposals** (rule 7): unless config.SPECIES_IN_PROPOSALS or
  `--species-in-proposals` keeps it, the species is taken out of every proposal but one that
  refines an original with that same species, which keeps it so as to stay narrower than that
  original (`refine.without_species`). Proposals that become the same statement are one edge,
  with the PMIDs and originals of each. One that becomes an original it refines ("E in human"
  becomes E) no longer refines it, and is dropped if it refines nothing else; one that becomes
  an edge of the pair is a link to it. The report counts the species-qualified proposals folded
  so. The originals' support and PMIDs are the same either way.
- **Held pairs.** A pair is held, and its edges in scope pass through as read and are counted,
  while a claim of it that would count has no review (`kgb-augment review` clears it), or a
  claim names anatomy the ground step never looked up (`kgb-augment ground` clears it), so that
  a partial run never writes a partial answer. `--no-review` counts the unreviewed claims
  instead, for testing only; a rejected claim counts for nothing either way.
- **Support only** (rule 11). A claim statement narrower than an edge only by a predicate
  config.SUPPORT_ONLY lists under the edge's (`ameliorates_condition` under `treats`) supports
  the edge but is never proposed under it, and, unless config.REVIEW_SUPPORT_ONLY or
  `--review-support-only`, counts without review and holds no pair. Give `review` the same
  flag: a claim it didn't send holds its pair here until it is reviewed.
- **Unchecked edges** pass through as read, apart from their `id`: those out of scope, and those
  in a pair that isn't fully labeled after a failed call, or with claims a stopped label run
  never checked, which the next label run finishes.
- **Rerunning on its own output.** Every field step 6 writes on an input edge, now or in an
  earlier evidence format, is stripped, and the PMIDs it appended are taken back out of
  `publications` (`graphio.as_cited`), before the edge is refined again; and the edges an
  earlier run added (`kgbcia-augmentation:`) are dropped: the run adds its own.

It writes `nodes.jsonl` and `edges.jsonl` (the originals in input order, then the proposed edges
by id) to the output directory, and `refinements.jsonl` (one line per refined pair, by pair) and
`apply_report.json` to the work directory. The graph and `refinements.jsonl` are written under
temporary names and renamed into place at the end, so a failed run leaves none half-written;
`apply_report.json` is written after them.
"""

from __future__ import annotations

import argparse
import itertools
import os
import sys
from collections.abc import Collection, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pubmed_evidence import bundle, cache, config, graphio, records, refine, repair
from pubmed_evidence.bundle import Bundle, Bundles
from shared.pubmed import default_cache_path
from shared.schema import AugmentedEdge, Edge, Statement

APPLY_REPORT = records.APPLY_REPORT
REFINEMENTS = records.REFINEMENTS
# The fields of the evidence formats before this one, which an input edge from an earlier run
# may still carry.
EARLIER_FIELDS = (
    "negates_original",
    "same_statement_edge_ids",
    "evidence",
    "evidence_score",
    "evidence_counts",
    "lit_action",
    "lit_validation_status",
    "lit_support_mean",
    "lit_support_interval",
    "lit_evidence_mass",
    "lit_refuting_publications",
    "lit_unresolved_dimensions",
    "lit_superseded_by",
    "lit_decision_rule",
    "lit_rule_trace",
)
# Every field step 6 has written on an input edge: AugmentedEdge's own and the earlier ones,
# stripped before an edge is refined again. Not `supporting_data_source`: it is a Biolink slot
# a source may set, and step 6 writes it only on the edges it adds, which are dropped whole.
AUGMENTED = tuple(
    dict.fromkeys(
        [
            *(
                f
                for f in AugmentedEdge.model_fields
                if f not in Edge.model_fields and f != "supporting_data_source"
            ),
            *EARLIER_FIELDS,
        ]
    )
)


class ApplyError(RuntimeError):
    """A problem with the inputs that stops step 6, with a one-line message."""


def resolve_run(
    work: Path,
    model: str | None = None,
    prompt_version: str | None = None,
    reviewer: str | None = None,
    review_prompt_version: str | None = None,
) -> records.LabelRun:
    """The run to apply: `label_run.json` in the work dir, with each given value overriding it.
    The review's effort carries over only while the review is the stored one."""
    stored = records.read_label_run(work)
    run = records.LabelRun(
        model=model or (stored.model if stored else ""),
        prompt_version=prompt_version or (stored.prompt_version if stored else ""),
        prompt=stored.prompt if stored else None,
        reasoning_effort=stored.reasoning_effort if stored else None,
        reviewer=reviewer or (stored.reviewer if stored else None),
        review_prompt_version=review_prompt_version
        or (stored.review_prompt_version if stored else None),
        repairer=stored.repairer if stored else None,
        repair_prompt_version=stored.repair_prompt_version if stored else None,
        uberon_version=stored.uberon_version if stored else None,
    )
    if not run.model or not run.prompt_version:
        raise ApplyError(
            f"no label run: {work / records.LABEL_RUN} not found or incomplete; run "
            "`kgb-augment label` first, or pass --model and --prompt-version"
        )
    if run.reviewer and not run.review_prompt_version:
        raise ApplyError(f"reviewer {run.reviewer} has no review prompt version; pass one")
    reviewed = (run.reviewer, run.review_prompt_version)
    if stored is not None and reviewed == (stored.reviewer, stored.review_prompt_version):
        run.review_reasoning_effort = stored.review_reasoning_effort
    return run


def has_claims(labels: cache.LabelCache, run: records.LabelRun) -> bool:
    """True if the cache holds any per-pair answer of the run."""
    row = labels.db.execute(
        "SELECT 1 FROM abstract_claims WHERE prompt_version = ? AND model = ? LIMIT 1",
        (run.prompt_version, run.model),
    ).fetchone()
    return row is not None


# --- Refining the pairs ---------------------------------------------------------------------


@dataclass(frozen=True)
class Refined:
    """A pair step 6 refined: its bundle and its refinement, with the species taken out of its
    proposals unless they keep it."""

    bundle: Bundle
    refinement: refine.PairRefinement


@dataclass
class Refinements:
    """Every bundled pair's refinement, as step 6 writes it."""

    pairs: list[Refined] = field(default_factory=list)  # by pair id; none held
    outcomes: dict[str, tuple[Refined, refine.EdgeOutcome]] = field(default_factory=dict)
    held: list[str] = field(default_factory=list)  # pairs held for a claim with no review

    @property
    def folded(self) -> int:
        """The species-qualified proposals and links folded into ones without a species."""
        return sum(len(r.refinement.folded) for r in self.pairs)


def refine_pairs(
    loaded: Bundles,
    require_review: bool = True,
    species_in_proposals: bool = config.SPECIES_IN_PROPOSALS,
    review_support_only: bool = config.REVIEW_SUPPORT_ONLY,
) -> Refinements:
    """Refine every bundled pair (`refine.refine_pair`). A pair with a claim that would count
    and has no review is held, unless `require_review` is False; a held pair contributes
    nothing. `review_support_only` requires review for support-only statements too (rule
    11). Unless `species_in_proposals`, the species is taken out of the proposals
    (`refine.without_species`). Each edge's outcome is kept by its id, the first one where an
    edge is listed twice (identical records; `bundle.edges_of` refuses an id two different ones
    give)."""
    out = Refinements()
    for b in loaded.pairs:
        r = refine.refine_pair(
            b.edges,
            b.claims,
            loaded.lattice,
            loaded.categories,
            b.others,
            require_review=require_review,
            review_support_only=review_support_only,
        )
        if r.held:
            out.held.append(b.pair_id)
            continue
        if not species_in_proposals:
            r = refine.without_species(r, b.others)
        refined = Refined(b, r)
        out.pairs.append(refined)
        for o in r.edges:
            out.outcomes.setdefault(o.edge_id, (refined, o))
    return out


# --- What step 6 writes ---------------------------------------------------------------------


def _slots(statement: Statement, negated: bool = False) -> dict[str, Any]:
    """A statement as an edge's slots: subject, predicate, object, `qualified_predicate` and
    each qualifier, and `negated` when it's negated, or always with `negated`."""
    out: dict[str, Any] = {
        "subject": statement.subject,
        "predicate": statement.predicate,
        "object": statement.object,
    }
    if statement.qualified_predicate is not None:
        out["qualified_predicate"] = statement.qualified_predicate
    out.update(sorted(statement.qualifiers.items()))
    if statement.negated or negated:
        out["negated"] = statement.negated
    return out


def _provenance(publications: Sequence[str]) -> dict[str, Any]:
    """A proposed edge's provenance: this stage's infores, its data from PubMed, asserted by
    text mining, and the PMIDs that support it."""
    return {
        "primary_knowledge_source": config.INFORES,
        "aggregator_knowledge_source": [],
        "supporting_data_source": [config.SUPPORTING_DATA_SOURCE],
        "knowledge_level": config.NEW_EDGE_KNOWLEDGE_LEVEL,
        "agent_type": config.NEW_EDGE_AGENT_TYPE,
        "publications": list(publications),
    }


def _cited(publications: Iterable[str]) -> set[str]:
    """The PubMed ids among an edge's publications, as 'PMID:n'."""
    return {graphio.publication_of(p) for p in map(graphio.pmid_of, publications) if p}


def lit_evidence(b: Bundle, supports: Collection[int], cited: set[str]) -> list[dict[str, Any]]:
    """The pair's abstracts as listed on one edge: each claim with whether it `supports` the
    edge (its index in `b.claims` is in `supports`), and `source` first in an abstract's
    `found_by` when `cited` ('PMID:n', the edge's publications as its source cited them, or a
    proposed edge's originals') has it."""
    supports = set(supports)
    index = itertools.count()
    out = []
    for entry in b.evidence:
        claims = [r.model_copy(update={"supports": next(index) in supports}) for r in entry.claims]
        found = list(entry.found_by)
        if entry.publication in cited and "source" not in found:
            found.insert(0, "source")
        listed = entry.model_copy(update={"claims": claims, "found_by": found})
        out.append(listed.model_dump(mode="json", exclude_none=True))
    return out


def checked_edge(
    row: dict[str, Any], eid: str, refined: Refined, outcome: refine.EdgeOutcome
) -> dict[str, Any]:
    """A checked input edge as written: the record as read (without an earlier run's fields),
    with its id, the supporting PMIDs its source didn't cite appended to its publications, and
    its outcome."""
    out = _plain(row, eid)
    cited = list(out.get("publications") or ())
    if outcome.added:
        out["publications"] = [*cited, *outcome.added]
    out |= {
        "augmentation": "checked",
        "original_edge_ids": [eid],
        "lit_support_score": outcome.score,
        "lit_supporting_publications": list(outcome.pmids),
        "lit_added_publications": list(outcome.added),
        "lit_refined_by": list(outcome.refined_by),
        "lit_context_notes": list(outcome.context_notes),
        "lit_evidence": lit_evidence(refined.bundle, outcome.claims, _cited(cited)),
    }
    AugmentedEdge.model_validate(out)
    return out


def proposed_edge(refined: Refined, proposal: refine.Proposal) -> dict[str, Any]:
    """A proposed edge as written, with this stage's provenance; its evidence is found against
    its originals' publications."""
    b, p = refined.bundle, proposal
    originals = {e.id: e for e in b.edges}
    cited = _cited(x for e in p.original_edge_ids for x in originals[e].publications)
    out = {
        "id": p.id,
        **_slots(p.statement),
        **_provenance(p.pmids),
        "original_edge_ids": list(p.original_edge_ids),
        "augmentation": "proposed",
        "lit_support_score": p.score,
        "lit_supporting_publications": list(p.pmids),
        "lit_context_notes": list(p.context_notes),
        "lit_evidence": lit_evidence(b, p.claims, cited),
    }
    AugmentedEdge.model_validate(out)
    return out


def _proposal_record(p: refine.Proposal) -> dict[str, Any]:
    return {
        **_slots(p.statement, negated=True),
        "publications": list(p.pmids),
        "support_score": p.score,
        "original_edge_ids": list(p.original_edge_ids),
    }


def pair_record(refined: Refined) -> dict[str, Any]:
    """A refined pair's line of `refinements.jsonl`: the number of abstracts read for it; each
    in-scope edge's statement, support and PMIDs, and the edges that refine it; the proposed
    edges; and the links, each with the edges of the pair that already make its statement
    (`linked_to`). An edge listed twice is listed once."""
    r = refined.refinement
    edges = {}
    for o in r.edges:
        edges.setdefault(
            o.edge_id,
            {
                "edge_id": o.edge_id,
                "statement": _slots(o.statement, negated=True),
                "supported": o.supported,
                "support_score": o.score,
                "supporting_publications": list(o.pmids),
                "added_publications": list(o.added),
                "refined_by": list(o.refined_by),
            },
        )
    return {
        "pair_id": refined.bundle.pair_id,
        "abstracts_read": len(refined.bundle.evidence),
        "edges": list(edges.values()),
        "proposed_edges": [{"id": p.id, **_proposal_record(p)} for p in r.proposed],
        "links": [{"linked_to": list(p.existing), **_proposal_record(p)} for p in r.links],
    }


def from_earlier_run(row: dict[str, Any]) -> bool:
    """Whether an input edge is one an earlier run of step 6 wrote: it carries `augmentation`
    (this format) or a `lit_` field (this format's and every earlier one's)."""
    return "augmentation" in row or any(k.startswith("lit_") for k in row)


def _plain(row: dict[str, Any], eid: str) -> dict[str, Any]:
    """An input edge as read, with its id, and with its publications as its source cited them.
    An edge from an earlier run also loses the fields that run wrote (AUGMENTED).

    Decision (review of #22, Nov 2026): strip only edges an earlier run wrote, and leave every
    other edge exactly as its source wrote it. AUGMENTED holds generic names (`augmentation`,
    `original_edge_ids`, `evidence`, `evidence_score`, `evidence_counts`) that a source may use
    for its own data; stripping them from every edge would silently delete that data, even on
    edges this step never checked. On the filter's output, the normal input, nothing is
    stripped."""
    out = {**graphio.as_cited(row), "id": eid}
    if not from_earlier_run(row):
        return out
    return {k: v for k, v in out.items() if k not in AUGMENTED}


class _Pass:
    """One pass over the input edges, then the proposed edges, counting what it writes."""

    def __init__(self, refined: Refinements, scope: set[str], unlabeled: set[str], held: set[str]):
        self.refined = refined
        self.scope = scope  # the statements select kept
        self.unlabeled = unlabeled  # the pairs not fully labeled
        self.held = held  # the pairs held
        self.stats = dict.fromkeys(
            (
                "edges_read",
                "ids_assigned",
                "earlier_edges_dropped",
                "edges_checked",
                "edges_supported",
                "edges_unsupported",
                "edges_refined",
                "publications_added",
            ),
            0,
        )
        self.unchecked = dict.fromkeys(("not_fully_labeled", "not_searched", "held"), 0)
        self.invalid = graphio.InvalidEdges()

    def originals(self, rows: Iterable[dict[str, Any]], path: Path) -> Iterator[dict[str, Any]]:
        """The input edges, each with its `id`, the checked ones with their outcome; none an
        earlier run added. Each is validated as an Edge, but written back as its own dict; one
        that isn't valid is written exactly as read and counted (graphio.InvalidEdges)."""
        stats = self.stats
        for row in rows:
            stats["edges_read"] += 1
            edge = self.invalid.check(row, stats["edges_read"])
            if edge is None:
                yield row
                continue
            if not row.get("id"):
                stats["ids_assigned"] += 1
            eid = graphio.edge_id(row)
            if eid.startswith(graphio.ID_PREFIX):
                stats["earlier_edges_dropped"] += 1
                continue
            found = self.refined.outcomes.get(eid)
            if found is None:
                if graphio.edge_statement_id(edge) in self.scope:
                    pid = graphio.pair_id(edge.subject, edge.object)
                    if pid in self.held:
                        self.unchecked["held"] += 1
                    elif pid in self.unlabeled:
                        self.unchecked["not_fully_labeled"] += 1
                    else:
                        self.unchecked["not_searched"] += 1
                yield _plain(row, eid)
                continue
            refined, outcome = found
            stats["edges_checked"] += 1
            stats["edges_supported" if outcome.supported else "edges_unsupported"] += 1
            stats["edges_refined"] += bool(outcome.refined_by)
            stats["publications_added"] += len(outcome.added)
            yield checked_edge(row, eid, refined, outcome)

    def added(self) -> Iterator[dict[str, Any]]:
        proposals = [(p, r) for r in self.refined.pairs for p in r.refinement.proposed]
        for p, r in sorted(proposals, key=lambda x: x[0].id):
            yield proposed_edge(r, p)


def _claims(loaded: Bundles, refined: Refinements) -> dict[str, int]:
    """The claims of the bundled pairs, by review; those that count toward an edge of a refined
    pair; and those whose sentence is a repair's."""
    out = dict.fromkeys(
        ("used", "counting", "upheld", "rejected", "unreviewed", "sentences_repaired"), 0
    )
    for b in loaded.pairs:
        for claim in b.claims:
            record = claim.record
            out["used"] += 1
            verdict = {"upheld": "upheld", "reject": "rejected"}.get(record.review or "")
            out[verdict or "unreviewed"] += 1
            out["sentences_repaired"] += record.evidence_sentence_repaired
    out["counting"] = sum(
        len({i for o in r.refinement.edges for i in o.claims}) for r in refined.pairs
    )
    return out


def _load(input_dir: Path, cache_dir: Path, work: Path, run: records.LabelRun) -> Bundles:
    """The run's bundled pairs, with its review's verdicts; ApplyError for a missing input, an
    invalid edge in scope, or a run with no claims where some are needed."""
    label_path = cache_dir / config.LABEL_CACHE
    for path, step in ((label_path, "label"), (default_cache_path(), "pubmed")):
        if not path.exists():
            raise ApplyError(f"no {step} cache at {path}; run `kgb-augment {step}` first")
    try:
        loaded = bundle.load(
            work,
            cache_dir,
            input_dir,
            run,
            reviewer=run.reviewer,
            review_prompt_version=run.review_prompt_version,
        )
    except (FileNotFoundError, ValueError) as e:
        raise ApplyError(str(e)) from e
    if loaded.unlabeled:
        with cache.LabelCache(label_path) as labels:
            if not has_claims(labels, run):
                raise ApplyError(
                    f"no claims for {run.model} {run.prompt_version} in {label_path}; run "
                    "`kgb-augment label`, or pass the --model and --prompt-version of a run"
                )
    return loaded


def _warn(report: dict[str, Any], run: records.LabelRun) -> None:
    held, unchecked = report["held"], report["unchecked"]
    if held["pairs_unreviewed"]:
        who = run.reviewer
        why = f"{who} hasn't reviewed them" if who else "the run has no review"
        print(
            f"apply: warning: {held['pairs_unreviewed']} pairs have claims that would count but "
            f"no review ({why}), so their edges are written unchanged; run `kgb-augment "
            "review`, or pass --no-review to count them, for testing only",
            file=sys.stderr,
        )
    if held["pairs_anatomy_not_looked_up"]:
        print(
            f"apply: warning: {held['pairs_anatomy_not_looked_up']} pairs have a claim naming "
            "anatomy the ground step never looked up (a lookup failed, or no ground run came "
            "after the label run), so their edges are written unchanged; run `kgb-augment "
            "ground` to look the names up",
            file=sys.stderr,
        )
    if unchecked["pairs_not_fully_labeled"]:
        print(
            f"apply: warning: {unchecked['pairs_not_fully_labeled']} pairs aren't fully labeled "
            f"(a label call failed or left an abstract out, or a label run stopped before "
            f"checking its claims), so their {unchecked['edges_not_fully_labeled']} edges in "
            "scope are written unchecked; rerun `kgb-augment label` to finish them",
            file=sys.stderr,
        )
    untyped = report["skipped"].get("abstracts without publication types", 0)
    if untyped:
        print(
            f"apply: warning: {untyped} abstracts have no publication types, so PubMed sets "
            "neither a study design nor a retraction for them; rerun `kgb-augment pubmed` to "
            "fetch them",
            file=sys.stderr,
        )
    warning = repair.stale(run)
    if warning:
        print(f"apply: warning: {warning}", file=sys.stderr)


def apply_claims(
    input_dir: Path,
    output_dir: Path,
    cache_dir: Path,
    work: Path,
    *,
    require_review: bool = True,
    species_in_proposals: bool | None = None,
    review_support_only: bool | None = None,
    model: str | None = None,
    prompt_version: str | None = None,
    reviewer: str | None = None,
    review_prompt_version: str | None = None,
) -> dict[str, Any]:
    """Refine the checked edges, write the graph to `output_dir` and `refinements.jsonl` and
    `apply_report.json` to `work`; return the report. `species_in_proposals` keeps the species
    in proposed edges (default config.SPECIES_IN_PROPOSALS). `review_support_only` counts
    support-only statements only once upheld (default config.REVIEW_SUPPORT_ONLY; rule 11).
    Raises ApplyError on a problem with the inputs."""
    input_dir, output_dir, work = Path(input_dir), Path(output_dir), Path(work)
    cache_dir = Path(cache_dir)
    if species_in_proposals is None:
        species_in_proposals = config.SPECIES_IN_PROPOSALS
    if review_support_only is None:
        review_support_only = config.REVIEW_SUPPORT_ONLY
    if output_dir.resolve() == input_dir.resolve():
        raise ApplyError(f"output dir {output_dir} is the input dir; choose another")
    for name in (graphio.NODES, graphio.EDGES):
        if not graphio.graph_file(input_dir, name).exists():
            raise ApplyError(f"{input_dir / name} not found")
    run = resolve_run(work, model, prompt_version, reviewer, review_prompt_version)
    loaded = _load(input_dir, cache_dir, work, run)
    refined = refine_pairs(loaded, require_review, species_in_proposals, review_support_only)
    scope = {
        s.statement_id
        for s in records.read_records(work / records.STATEMENTS, records.SelectedStatement)
    }
    held = {*refined.held, *loaded.not_looked_up}
    graph = _Pass(refined, scope, set(loaded.unlabeled), held)

    output_dir.mkdir(parents=True, exist_ok=True)
    targets = [output_dir / graphio.NODES, output_dir / graphio.EDGES, work / REFINEMENTS]
    temps = [t.with_name(f".{t.name}.tmp") for t in targets]
    nodes_in = graphio.graph_file(input_dir, graphio.NODES)
    edges_in = graphio.graph_file(input_dir, graphio.EDGES)
    try:
        nodes = graphio.write_jsonl(temps[0], graphio.read_jsonl(nodes_in), compact=True)
        edges = graphio.write_jsonl(
            temps[1],
            itertools.chain(graph.originals(graphio.read_jsonl(edges_in), edges_in), graph.added()),
            compact=True,
        )
        lines = graphio.write_jsonl(temps[2], map(pair_record, refined.pairs), compact=True)
        for temp, target in zip(temps, targets, strict=True):
            os.replace(temp, target)
    finally:
        for temp in temps:
            temp.unlink(missing_ok=True)

    stats = graph.stats
    report = {
        "run": run.model_dump()
        | {
            "require_review": require_review,
            "species_in_proposals": species_in_proposals,
            "review_support_only": review_support_only,
        },
        "input": str(input_dir),
        "output": str(output_dir),
        "uberon_version": loaded.uberon_version,
        "nodes": nodes,
        **{k: stats[k] for k in ("edges_read", "ids_assigned", "earlier_edges_dropped")},
        "pairs_refined": len(refined.pairs),
        **{
            k: stats[k]
            for k in ("edges_checked", "edges_supported", "edges_unsupported", "edges_refined")
        },
        "publications_added": stats["publications_added"],
        "proposed": {
            "edges": sum(len(r.refinement.proposed) for r in refined.pairs),
            "links": sum(len(r.refinement.links) for r in refined.pairs),
            "species_folded": refined.folded,
        },
        "held": {
            "pairs_unreviewed": len(refined.held),
            "pairs_anatomy_not_looked_up": len(loaded.not_looked_up),
            "edges": graph.unchecked["held"],
        },
        "unchecked": {
            "edges_not_fully_labeled": graph.unchecked["not_fully_labeled"],
            "edges_not_searched": graph.unchecked["not_searched"],
            "pairs_not_fully_labeled": len(loaded.unlabeled),
        },
        "claims": _claims(loaded, refined),
        "skipped": dict(sorted(loaded.counts.items())),
        "refinements": lines,
        "edges_written": edges,
        "invalid_edges": graph.invalid.report(),
    }
    records.write_json(work / APPLY_REPORT, report)
    _warn(report, run)
    warning = graphio.invalid_warning(
        "apply", report["invalid_edges"], edges_in, "written as read, unchecked"
    )
    if warning:
        print(warning, file=sys.stderr)
    return report


def summary(report: dict[str, Any]) -> list[str]:
    run, unchecked, held = report["run"], report["unchecked"], report["held"]
    review_ = (
        f"reviewer={run['reviewer']} review_prompt={run['review_prompt_version']}"
        if run["reviewer"]
        else "no review"
    )
    proposed, claims = report["proposed"], report["claims"]
    species = (
        "kept"
        if run["species_in_proposals"]
        else f"taken out, {proposed['species_folded']} species-qualified proposals folded "
        "(--species-in-proposals keeps them)"
    )
    lines = [
        f"run model={run['model']} prompt={run['prompt_version']} {review_}",
        f"read {report['edges_read']} edges ({report['ids_assigned']} given an id), dropping "
        f"the {report['earlier_edges_dropped']} an earlier run added; refined "
        f"{report['pairs_refined']} pairs",
        f"checked {report['edges_checked']} edges: {report['edges_supported']} supported, "
        f"{report['edges_unsupported']} unsupported, {report['edges_refined']} refined by more "
        f"specific edges; {report['publications_added']} PMIDs added to their publications",
        f"proposed {proposed['edges']} new edges and {proposed['links']} links to edges that "
        f"already say it; species in proposals: {species}",
        "support-only statements (config.SUPPORT_ONLY): never proposed, "
        + (
            "counted once upheld"
            if run["review_support_only"]
            else "counted unreviewed (--review-support-only requires review)"
        ),
        f"held unchanged: {held['edges']} edges in {held['pairs_unreviewed']} pairs with claims "
        f"no review has judged and {held['pairs_anatomy_not_looked_up']} with anatomy the ground "
        "step never looked up",
        f"in scope but unchecked: {unchecked['edges_not_fully_labeled']} edges in "
        f"{unchecked['pairs_not_fully_labeled']} pairs not fully labeled, "
        f"{unchecked['edges_not_searched']} in pairs not searched",
        f"claims: {claims['used']} ({claims['counting']} counting; {claims['upheld']} upheld, "
        f"{claims['rejected']} rejected, {claims['unreviewed']} unreviewed), "
        f"{claims['sentences_repaired']} with a repaired sentence; UBERON "
        f"{report['uberon_version']}",
        f"wrote {report['nodes']} nodes and {report['edges_written']} edges to "
        f"{report['output']}, and {report['refinements']} pair refinements",
    ]
    if not run["require_review"]:
        lines.insert(1, "--no-review: unreviewed claims counted, for testing only")
    return [f"apply: {line}" for line in lines]


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--no-review",
        action="store_true",
        help="for testing only: count the claims no review has judged, instead of holding "
        "their pairs (a rejected claim still doesn't count)",
    )
    parser.add_argument(
        "--species-in-proposals",
        action=argparse.BooleanOptionalAction,
        default=config.SPECIES_IN_PROPOSALS,
        help="keep the species in proposed edges, such as 'E in human'; without it, the species "
        "is taken out and the proposals that become the same are merged (the originals' support "
        "is the same either way)",
    )
    parser.add_argument(
        "--review-support-only",
        action=argparse.BooleanOptionalAction,
        default=config.REVIEW_SUPPORT_ONLY,
        help="count a statement that supports an edge only through a narrower predicate "
        "config.SUPPORT_ONLY lists under its own ('ameliorates' under 'treats') only once "
        "review upheld its claim; without it, it counts unreviewed (give review the same flag)",
    )
    parser.add_argument("--model", help="bulk model of the run (default: from label_run.json)")
    parser.add_argument(
        "--prompt-version", help="label prompt version (default: from label_run.json)"
    )
    parser.add_argument("--reviewer", help="review model (default: from label_run.json)")
    parser.add_argument(
        "--review-prompt-version", help="review prompt version (default: from label_run.json)"
    )


def run(args: argparse.Namespace) -> int:
    try:
        report = apply_claims(
            args.input,
            args.output,
            args.cache_dir,
            args.work,
            require_review=not args.no_review,
            species_in_proposals=args.species_in_proposals,
            review_support_only=args.review_support_only,
            model=args.model,
            prompt_version=args.prompt_version,
            reviewer=args.reviewer,
            review_prompt_version=args.review_prompt_version,
        )
    except ApplyError as e:
        print(f"apply: {e}", file=sys.stderr)
        return 1
    print("\n".join(summary(report)))
    return 0
