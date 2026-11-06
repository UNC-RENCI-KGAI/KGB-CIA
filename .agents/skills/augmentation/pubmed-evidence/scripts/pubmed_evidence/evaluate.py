"""Choosing the models: a run's claims, and the refinements they lead to, scored against a
curator's gold set.

The gold set is a curator's claims for some abstracts of some pairs, and the edges of those
pairs to score. This step reads it and the caches and calls no model, so comparing models and
prompt versions costs nothing once each has labeled the gold pairs. Step 6's refinement
(`refine.refine_pair`, by the rules DESIGN.md's step 6 gives, cited here by their numbers) runs
on both sides: on the curator's claims, the gold side, and on each run's checked claims. It
scores four things:

- **Claims.** Each gold abstract's claims against the run's: precision, recall and F1, each
  claim matching at most one on the other side. Exact: the same polarity and statement. Up to
  an ancestor: the same polarity, and the run's statement the curator's or one it entails (the
  run stopped at `affects` where the curator has `causes decreased activity`). Scored on the
  claims as the run answered them, and as checked: without those that failed a check or whose
  evidence sentence wasn't found, even after the repair pass, which count for nothing in step 6.
- **Support.** Whether each gold edge is supported on the gold claims and on the run's:
  accuracy, Cohen's kappa, the confusion matrix, and precision, recall and F1 of each.
- **PMIDs.** Each edge's supporting PMIDs on the run's claims against those on the gold claims:
  micro precision, recall and F1 over the edges.
- **Proposals.** The statements each side proposes for a pair, and those it links to an edge of
  the pair that already makes them: micro precision, recall and F1 over the pairs, matched by
  statement key (`refine.statement_key`).

Both sides read the same abstracts: a pair is refined on its gold abstracts, and only where the
run answered every one of them. Its edges are the pair's gold edges, all in scope; the gold set
names no others. A claim counts as step 6 counts it (rules 1 and 8 to 10): its quote passed
the check, its paper isn't retracted (PubMed's publication types, from the PubMed cache), and it
isn't a hypothesis. No review verdict is read, on either side (`require_review=False`): this
scores the extractor. The species is taken out of both sides' proposals, as step 6 does, unless
config.SPECIES_IN_PROPOSALS or `--species-in-proposals` keeps it (`refine.without_species`,
rule 7). Every statement is read the way round Biolink's domain and range put it
(`refine.directed`), the curator's as the run's, with the nodes' categories from select's
`entities.jsonl`. Claims are read as step 6 reads them (`bundle.claim_record`), anatomy names
grounded as the ground step grounds them. The ground step grounds only the names of the run
`label_run.json` names, so a name its cache doesn't have (the curator's, another run's) is
grounded here the same way, through the Name Resolver and NodeNorm, and cached; the report
counts the names grounded, those not, and those whose lookup failed (a rerun retries them).
An abstract a fallback model answered (a Claude model's server-side fallback, the answer's
`answered_by`) is counted apart and left out of the scores, which are the requested model's
own, and so is every edge of its pair. Cost comes from the calls the label cache logged, so it
is what the run really cost per pair rather than an estimate.

The gold file is JSONL, with two kinds of line; other keys are ignored:

- `{"pair_id": "CHEBI:45783|MONDO:0011996", "pmid": "12345", "gold_claims": [...]}`: the
  curator's claims for one abstract of the pair, each in the shape the label step stores
  (`label.stored_claim`), and [] for an abstract that states nothing about the pair. The anatomy
  may be a name, grounded as the run's are, or an UBERON CURIE. A `label_sample.jsonl` line
  becomes one by adding `gold_claims`. A line whose `gold_claims` is null or missing is skipped
  as not yet curated.
- `{"edge": {...}}`: an edge to score, as the graph's `edges.jsonl` has it. Its expected outcome
  is refinement's on the gold claims, so the line needs no label.

**Drawing the pairs** (`--draw N`). The gold pairs are drawn from step 6's `refinements.jsonl`,
stratified by each edge's outcome (`OUTCOMES`) and predicate family (the scope rule that kept
it), so the rarer outcomes are in the set: each stratum gets about as many pairs as the others
(`draw`). A pair with no abstracts read is left out, and counted: a curator would have nothing
to read for it, and `label --pairs` nothing to label. They go to `gold_pairs.jsonl`, and a
`label --pairs FILE` run with each candidate model labels exactly those pairs and writes them,
with the model's claims, to `label_sample.jsonl`, whose lines a curator turns into gold lines.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from pubmed_evidence import (
    bundle,
    config,
    graphio,
    ground,
    pubmed,
    records,
    refine,
    repair,
    scope,
    sentences,
    uberon,
)
from pubmed_evidence.cache import AbstractClaimsRow, Grounding, LabelCache, PubMedCache, claim_id
from pubmed_evidence.label import rules_of
from shared.schema import ClaimRecord, Edge, Statement

GOLD = "gold.jsonl"
REPORT = "evaluate_report.md"
# The pairs `--draw` picks for a curator, which `label --pairs` labels.
GOLD_PAIRS = "gold_pairs.jsonl"
PAIRS_SCALE = 10_000
# Whether an edge is supported, on the gold claims and on the run's.
SUPPORT = ("supported", "unsupported")
# An edge's outcome, which `--draw` stratifies by: unsupported; supported, with nothing more
# specific; or refined: supported, with proposals or links (`lit_refined_by`).
OUTCOMES = ("unsupported", "supported", "refined")
# What the report counts of the anatomy names a side gives (`ground.ground_anatomy`'s counts).
ANATOMY_COUNTS = ("names", "grounded", "ungrounded", "failed")


class EvaluateError(RuntimeError):
    """A missing or empty input that stops the evaluation."""


# --- The gold set ---------------------------------------------------------------------------

# An abstract of a pair: (pair_id, PMID as bare digits).
AbstractKey = tuple[str, str]


@dataclass(frozen=True)
class GoldAbstract:
    """The curator's answer for one abstract of a pair: its claims, each in the shape the label
    step stores."""

    pair_id: str
    pmid: str
    claims: tuple[dict[str, Any], ...]


@dataclass
class Gold:
    path: Path | None
    abstracts: dict[AbstractKey, GoldAbstract] = field(default_factory=dict)
    edges: dict[str, Edge] = field(default_factory=dict)  # by edge id, each with its `id`
    unlabeled: int = 0  # abstract lines whose gold_claims is null: not yet curated
    invalid: int = 0  # lines that can't be read
    duplicates: int = 0  # a repeated abstract or edge; the last line wins

    @property
    def claims(self) -> int:
        return sum(len(a.claims) for a in self.abstracts.values())

    def by_pair(self) -> dict[str, list[str]]:
        """Each pair's gold PMIDs, in order."""
        out: dict[str, list[str]] = defaultdict(list)
        for pid, pmid in self.abstracts:
            out[pid].append(pmid)
        return {pid: sorted(pmids, key=graphio.pmid_key) for pid, pmids in sorted(out.items())}

    def edges_by_pair(self) -> dict[str, list[Edge]]:
        """Each pair's gold edges, in the order the gold file first gives them."""
        out: dict[str, list[Edge]] = defaultdict(list)
        for e in self.edges.values():
            out[graphio.pair_id(e.subject, e.object)].append(e)
        return dict(out)


def _validation(e: ValidationError) -> str:
    problem = e.errors()[0]
    return f"{'.'.join(map(str, problem['loc'])) or 'record'}: {problem['msg']}"


def _claim_problem(
    claim: Any, nodes: set[str], categories: Mapping[str, Sequence[str]] | None
) -> str | None:
    """Why a gold claim can't be read, or None. With the nodes' categories, a claim whose
    predicate fits them neither way round can't."""
    if not isinstance(claim, dict):
        return "not a JSON object"
    try:
        record = bundle.claim_record(claim, sentence_found=True, categories=categories)
    except ValidationError as e:
        return _validation(e)
    except KeyError as e:
        return f"no {e.args[0]}"
    except (AttributeError, TypeError, ValueError) as e:
        return str(e)
    for s in (record.statement, *record.also_supports):
        if {s.subject, s.object} != nodes:
            return f"a statement from {s.subject} to {s.object} isn't about the pair"
    return None


def _abstract_line(
    gold: Gold, line: dict[str, Any], categories: Mapping[str, Sequence[str]] | None
) -> str | None:
    claims = line.get("gold_claims")
    if claims is None:
        gold.unlabeled += 1
        return None
    pair_id, pmid = line.get("pair_id"), graphio.parse_pmid(line.get("pmid"))
    if not isinstance(pair_id, str) or pair_id.count("|") != 1 or pmid is None:
        return "an abstract needs a pair_id ('a|b') and a pmid"
    if not isinstance(claims, list):
        return "gold_claims is not a list"
    pair_id = graphio.pair_id(*pair_id.split("|"))
    nodes = set(graphio.pair_nodes(pair_id))
    for i, claim in enumerate(claims):
        problem = _claim_problem(claim, nodes, categories)
        if problem:
            return f"gold_claims[{i}]: {problem}"
    key = (pair_id, pmid)
    gold.duplicates += key in gold.abstracts
    gold.abstracts[key] = GoldAbstract(pair_id, pmid, tuple(claims))
    return None


def _edge_line(gold: Gold, line: dict[str, Any]) -> str | None:
    row = line["edge"]
    if not isinstance(row, dict):
        return "edge is not a JSON object"
    try:
        edge = Edge.model_validate({**row, "id": graphio.edge_id(row)})
    except ValidationError as e:
        return f"edge is not a valid Edge: {_validation(e)}"
    gold.duplicates += edge.id in gold.edges
    gold.edges[edge.id] = edge
    return None


def load_gold(path: Path, categories: Mapping[str, Sequence[str]] | None = None) -> Gold:
    """The gold set in a gold file. A line that can't be read is skipped with a warning; with
    the nodes' `categories`, so is an abstract with a claim whose predicate fits its nodes
    neither way round."""
    path = Path(path)
    if not path.exists():
        raise EvaluateError(f"no gold file {path}")
    gold = Gold(path)
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            line = json.loads(raw)
        except json.JSONDecodeError:
            line = None
        if not isinstance(line, dict):
            problem = "not a JSON object"
        elif "edge" in line:
            problem = _edge_line(gold, line)
        else:
            problem = _abstract_line(gold, line, categories)
        if problem:
            print(f"gold: {path}:{n}: {problem}; skipped", file=sys.stderr)
            gold.invalid += 1
    return gold


def parse_run(text: str) -> tuple[str, str | None]:
    """'MODEL' or 'MODEL@PROMPT_VERSION'. Prompt versions contain '@' themselves
    ('label@1a2b3c4d'), so the model is everything before the first '@'."""
    model, sep, version = text.partition("@")
    return model, (version if sep and version else None)


# --- Drawing the pairs to curate ------------------------------------------------------------


def outcome(supported: bool, refined_by: Collection[str]) -> str:
    """An edge's outcome (`OUTCOMES`): unsupported; supported, with nothing more specific; or
    refined: supported, with proposals or links."""
    if not supported:
        return "unsupported"
    return "refined" if refined_by else "supported"


def family_of(
    statement: Mapping[str, Any],
    rules: Sequence[scope.Rule],
    categories: Mapping[str, Sequence[str]],
) -> str:
    """A refined edge's predicate family: the id of the scope rule that kept it, matched as
    select matches it on its nodes' categories; its predicate if none matches."""
    subject, object = statement["subject"], statement["object"]
    rule = scope.match_rule(
        rules,
        tuple(categories.get(subject) or ()),
        statement["predicate"],
        tuple(categories.get(object) or ()),
    )
    return rule.id if rule is not None else statement["predicate"]


def draw(
    refinements: Iterable[Mapping[str, Any]],
    rules: Sequence[scope.Rule],
    categories: Mapping[str, Sequence[str]],
    n: int,
    seed: int = 0,
) -> list[dict[str, Any]]:
    """N pairs from step 6's refinement records (`refinements.jsonl`'s lines), stratified by
    outcome and predicate family, as the lines of a pairs file. A stratum is an (outcome,
    family) of the refined edges, and a pair is in the strata of each of its edges. The strata
    take turns, the smallest first, each drawing a pair it has that isn't drawn yet, in an order
    shuffled by `seed`, until N are drawn or none is left: each stratum gets about as many pairs
    as the others, or all it has, so the rarer outcomes come up. A pair with no abstracts read
    (`abstracts_read` 0) is left out: its edges are unsupported for want of anything to read,
    and a curator would have nothing to read either. A line names the pair, the stratum it was
    drawn for, and its edges' outcomes."""
    by_stratum: dict[tuple[str, str], set[str]] = defaultdict(set)
    edges: dict[str, dict[str, str]] = defaultdict(dict)
    for line in refinements:
        if not line["abstracts_read"]:
            continue
        pid = line["pair_id"]
        for e in line["edges"]:
            o = outcome(e["supported"], e["refined_by"])
            by_stratum[(o, family_of(e["statement"], rules, categories))].add(pid)
            edges[pid][e["edge_id"]] = o
    rng = random.Random(seed)
    queues = {}
    for stratum, pairs in sorted(by_stratum.items()):
        queue = sorted(pairs)
        rng.shuffle(queue)
        queues[stratum] = queue
    drawn: dict[str, tuple[str, str]] = {}
    turns = sorted(queues, key=lambda s: (len(queues[s]), s))
    while len(drawn) < n and any(queues.values()):
        for stratum in turns:
            queue = queues[stratum]
            while queue and queue[-1] in drawn:
                queue.pop()
            if queue and len(drawn) < n:
                drawn[queue.pop()] = stratum
    return [
        {"pair_id": pid, "outcome": o, "family": family, "edges": edges[pid]}
        for pid, (o, family) in drawn.items()
    ]


def draw_pairs(work: Path, n: int, seed: int = 0) -> tuple[Path, list[dict[str, Any]], int]:
    """Draw N pairs from `<work>/refinements.jsonl` (`draw`) into `<work>/gold_pairs.jsonl`,
    with the scope rules select used and the nodes' categories from `entities.jsonl`. Returns
    the file, its lines, and the number of refined pairs left out for having no abstracts.
    Raises EvaluateError for a missing input or N below 1."""
    if n < 1:
        raise EvaluateError(f"--draw needs a number of pairs above 0, not {n}")
    path = Path(work) / records.REFINEMENTS
    if not path.exists():
        raise EvaluateError(f"no {path}; run `kgb-augment apply` first")
    refined = list(graphio.read_jsonl(path))
    lines = draw(refined, rules_of(work), categories_of(work), n, seed)
    out = Path(work) / GOLD_PAIRS
    graphio.write_jsonl(out, lines)
    return out, lines, sum(not line["abstracts_read"] for line in refined)


def categories_of(work: Path) -> dict[str, list[str]]:
    """Node id -> Biolink categories, from select's `entities.jsonl`; none, with a warning,
    without it."""
    path = Path(work) / records.ENTITIES
    if not path.exists():
        print(
            f"evaluate: warning: no {path}, so statements are read as written rather than the "
            "way round Biolink reads them, and proposals aren't checked against Biolink; run "
            "`kgb-augment select` first",
            file=sys.stderr,
        )
        return {}
    return {e.id: list(e.category) for e in records.read_records(path, records.Entity)}


# --- Scoring --------------------------------------------------------------------------------

# A claim as it's matched: its polarity and its statement, read the way round Biolink reads it.
# A symmetric statement matches written either way round (`refine.readings`): which node the
# curator or the model put first says nothing.
Key = tuple[str, Statement]
Same = Callable[[Key, Key], bool]


def exact(gold: Key, found: Key) -> bool:
    return gold[0] == found[0] and gold[1] in refine.readings(found[1])


def up_to_ancestor(lattice: refine.Lattice) -> Same:
    """A match when the found claim has the gold claim's polarity, and its statement, either
    way round if it's symmetric, is the gold claim's or one that statement entails."""

    def same(gold: Key, found: Key) -> bool:
        return gold[0] == found[0] and any(
            lattice.entails(gold[1], s) for s in refine.readings(found[1])
        )

    return same


def matched(gold: Sequence[Key], found: Sequence[Key], same: Same) -> int:
    """The most found claims that match gold claims one to one, `same(gold claim, found claim)`
    saying which may: a maximum bipartite matching, by augmenting paths."""
    owner: dict[int, int] = {}  # found claim -> the gold claim it's matched to

    def assign(g: int, seen: set[int]) -> bool:
        for f, claim in enumerate(found):
            if f in seen or not same(gold[g], claim):
                continue
            seen.add(f)
            if f not in owner or assign(owner[f], seen):
                owner[f] = g
                return True
        return False

    return sum(assign(g, set()) for g in range(len(gold)))


def _scores(gold: int, found: int, hits: int) -> dict[str, Any]:
    return {
        "gold": gold,
        "found": found,
        "matched": hits,
        "precision": hits / found if found else None,
        "recall": hits / gold if gold else None,
        # 2PR / (P + R), which is 0 rather than undefined when nothing matched.
        "f1": 2 * hits / (gold + found) if gold + found else None,
    }


def claim_scores(abstracts: Iterable[tuple[Sequence[Key], Sequence[Key]]], same: Same) -> dict:
    """Precision, recall and F1 of the found claims against the gold claims, over abstracts
    given as (gold claims, found claims)."""
    gold = found = hits = 0
    for g, f in abstracts:
        gold += len(g)
        found += len(f)
        hits += matched(g, f, same)
    return _scores(gold, found, hits)


def overlap(groups: Iterable[tuple[Collection[str], Collection[str]]]) -> dict[str, Any]:
    """Micro precision, recall and F1 of the found items against the gold ones, over groups
    given as (gold items, found items): an edge's PMIDs, or a pair's proposal keys."""
    gold = found = hits = 0
    for g, f in groups:
        g, f = set(g), set(f)
        gold += len(g)
        found += len(f)
        hits += len(g & f)
    return _scores(gold, found, hits)


def metrics(pairs: Sequence[tuple[str, str]], labels: Sequence[str] = SUPPORT) -> dict[str, Any]:
    """Accuracy, Cohen's kappa, the confusion matrix (rows the first of each pair, columns the
    second, in `labels` order), per-label precision, recall and F1 of the second against the
    first, and macro F1."""
    index = {label: i for i, label in enumerate(labels)}
    confusion = [[0] * len(labels) for _ in labels]
    for gold, label in pairs:
        confusion[index[gold]][index[label]] += 1
    n = len(pairs)
    gold_totals = [sum(row) for row in confusion]
    label_totals = [sum(row[j] for row in confusion) for j in range(len(labels))]
    correct = sum(confusion[i][i] for i in range(len(labels)))
    accuracy = correct / n if n else None
    kappa = None
    if n:
        expected = sum(g * p for g, p in zip(gold_totals, label_totals, strict=True)) / n**2
        if expected < 1:
            kappa = (correct / n - expected) / (1 - expected)
    per_label = {}
    for i, label in enumerate(labels):
        tp, gold, labeled = confusion[i][i], gold_totals[i], label_totals[i]
        per_label[label] = {
            "precision": tp / labeled if labeled else None,
            "recall": tp / gold if gold else None,
            "f1": 2 * tp / (gold + labeled) if gold + labeled else None,
            "gold": gold,
            "labeled": labeled,
        }
    f1s = [v["f1"] for v in per_label.values() if v["f1"] is not None]
    return {
        "n": n,
        "correct": correct,
        "accuracy": accuracy,
        "kappa": kappa,
        "confusion": confusion,
        "per_label": per_label,
        "macro_f1": sum(f1s) / len(f1s) if f1s else None,
    }


def cost_summary(usage: dict[str, Any]) -> dict[str, Any]:
    """Cost per successful call, per pair and per 10,000 pairs of a run's logged calls; None
    where the cost or the denominator is unknown."""
    calls = (usage.get("calls") or 0) - (usage.get("errors") or 0)
    pairs = usage.get("pairs") or 0
    cost = usage.get("cost")
    return {
        "cost_per_call": cost / calls if cost is not None and calls else None,
        "cost_per_pair": cost / pairs if cost is not None and pairs else None,
        "cost_per_10k_pairs": cost / pairs * PAIRS_SCALE if cost is not None and pairs else None,
        "input_tokens_per_pair": usage["input_tokens"] / pairs if pairs else None,
        "output_tokens_per_pair": usage["output_tokens"] / pairs if pairs else None,
    }


# --- Claims and refinements -----------------------------------------------------------------


@dataclass
class _Inputs:
    """What both sides are read with: the PubMed cache (publication types, groundings), the
    nodes' categories, the lattice, the gold abstracts' publication types, and whether
    proposals keep their species."""

    pubmed: PubMedCache
    categories: dict[str, list[str]]
    lattice: refine.Lattice
    articles: dict[str, tuple[int | None, list[str] | None]]
    species_in_proposals: bool
    lookup: ground.Lookup | None = None  # the Name Resolver's bulk lookup; tests fake it
    nodenorm: Callable[[list[str]], dict] | None = None

    def grounded(
        self, claims: Iterable[dict[str, Any]]
    ) -> tuple[dict[str, Grounding], dict[str, int]]:
        """Each anatomy name's grounding, by the name as given, those the cache lacks grounded
        first as the ground step grounds them (and cached); with ANATOMY_COUNTS. A name whose
        lookup fails stays ungrounded, and the next run retries it. An UBERON CURIE isn't
        looked up: a gold claim may give one."""
        names = {
            n for c in claims if (n := ground.claim_anatomy(c)) and not n.startswith(ground.UBERON)
        }
        s = ground.ground_anatomy(names, self.pubmed, lookup=self.lookup, nodenorm=self.nodenorm)
        return ground.groundings(self.pubmed, names), {k: s[k] for k in ANATOMY_COUNTS}

    def claims(self, pmid: str, records_: Iterable[ClaimRecord]) -> list[refine.Claim]:
        """An abstract's claims as refinement reads them: retracted when PubMed's publication
        types mark the paper so (rule 9)."""
        types = (self.articles.get(pmid) or (None, None))[1] or []
        _, retracted = pubmed.article_type(types)
        return [refine.Claim(graphio.publication_of(pmid), r, retracted) for r in records_]

    def refine_pairs(
        self, gold: Gold, claims: Mapping[str, list[refine.Claim]]
    ) -> dict[str, refine.PairRefinement]:
        """The refinement of each pair with claims here and gold edges, by pair id: its gold
        edges, all in scope, refined on the claims, every one counting that isn't rejected
        (`require_review=False`), with the species taken out of the proposals unless
        `species_in_proposals` keeps it, as in step 6."""
        edges = gold.edges_by_pair()
        out = {}
        for pid, pair_claims in claims.items():
            if pid not in edges:
                continue
            r = refine.refine_pair(
                edges[pid], pair_claims, self.lattice, self.categories, require_review=False
            )
            out[pid] = r if self.species_in_proposals else refine.without_species(r)
        return out


def _key(record: ClaimRecord) -> Key:
    return record.polarity, record.statement


def _gold_records(gold: Gold, inputs: _Inputs) -> dict[AbstractKey, list[ClaimRecord]]:
    """The curator's claims as records, read the way round Biolink reads them. An anatomy given
    as an UBERON CURIE is taken as grounded; a name is grounded as the run's are."""
    grounded, _ = inputs.grounded(c for a in gold.abstracts.values() for c in a.claims)
    out = {}
    for key, a in gold.abstracts.items():
        out[key] = []
        for claim in a.claims:
            name = ground.claim_anatomy(claim)
            if name is not None and name.startswith(ground.UBERON):
                grounding = Grounding(name, "anatomy", name)
            else:
                grounding = grounded.get(name) if name else None
            try:
                record = bundle.claim_record(
                    claim, sentence_found=True, grounding=grounding, categories=inputs.categories
                )
            except ValueError as e:
                raise EvaluateError(
                    f"gold claim of {key[0]} PMID {key[1]}: {e}; load the gold set with the "
                    "nodes' categories"
                ) from e
            out[key].append(record)
    return out


@dataclass
class _Answers:
    """One run's answers for the gold abstracts: those its own model gave, with each claim as
    answered and, if it passed its checks, as step 6 reads it."""

    own: dict[AbstractKey, AbstractClaimsRow] = field(default_factory=dict)
    fallback: set[AbstractKey] = field(default_factory=set)  # a fallback model answered
    answered: dict[AbstractKey, list[Key]] = field(default_factory=dict)
    records: dict[AbstractKey, list[ClaimRecord]] = field(default_factory=dict)
    counts: Counter = field(default_factory=Counter)
    anatomy: dict[str, int] = field(default_factory=dict)  # ANATOMY_COUNTS of its claims

    def checked(self, key: AbstractKey) -> list[Key]:
        return [_key(r) for r in self.records[key] if r.evidence_sentence_valid]


def _as_answered(stored: Mapping[str, Any], categories: Mapping[str, Sequence[str]]) -> Key:
    """A claim as the run answered it, its statement read the way round Biolink reads it where
    its predicate fits either way."""
    statement = Statement.model_validate(stored["statement"])
    return stored["polarity"], refine.directed(statement, categories) or statement


def _answers(
    labels: LabelCache,
    gold: Gold,
    inputs: _Inputs,
    model: str,
    version: str,
    repairs_by: tuple[str | None, str | None],
) -> _Answers:
    pair_ids = sorted({pid for pid, _ in gold.abstracts})
    out = _Answers()
    for a in labels.abstract_claims(version, model, pair_ids):
        key = (a.pair_id, a.pmid)
        if key not in gold.abstracts:
            continue
        if a.answered_by in (None, model):
            out.own[key] = a
        else:
            out.fallback.add(key)
    checks = {
        (c.pair_id, c.pmid, c.claim_id): c for c in labels.claim_checks(version, model, pair_ids)
    }
    repairs = repair.sentence_repairs(labels, version, model, *repairs_by, pair_ids)
    grounded, out.anatomy = inputs.grounded(c for a in out.own.values() for c in a.claims)
    counts = out.counts
    for key, a in sorted(out.own.items()):
        out.answered[key] = [_as_answered(c, inputs.categories) for c in a.claims]
        out.records[key] = []
        for stored in a.claims:
            counts["claims"] += 1
            ckey = (*key, claim_id(stored))
            check = checks.get(ckey)
            if check is None or not check.valid:
                counts["failing_checks"] += 1
                continue
            name = ground.claim_anatomy(stored)
            try:
                record = bundle.claim_record(
                    stored,
                    sentence_found=check.sentence_valid,
                    repaired=repairs.get(ckey),
                    grounding=grounded.get(name) if name else None,
                    answered_by=a.answered_by,
                    categories=inputs.categories,
                )
            except ValueError:  # a pydantic ValidationError too
                counts["failing_checks"] += 1
                continue
            counts["sentences_not_found"] += not record.evidence_sentence_valid
            counts["sentences_repaired"] += record.evidence_sentence_repaired
            out.records[key].append(record)
    return out


def _text(statement: Statement) -> str:
    """A statement in English, its nodes by id."""
    return sentences.sentence(statement, {})


def _supported(o: refine.EdgeOutcome) -> str:
    return "supported" if o.supported else "unsupported"


def _proposals(r: refine.PairRefinement) -> dict[str, Statement]:
    """A pair's proposals and links, by statement key."""
    return {p.key: p.statement for p in (*r.proposed, *r.links)}


def _gold_side(
    gold: Gold, inputs: _Inputs
) -> tuple[dict[AbstractKey, list[Key]], dict[str, refine.PairRefinement]]:
    """The curator's claims as matched, by abstract, and the refinement on them of each pair
    with gold abstracts and gold edges."""
    records_ = _gold_records(gold, inputs)
    claims = {
        pid: [c for pmid in pmids for c in inputs.claims(pmid, records_[(pid, pmid)])]
        for pid, pmids in gold.by_pair().items()
    }
    keys = {k: [_key(r) for r in rs] for k, rs in records_.items()}
    return keys, inputs.refine_pairs(gold, claims)


def _gold_outcomes(gold: Gold, refined: Mapping[str, refine.PairRefinement]) -> dict[str, Any]:
    """The gold side: each gold edge's outcome on its pair's gold claims, and each pair's
    proposals and links."""
    pairs = sorted(refined)
    edges = [(pid, o) for pid in pairs for o in refined[pid].edges]
    counts = Counter(outcome(o.supported, o.refined_by) for _, o in edges)
    return {
        "edges": len(gold.edges),
        "scored": len(edges),
        "without_abstracts": len(gold.edges) - len(edges),
        "outcomes": {k: counts[k] for k in OUTCOMES},
        "proposed": sum(len(refined[pid].proposed) for pid in pairs),
        "links": sum(len(refined[pid].links) for pid in pairs),
        "species_folded": sum(len(refined[pid].folded) for pid in pairs),
        "by_edge": [
            {
                "edge_id": o.edge_id,
                "pair_id": pid,
                "outcome": outcome(o.supported, o.refined_by),
                "pmids": list(o.pmids),
                "refined_by": list(o.refined_by),
            }
            for pid, o in edges
        ],
        "proposals": [
            {
                "pair_id": pid,
                "statement": _text(p.statement),
                "pmids": list(p.pmids),
                "original_edge_ids": list(p.original_edge_ids),
                "linked_to": list(p.existing),
            }
            for pid in pairs
            for p in sorted((*refined[pid].proposed, *refined[pid].links), key=lambda p: p.key)
        ],
    }


def _score_run(
    gold: Gold,
    answers: _Answers,
    gold_keys: Mapping[AbstractKey, list[Key]],
    gold_side: Mapping[str, refine.PairRefinement],
    inputs: _Inputs,
) -> dict[str, Any]:
    """One run's scores: its claims against the curator's, and the refinement on them against
    the refinement on the curator's."""
    covered = sorted(answers.own)
    ancestor = up_to_ancestor(inputs.lattice)

    def scores(found: Callable[[AbstractKey], list[Key]]) -> dict[str, Any]:
        abstracts = [(gold_keys[k], found(k)) for k in covered]
        return {
            "exact": claim_scores(abstracts, exact),
            "ancestor": claim_scores(abstracts, ancestor),
        }

    claims, fallback_pairs = {}, set()
    for pid, pmids in gold.by_pair().items():
        keys = [(pid, pmid) for pmid in pmids]
        if all(k in answers.own for k in keys):
            claims[pid] = [c for k in keys for c in inputs.claims(k[1], answers.records[k])]
        elif all(k in answers.own or k in answers.fallback for k in keys):
            fallback_pairs.add(pid)
    run_side = inputs.refine_pairs(gold, claims)
    pairs = [pid for pid in sorted(gold_side) if pid in run_side]
    rows = [
        (g, r)
        for pid in pairs
        for g, r in zip(gold_side[pid].edges, run_side[pid].edges, strict=True)
    ]
    proposals = {pid: (_proposals(gold_side[pid]), _proposals(run_side[pid])) for pid in pairs}
    return {
        "abstracts": len(gold.abstracts),
        "covered": len(covered),
        "coverage": len(covered) / len(gold.abstracts) if gold.abstracts else None,
        "fallback_served": len(answers.fallback),
        "claims": {
            k: answers.counts[k]
            for k in ("claims", "failing_checks", "sentences_not_found", "sentences_repaired")
        },
        "anatomy": answers.anatomy,
        "as_answered": scores(lambda k: answers.answered[k]),
        "as_checked": scores(answers.checked),
        "edges": sum(len(r.edges) for r in gold_side.values()),
        "edges_covered": len(rows),
        "edges_fallback_served": sum(
            len(gold_side[pid].edges) for pid in fallback_pairs if pid in gold_side
        ),
        "pairs": len(gold_side),
        "pairs_covered": len(pairs),
        "support": metrics([(_supported(g), _supported(r)) for g, r in rows]),
        "pmids": overlap((g.pmids, r.pmids) for g, r in rows),
        "proposals": overlap((g.keys(), r.keys()) for g, r in proposals.values()),
        "disagreements": {
            "edges": [
                {
                    "edge_id": g.edge_id,
                    "on_gold": _supported(g),
                    "on_run": _supported(r),
                    "missing": [p for p in g.pmids if p not in r.pmids],
                    "extra": [p for p in r.pmids if p not in g.pmids],
                }
                for g, r in rows
                if set(g.pmids) != set(r.pmids)
            ],
            "pairs": [
                {
                    "pair_id": pid,
                    "missing": sorted(_text(s) for k, s in g.items() if k not in r),
                    "extra": sorted(_text(s) for k, s in r.items() if k not in g),
                }
                for pid, (g, r) in proposals.items()
                if g.keys() != r.keys()
            ],
        },
    }


def _repairs_by(
    label_run: records.LabelRun | None, model: str, version: str
) -> tuple[str | None, str | None]:
    """(repairer, repair prompt version) whose sentence repairs count for a run: those
    `label_run.json` names, if it names this run, else those a label run makes now."""
    if label_run is not None and (label_run.model, label_run.prompt_version) == (model, version):
        return label_run.repairer, label_run.repair_prompt_version
    return repair.current()


def _lattice(cache_dir: Path) -> tuple[refine.Lattice, str | None]:
    path = cache_dir / config.UBERON_FILE
    if not path.exists():
        print(
            f"evaluate: warning: no {path}, so one anatomy term is below another only when "
            "they're equal; run `kgb-augment ground` to fetch it",
            file=sys.stderr,
        )
        return refine.Lattice(), None
    release = uberon.load(path)
    return refine.Lattice(anatomy=release.ancestors), release.version


def evaluate(
    gold: Path | Gold,
    cache_dir: Path,
    runs: Sequence[str | tuple[str, str | None]],
    label_run: records.LabelRun | None = None,
    categories: Mapping[str, Sequence[str]] | None = None,
    *,
    species_in_proposals: bool | None = None,
    lookup: ground.Lookup | None = None,
    nodenorm: Callable[[list[str]], dict] | None = None,
) -> dict[str, Any]:
    """Score each run against the gold set, read with the nodes' `categories`. A run is
    'MODEL', 'MODEL@PROMPT_VERSION' or a (model, prompt version or None) tuple; a model without
    a prompt version is scored once per prompt version it has answers for in the cache,
    skipping those with none for a gold abstract. A run given with its prompt version is always
    scored, even with no answers. Repaired sentences are those `label_run` names for its own
    run, and the current repair model's and prompt's for any other. `species_in_proposals`
    keeps the species in both sides' proposals (default config.SPECIES_IN_PROPOSALS). Anatomy
    names the PubMed cache hasn't grounded are grounded through `lookup` and `nodenorm` (by
    default the Name Resolver and NodeNorm), as the ground step grounds them."""
    if species_in_proposals is None:
        species_in_proposals = config.SPECIES_IN_PROPOSALS
    categories = {k: list(v) for k, v in (categories or {}).items()}
    gold = gold if isinstance(gold, Gold) else load_gold(gold, categories)
    if not gold.abstracts:
        raise EvaluateError(f"no gold claims in {gold.path}")
    if not runs:
        raise EvaluateError("no run to score; pass --run MODEL[@PROMPT_VERSION]")
    cache_dir = Path(cache_dir)
    label_path, pubmed_path = cache_dir / config.LABEL_CACHE, cache_dir / config.PUBMED_CACHE
    for path, step in ((label_path, "label"), (pubmed_path, "pubmed")):
        if not path.exists():
            raise EvaluateError(f"no {step} cache {path}; run `kgb-augment {step}` first")
    nodes = {n for pid, _ in gold.abstracts for n in graphio.pair_nodes(pid)}
    unknown = sorted(n for n in nodes if not categories.get(n))
    if categories and unknown:
        print(
            f"evaluate: warning: {len(unknown)} gold nodes have no categories in entities.jsonl "
            f"(e.g. {unknown[0]}); their statements are read as written",
            file=sys.stderr,
        )
    lattice, uberon_version = _lattice(cache_dir)
    results = []
    with LabelCache(label_path) as labels, PubMedCache(pubmed_path) as cache:
        articles = cache.articles(pmid for _, pmid in gold.abstracts)
        inputs = _Inputs(
            cache, categories, lattice, articles, species_in_proposals, lookup, nodenorm
        )
        _, anatomy = inputs.grounded(c for a in gold.abstracts.values() for c in a.claims)
        gold_keys, gold_side = _gold_side(gold, inputs)
        for spec in runs:
            model, version = parse_run(spec) if isinstance(spec, str) else spec
            versions = [version] if version else labels.claim_prompt_versions(model)
            for v in versions:
                repairs_by = _repairs_by(label_run, model, v)
                answers = _answers(labels, gold, inputs, model, v, repairs_by)
                if not version and not answers.own and not answers.fallback:
                    print(
                        f"evaluate: {model}@{v} has no answers for the gold abstracts; skipped",
                        file=sys.stderr,
                    )
                    continue
                result = _score_run(gold, answers, gold_keys, gold_side, inputs)
                usage = labels.usage("label", model, v)
                results.append(
                    {
                        "model": model,
                        "prompt_version": v,
                        "run": f"{model}@{v}",
                        "repairer": repairs_by[0],
                        "repair_prompt_version": repairs_by[1],
                    }
                    | result
                    | {"usage": usage}
                    | cost_summary(usage)
                )
            if not versions:
                print(f"evaluate: no answers by {model} in {label_path}", file=sys.stderr)
    return {
        "uberon_version": uberon_version,
        "anatomy": anatomy,
        "species_in_proposals": species_in_proposals,
        "gold": _gold_outcomes(gold, gold_side),
        "runs": results,
    }


# --- Report ---------------------------------------------------------------------------------


def _num(value: float | None, digits: int = 3) -> str:
    return "–" if value is None else f"{value:.{digits}f}"


def _tokens(value: float | None) -> str:
    return "–" if value is None else f"{value:,.0f}"


def _anatomy(counts: Mapping[str, int]) -> str:
    """Anatomy names' counts in words; empty for no names."""
    if not counts.get("names"):
        return ""
    text = (
        f"{counts['names']} anatomy names, {counts['grounded']} grounded to UBERON, "
        f"{counts['ungrounded']} not (they narrow no statement, and are context notes)"
    )
    if counts["failed"]:
        text += f", {counts['failed']} whose lookup failed (left ungrounded; rerun to retry)"
    return text


def _pct(value: float | None) -> str:
    return "–" if value is None else f"{value:.0%}"


def _money(value: float | None) -> str:
    return "–" if value is None else f"${value:,.2f}"


def _pr(s: dict[str, Any]) -> str:
    return f"{_num(s['precision'])} / {_num(s['recall'])}"


def _ids(ids: Sequence[str]) -> str:
    return ", ".join(f"`{i}`" for i in ids) or "–"


def _items(items: Sequence[str]) -> str:
    return "; ".join(items) or "–"


def _pair(pair_id: str) -> str:
    """A pair id in a table cell, its '|' escaped."""
    return "`" + pair_id.replace("|", "\\|") + "`"


def comparison_table(results: Sequence[dict[str, Any]]) -> list[str]:
    lines = [
        "| Run | Abstracts | Claims P / R, exact | Claims P / R, up to an ancestor | Edges "
        "| Support accuracy | Kappa | PMIDs P / R | Proposals P / R | Cost per 10k pairs |",
        "| --- |" + " --- |" * 9,
    ]
    for r in results:
        c, s = r["as_checked"], r["support"]
        lines.append(
            f"| `{r['run']}` | {r['covered']}/{r['abstracts']} | {_pr(c['exact'])} "
            f"| {_pr(c['ancestor'])} | {r['edges_covered']}/{r['edges']} "
            f"| {_num(s['accuracy'])} | {_num(s['kappa'])} | {_pr(r['pmids'])} "
            f"| {_pr(r['proposals'])} | {_money(r['cost_per_10k_pairs'])} |"
        )
    return lines


def _claims_table(r: dict[str, Any]) -> list[str]:
    lines = [
        "### Claims",
        "",
        "| Claims | Exact: precision | recall | F1 | Up to an ancestor: precision | recall | F1 "
        "| Run's | Gold |",
        "| --- |" + " --- |" * 8,
    ]
    for name, key in (("As answered", "as_answered"), ("As checked", "as_checked")):
        e, a = r[key]["exact"], r[key]["ancestor"]
        lines.append(
            f"| {name} | {_num(e['precision'])} | {_num(e['recall'])} | {_num(e['f1'])} "
            f"| {_num(a['precision'])} | {_num(a['recall'])} | {_num(a['f1'])} "
            f"| {e['found']} | {e['gold']} |"
        )
    return lines + [""]


def _support_table(m: dict[str, Any]) -> list[str]:
    """The support confusion matrix, the gold claims' by rows and the run's by columns, with its
    scores."""
    lines = [
        "### Support",
        "",
        f"Accuracy {_num(m['accuracy'])} ({m['correct']}/{m['n']}), Cohen's kappa "
        f"{_num(m['kappa'])}, macro F1 {_num(m['macro_f1'])}.",
        "",
        "| On the gold claims \\ on the run's | " + " | ".join(SUPPORT) + " |",
        "| --- |" + " --- |" * len(SUPPORT),
    ]
    for label, row in zip(SUPPORT, m["confusion"], strict=True):
        lines.append(f"| {label} | " + " | ".join(str(c) for c in row) + " |")
    lines += [
        "",
        "| Support | Precision | Recall | F1 | On the gold claims | On the run's |",
        "| --- |" + " --- |" * 5,
    ]
    for label, v in m["per_label"].items():
        if v["gold"] or v["labeled"]:
            lines.append(
                f"| {label} | {_num(v['precision'])} | {_num(v['recall'])} | {_num(v['f1'])} "
                f"| {v['gold']} | {v['labeled']} |"
            )
    return lines + [""]


def _overlap_table(r: dict[str, Any]) -> list[str]:
    lines = [
        "### PMIDs and proposals",
        "",
        "| Scored | Precision | Recall | F1 | Run's | Gold | Matched |",
        "| --- |" + " --- |" * 6,
    ]
    for name, key in (("Supporting PMIDs, by edge", "pmids"), ("Proposals, by pair", "proposals")):
        s = r[key]
        lines.append(
            f"| {name} | {_num(s['precision'])} | {_num(s['recall'])} | {_num(s['f1'])} "
            f"| {s['found']} | {s['gold']} | {s['matched']} |"
        )
    return lines + [""]


def _disagreements(d: dict[str, Any]) -> list[str]:
    if not d["edges"] and not d["pairs"]:
        return []
    lines = ["### Disagreements", ""]
    if d["edges"]:
        lines += [
            "| Edge | On the gold claims | On the run's | PMIDs missing | PMIDs extra |",
            "| --- |" + " --- |" * 4,
        ]
        for e in d["edges"]:
            lines.append(
                f"| `{e['edge_id']}` | {e['on_gold']} | {e['on_run']} | {_items(e['missing'])} "
                f"| {_items(e['extra'])} |"
            )
        lines.append("")
    if d["pairs"]:
        lines += ["| Pair | Statements missing | Statements extra |", "| --- | --- | --- |"]
        for p in d["pairs"]:
            missing, extra = _items(p["missing"]), _items(p["extra"])
            lines.append(f"| {_pair(p['pair_id'])} | {missing} | {extra} |")
        lines.append("")
    return lines


def _gold_section(g: dict[str, Any]) -> list[str]:
    o = g["outcomes"]
    lines = [
        "## The gold side: refinement on the curator's claims",
        "",
        f"{g['scored']} of {g['edges']} edges refined on their pair's gold claims"
        + (
            f"; {g['without_abstracts']} have no gold abstract for their pair and are left out"
            if g["without_abstracts"]
            else ""
        )
        + f". {o['supported'] + o['refined']} supported, {o['refined']} of them refined by more "
        f"specific edges, and {o['unsupported']} unsupported; {g['proposed']} statements "
        f"proposed and {g['links']} linked to an edge of their pair"
        + (
            f" ({g['species_folded']} species-qualified proposals folded)"
            if g["species_folded"]
            else ""
        )
        + ". Where a run's refinement differs from this, the run's claims are wrong, or the "
        "curator's.",
        "",
    ]
    if g["by_edge"]:
        lines += ["| Edge | Outcome | PMIDs | Refined by |", "| --- | --- | --- | --- |"]
        for e in g["by_edge"]:
            lines.append(
                f"| `{e['edge_id']}` | {e['outcome']} | {_items(e['pmids'])} "
                f"| {_ids(e['refined_by'])} |"
            )
        lines.append("")
    if g["proposals"]:
        lines += [
            "| Pair | Statement | PMIDs | Refines | Already an edge |",
            "| --- | --- | --- | --- | --- |",
        ]
        for p in g["proposals"]:
            lines.append(
                f"| {_pair(p['pair_id'])} | {p['statement']} | {_items(p['pmids'])} "
                f"| {_ids(p['original_edge_ids'])} | {_ids(p['linked_to'])} |"
            )
        lines.append("")
    return lines


def report(gold: Gold, result: dict[str, Any]) -> str:
    results = result["runs"]
    no_claim = sum(not a.claims for a in gold.abstracts.values())
    skipped = []
    if gold.unlabeled:
        skipped.append(f"{gold.unlabeled} not yet curated")
    if gold.invalid:
        skipped.append(f"{gold.invalid} invalid")
    if gold.duplicates:
        skipped.append(f"{gold.duplicates} duplicates (last kept)")
    species = (
        "kept"
        if result["species_in_proposals"]
        else "taken out, as step 6 does by default (--species-in-proposals keeps it)"
    )
    lines = [
        "# Gold-set evaluation",
        "",
        f"Gold set: `{gold.path}`, {len(gold.abstracts)} abstracts in {len(gold.by_pair())} "
        f"pairs with {gold.claims} claims ({no_claim} abstracts with none), and "
        f"{len(gold.edges)} edges."
        + (f" Lines skipped: {', '.join(skipped)}." if skipped else "")
        + f" UBERON: {result['uberon_version'] or 'none'}."
        + (f" Gold claims: {_anatomy(result['anatomy'])}." if _anatomy(result["anatomy"]) else "")
        + f" Species in proposals: {species}.",
        "",
        *_gold_section(result["gold"]),
        "## Comparison",
        "",
        *comparison_table(results),
        "",
        "Claims are scored on the gold abstracts the run answered, as checked: without those "
        "that failed a check or whose evidence sentence wasn't found, even after the repair "
        "pass. Exact needs the same polarity and statement; up to an ancestor lets the run's "
        "statement be broader than the curator's. Edges are refined on the run's checked claims "
        "where it answered every gold abstract of their pair, with no review: support and PMIDs "
        "are scored against the refinement on the gold claims edge by edge, proposals and links "
        "pair by pair. Cost is from the run's logged label calls.",
        "",
    ]
    for r in results:
        usage, claims = r["usage"], r["claims"]
        fallback = r["fallback_served"]
        lines += [
            f"## `{r['run']}`",
            "",
            f"- Coverage: {r['covered']} of {r['abstracts']} gold abstracts "
            f"({_pct(r['coverage'])})"
            + (
                f"; {fallback} more answered by a fallback model, left out of the scores, and so "
                f"are the edges of their pairs ({r['edges_fallback_served']})"
                if fallback
                else ""
            ),
            f"- Claims: {claims['claims']}, of which {claims['failing_checks']} failed a check "
            f"and {claims['sentences_not_found']} have an evidence sentence not found; "
            f"{claims['sentences_repaired']} sentences repaired by {r['repairer']}",
            *([f"- Anatomy: {_anatomy(r['anatomy'])}"] if _anatomy(r["anatomy"]) else []),
            f"- Edges refined: {r['edges_covered']} of {r['edges']}, in {r['pairs_covered']} of "
            f"{r['pairs']} pairs",
            f"- Calls: {usage['calls']} ({usage['errors']} failed) over {usage['pairs']} pairs; "
            f"{_tokens(r['input_tokens_per_pair'])} input and "
            f"{_tokens(r['output_tokens_per_pair'])} output tokens per pair",
            f"- Cost: {_money(r['cost_per_call'])} per call, {_money(r['cost_per_pair'])} per "
            f"pair, {_money(r['cost_per_10k_pairs'])} per 10,000 pairs",
            "",
            *_claims_table(r),
            *_support_table(r["support"]),
            *_overlap_table(r),
            *_disagreements(r["disagreements"]),
        ]
    return "\n".join(lines).rstrip() + "\n"


# --- CLI ------------------------------------------------------------------------------------


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--gold",
        type=Path,
        default=None,
        metavar="FILE",
        help=f"gold JSONL: curator claims per (pair, PMID), and the edges to score "
        f"(default: <work>/{GOLD})",
    )
    parser.add_argument(
        "--run",
        action="append",
        default=None,
        metavar="MODEL[@PROMPT_VERSION]",
        help="run to score; repeat to compare. A model alone scores each of its prompt "
        "versions (default: the run in label_run.json)",
    )
    parser.add_argument(
        "--species-in-proposals",
        action=argparse.BooleanOptionalAction,
        default=config.SPECIES_IN_PROPOSALS,
        help="score proposals with their species, as `apply --species-in-proposals` writes "
        "them; without it, the species is taken out of both sides' proposals, as step 6 does",
    )
    parser.add_argument(
        "--draw",
        type=int,
        default=None,
        metavar="N",
        help=f"instead of scoring, draw N pairs to curate from <work>/{records.REFINEMENTS}, "
        f"stratified by outcome ({', '.join(OUTCOMES)}) and predicate family, into "
        f"<work>/{GOLD_PAIRS} for `label --pairs`; pairs with no abstracts are left out",
    )
    parser.add_argument("--seed", type=int, default=0, help="seed for --draw (default: 0)")


def _draw(args: argparse.Namespace) -> int:
    try:
        path, lines, without = draw_pairs(args.work, args.draw, args.seed)
    except (EvaluateError, ValueError) as e:
        print(f"evaluate: {e}", file=sys.stderr)
        return 1
    if without:
        print(f"evaluate: {without} refined pairs with no abstracts left out: nothing to curate")
    strata = Counter((line["outcome"], line["family"]) for line in lines)
    for (o, family), n in sorted(strata.items()):
        print(f"evaluate: {n} pairs drawn for {o} {family}")
    if len(lines) < args.draw:
        print(
            f"evaluate: warning: only {len(lines)} refined pairs have abstracts, fewer than "
            f"{args.draw}",
            file=sys.stderr,
        )
    print(f"evaluate: wrote {len(lines)} pairs to {path}; label them with `label --pairs {path}`")
    return 0


def run(args: argparse.Namespace) -> int:
    if args.draw is not None:
        return _draw(args)
    gold_path = args.gold if args.gold is not None else args.work / GOLD
    runs: list[str | tuple[str, str | None]] = list(args.run or [])
    label_run = records.read_label_run(args.work)
    try:
        if not runs:
            if label_run is None:
                raise EvaluateError(
                    f"no {args.work / records.LABEL_RUN}; pass --run MODEL[@PROMPT_VERSION]"
                )
            runs = [(label_run.model, label_run.prompt_version)]
        categories = categories_of(args.work)
        gold = load_gold(gold_path, categories)
        result = evaluate(
            gold,
            args.cache_dir,
            runs,
            label_run,
            categories,
            species_in_proposals=args.species_in_proposals,
        )
    except EvaluateError as e:
        print(f"evaluate: {e}", file=sys.stderr)
        return 1
    if not any(r["covered"] for r in result["runs"]):
        print(
            "evaluate: no run has answers for the gold abstracts; label their pairs with each "
            "model first (`kgb-augment label --pairs FILE --model M` labels exactly the pairs in "
            "a file `evaluate --draw` wrote)",
            file=sys.stderr,
        )
        return 1
    failed = result["anatomy"].get("failed", 0) + sum(
        r["anatomy"].get("failed", 0) for r in result["runs"]
    )
    if failed:
        print(
            f"evaluate: warning: {failed} anatomy names couldn't be looked up, so they narrow no "
            "statement; rerun to retry",
            file=sys.stderr,
        )
    path = args.work / REPORT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report(gold, result), encoding="utf-8")
    g = result["gold"]
    supported = g["outcomes"]["supported"] + g["outcomes"]["refined"]
    print(
        f"evaluate: {len(gold.abstracts)} gold abstracts, {len(gold.edges)} edges, "
        f"{len(result['runs'])} runs; on the gold claims {supported} of {g['scored']} edges are "
        f"supported, with {g['proposed']} statements proposed and {g['links']} linked"
    )
    print("\n".join(comparison_table(result["runs"])))
    print(f"evaluate: wrote {path}")
    return 0
