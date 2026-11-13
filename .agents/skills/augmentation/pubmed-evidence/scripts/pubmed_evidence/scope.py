"""Step 1 (`kgb-augment select`): choose the edges worth checking in the literature, and group
them by node pair.

An edge is checked when abstracts state its relationship as a claim that a paper could
contradict (DESIGN.md, "Step 1: Which edges to query"). There are two kinds of rule.

A *statement rule* names predicates. It picks edges by (subject category, predicate, object
category), matching on Biolink ancestry so the child predicates and categories the transform
picks are covered, and, if it lists them, by how the edge was made (its `knowledge_level` and
`agent_type`). The first statement rule that matches an edge decides its tier:

- `query` rules keep every matching edge.
- `capped` rules (gene-gene, gene-phenotype) keep only their best-scored `--capped-limit` pairs,
  after dropping statements below `--min-score` if given; ties, unscored pairs included, go to
  the pair with the most sources, then by a hash of the pair id (`cap_rank`).
- `skip` rules keep nothing. They exist so the report can say which skip rule covered an
  edge type, and so an edge type no rule covers shows up as `unmatched`.

A statement rule matches an edge in its own direction, and also reversed when the rule is
`symmetric` or Biolink marks the edge's predicate symmetric (`interacts_with`, `binds`, ...),
since a source may write either "A binds B" or "B binds A". One that lists knowledge levels or
agent types matches only an edge whose own is among them.

A *provenance rule* names knowledge levels or agent types and no predicates (nor a subject, an
object or `symmetric`). It picks no edges by itself: it lowers the tier of the edges a
statement rule picked (`decide`). When the statement rule isn't a skip, and the first
provenance rule that the edge's knowledge level and agent type match has a tier no higher than
the statement rule's (skip < capped < query), the edge takes the provenance rule's tier: a skip
is reported as `skip:<provenance rule>`, and a capped edge goes into the provenance rule's own
cap. Its statement still names its statement rule (`SelectedStatement.rule`), which sets the
predicates the pair's claims may use, and names the provenance rule beside it
(`SelectedStatement.provenance_rule`). A statement made of edges in the statement rule's own
tier and edges a provenance rule lowered takes the higher tier, and no provenance rule; one
whose kept edges were all lowered takes the highest tier among them, then the provenance rule
first in rule order. Every kept edge counts in the statement's edges, sources, PMIDs and score.
So order matters among statement rules, and among provenance rules, but not between the two.

The default provenance rules cap edges mined from the literature (`text-co-occurrence` on
`knowledge_level: text_co_occurrence`, `text-mined` on `agent_type: text_mining_agent`),
whatever their predicate: eRAM writes a co-mention as `gene_associated_with_condition`. They
carry no publications or supporting sentence, so this step is the only one that can check
them, and it isn't circular: label asks whether an abstract states the relationship, not
whether it mentions both entities. The cap bounds the cost. A rules file can still skip them.

A negated source edge (`negated: true`) is kept like any other, as its own statement apart from
the positive one. Nothing is narrower than a negated statement (`refine.Lattice.entails`), so
step 6 never supports it. The edges step 6 added on an earlier run (their `id` starts with
`kgbcia-augmentation:`) are skipped: step 6 proposes them again from the edges they refine,
which are checked instead. Edges whose subject or object is not in `nodes.jsonl` are skipped
too. Both are counted in the report, as are the negated edges kept, and the kept edges whose
predicate doesn't read from their subject to their object the way Biolink's domain, range and
canonical direction put it (`misdirected_edges`, by `biolink.oriented`): 'gene
condition_associated_with_gene disease'. They're kept and checked as written, but the label
step reads claims Biolink's way round, so claims rarely reach them.

Kept edges become one `SelectedStatement` per distinct statement (`graphio.edge_statement_id`),
one `Pair` per unordered node pair and one `Entity` per node in a kept pair, in the work directory:
`statements.jsonl`, `pairs.jsonl`, `entities.jsonl`, and `scope_report.json` with the counts.

`--rules FILE` replaces the built-in rules with a JSON list of objects (or `{"rules": [...]}`)
with the same fields as `Rule`, in order of precedence:

    [{"id": "text-mined", "tier": "capped", "agent_type": ["text_mining_agent"]},
     {"id": "gene-disease", "tier": "query",
      "subject": ["biolink:GeneOrGeneProduct"],
      "predicates": ["biolink:gene_associated_with_condition", "biolink:causes"],
      "object": ["biolink:Disease"],
      "symmetric": false}]

`subject` and `object` list Biolink categories (empty or left out: any node); `tier` is
`query`, `capped` or `skip`; `symmetric` defaults to false; `knowledge_level` and `agent_type`
list the Biolink values the edge's may take (empty or left out: any). A rule that leaves
`predicates` out is a provenance rule: it must list a knowledge level or an agent type, and
can't set `subject`, `object` or `symmetric`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from pubmed_evidence import biolink, config, records
from pubmed_evidence.graphio import (
    EDGES,
    ID_PREFIX,
    NODES,
    InvalidEdges,
    edge_statement_id,
    extras,
    graph_file,
    invalid_warning,
    is_negated,
    pair_id,
    pair_nodes,
    pmid_key,
    pmid_of,
    qualifiers_of,
    read_jsonl,
    read_nodes,
    write_jsonl,
)
from pubmed_evidence.records import Entity, Pair, SelectedStatement
from shared.schema import AgentType, KnowledgeLevel, Node

STEP = "select"

# NodeNorm conflation merges proteins into genes, but an unconflated protein should match too.
GENE = "biolink:GeneOrGeneProduct"
# Drug is below ChemicalEntity.
CHEMICAL = "biolink:ChemicalEntity"

# Edge slots read as a confidence score for capped rules, best (highest) value wins. Scales
# differ between sources, so `--min-score` compares against the raw value. Not
# `lit_support_score`: step 6 writes that, from the literature, and a source's confidence is
# wanted.
SCORE_SLOTS = (
    "score",
    "confidence_score",
    "has_confidence_score",
    "combined_score",
    "stringdb_combined_score",
)
# Node slots that give an entity's synonyms, besides its name.
SYNONYM_SLOTS = ("symbol", "synonym", "full_name")

PROGRESS_EVERY = 1_000_000

# A provenance rule lowers an edge's tier, never raises it: it applies when its tier is at most
# the statement rule's.
TIER_RANK = {"skip": 0, "capped": 1, "query": 2}


class Rule(BaseModel):
    """A scope rule (module docstring). A statement rule names predicates, and keeps edges (or
    names them skipped) by category and predicate ancestry, and by the edge's knowledge level
    and agent type if it lists them. A provenance rule names only knowledge levels or agent
    types, and lowers the tier of the edges a statement rule keeps."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    tier: Literal["query", "capped", "skip"]
    subject: tuple[str, ...] = Field(default=(), description="Categories; empty: any node")
    predicates: tuple[str, ...] = Field(
        default=(),
        description="Empty: a provenance rule, which needs a knowledge_level or agent_type",
    )
    object: tuple[str, ...] = Field(default=(), description="Categories; empty: any node")
    symmetric: bool = False
    knowledge_level: tuple[KnowledgeLevel, ...] = Field(
        default=(), description="The edge's knowledge_level is one of these; empty: any"
    )
    agent_type: tuple[AgentType, ...] = Field(
        default=(), description="The edge's agent_type is one of these; empty: any"
    )

    @model_validator(mode="after")
    def _names_something(self) -> Rule:
        if not (self.predicates or self.knowledge_level or self.agent_type):
            raise ValueError("a rule needs predicates, a knowledge_level or an agent_type")
        if not self.predicates and (self.subject or self.object or self.symmetric):
            raise ValueError(
                "a provenance rule (one without predicates) can't set subject, object or symmetric"
            )
        return self

    @property
    def by_provenance(self) -> bool:
        """Whether the rule matches on how the edge was made, not on its statement alone."""
        return bool(self.knowledge_level or self.agent_type)

    @property
    def is_provenance(self) -> bool:
        """A provenance rule: no predicates, so it picks no edges by itself (`decide`)."""
        return not self.predicates


DEFAULT_RULES: tuple[Rule, ...] = (
    # Provenance rules: an edge mined from the literature (a co-mention, or a text-mining
    # pipeline's reading of one) is capped, whatever statement rule keeps it. Such edges are the
    # least trustworthy, and carry no publications or supporting sentence, so the cleaning
    # stage's evidence check can't judge them: this step is the only one that can. Checking
    # them isn't circular, since label asks whether an abstract states the relationship, not
    # whether it mentions both entities. The cap bounds the cost: eRAM writes its co-mentions
    # as `gene_associated_with_condition`, and in gene-disease's query tier they made 127,774
    # of the Nov 12 2026 run's 131,195 pairs. Their place in this list doesn't matter, only
    # their order between themselves.
    Rule(id="text-co-occurrence", tier="capped", knowledge_level=("text_co_occurrence",)),
    Rule(id="text-mined", tier="capped", agent_type=("text_mining_agent",)),
    # Two entities mentioned together isn't a claim, and checking co-mention against PubMed
    # would be circular. It is under `associated_with`, so it is skipped before the rules below.
    Rule(
        id="literature-co-occurrence",
        tier="skip",
        predicates=("biolink:occurs_together_in_literature_with",),
        symmetric=True,
    ),
    Rule(
        id="chemical-treats-disease",
        tier="query",
        subject=(CHEMICAL,),
        predicates=(
            "biolink:treats",
            "biolink:contraindicated_in",
            "biolink:treats_or_applied_or_studied_to_treat",
        ),
        object=("biolink:Disease",),
    ),
    Rule(
        id="chemical-affects-gene",
        tier="query",
        subject=(CHEMICAL,),
        predicates=(
            "biolink:affects",
            "biolink:directly_physically_interacts_with",
            "biolink:binds",
        ),
        object=(GENE,),
    ),
    # `associated_with` covers gene_associated_with_condition, genetically_associated_with,
    # the likelihood predicates (ClinGen's "Likely Pathogenic" in Monarch), correlated_with and
    # biomarker_for; `contributes_to` covers causes.
    Rule(
        id="gene-disease",
        tier="query",
        subject=(GENE,),
        predicates=("biolink:associated_with", "biolink:contributes_to"),
        object=("biolink:Disease",),
    ),
    Rule(
        id="gene-gene",
        tier="capped",
        subject=(GENE,),
        predicates=("biolink:physically_interacts_with", "biolink:interacts_with"),
        object=(GENE,),
        symmetric=True,
    ),
    Rule(
        id="gene-phenotype",
        tier="capped",
        subject=(GENE,),
        predicates=("biolink:has_phenotype",),
        object=("biolink:PhenotypicFeature",),
    ),
    # Pathway and GO names rarely appear verbatim in abstracts, and GO annotations carry their
    # own references and evidence codes. Pathway is below BiologicalProcessOrActivity.
    Rule(
        id="pathway-go",
        tier="skip",
        subject=(GENE,),
        predicates=("biolink:participates_in", "biolink:enables", "biolink:located_in"),
        object=("biolink:BiologicalProcessOrActivity", "biolink:CellularComponent"),
    ),
    # Measurements rather than claims, and very numerous. Cell is below AnatomicalEntity.
    Rule(
        id="expression",
        tier="skip",
        subject=(GENE,),
        predicates=("biolink:expressed_in",),
        object=("biolink:AnatomicalEntity",),
    ),
    # True by construction. DESIGN.md names the first of each pair; the inverses are the same
    # relationships written the other way round.
    Rule(
        id="by-construction",
        tier="skip",
        predicates=(
            "biolink:orthologous_to",
            "biolink:has_part",
            "biolink:part_of",
            "biolink:subclass_of",
            "biolink:superclass_of",
            "biolink:member_of",
            "biolink:has_member",
        ),
        symmetric=True,
    ),
)


class RulesError(ValueError):
    """A `--rules` file that can't be used; the message is one line."""


def load_rules(path: Path) -> tuple[Rule, ...]:
    """Rules from a JSON file (format in the module docstring)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise RulesError(f"can't read rules file {path}: {e}") from e
    if isinstance(data, dict):
        data = data.get("rules")
    if not isinstance(data, list) or not data:
        raise RulesError(f"rules file {path}: expected a non-empty JSON list of rules")
    try:
        rules = tuple(Rule.model_validate(r) for r in data)
    except ValidationError as e:
        first = e.errors()[0]
        where = ".".join(str(x) for x in first["loc"]) or "(whole)"
        raise RulesError(f"rules file {path}: rule {where}: {first['msg']}") from e
    ids = [r.id for r in rules]
    if len(set(ids)) != len(ids):
        raise RulesError(f"rules file {path}: rule ids must be unique")
    tk = biolink.toolkit()
    for r in rules:
        for curie in (*r.subject, *r.predicates, *r.object):
            if tk.get_element(curie) is None:
                raise RulesError(f"rules file {path}: rule {r.id}: {curie} is not in Biolink")
    return rules


def _side(categories: tuple[str, ...], allowed: tuple[str, ...]) -> bool:
    return not allowed or biolink.is_a(list(categories), list(allowed))


def _made_as(rule: Rule, knowledge_level: str | None, agent_type: str | None) -> bool:
    """Whether the edge's knowledge level and agent type are among the rule's (empty: any)."""
    if rule.knowledge_level and knowledge_level not in rule.knowledge_level:
        return False
    return not rule.agent_type or agent_type in rule.agent_type


def match_rule(
    rules: Iterable[Rule],
    subject_categories: tuple[str, ...],
    predicate: str,
    object_categories: tuple[str, ...],
    knowledge_level: str | None = None,
    agent_type: str | None = None,
) -> Rule | None:
    """The first statement rule that matches the edge, in either direction where that is
    allowed. A rule that lists knowledge levels or agent types matches only an edge whose own is
    among them. Provenance rules are passed over: `provenance_rule` finds those."""
    for rule in rules:
        if rule.is_provenance or not _made_as(rule, knowledge_level, agent_type):
            continue
        if not biolink.is_a(predicate, list(rule.predicates)):
            continue
        if _side(subject_categories, rule.subject) and _side(object_categories, rule.object):
            return rule
        reversible = rule.symmetric or biolink.is_symmetric(predicate)
        if (
            reversible
            and _side(object_categories, rule.subject)
            and _side(subject_categories, rule.object)
        ):
            return rule
    return None


def provenance_rule(
    rules: Iterable[Rule], knowledge_level: str | None, agent_type: str | None
) -> Rule | None:
    """The first provenance rule that the edge's knowledge level and agent type match."""
    for rule in rules:
        if rule.is_provenance and _made_as(rule, knowledge_level, agent_type):
            return rule
    return None


def decide(
    rules: Iterable[Rule],
    subject_categories: tuple[str, ...],
    predicate: str,
    object_categories: tuple[str, ...],
    knowledge_level: str | None = None,
    agent_type: str | None = None,
) -> tuple[Rule | None, Rule | None]:
    """(statement rule, provenance rule) for an edge. The statement rule is the first that
    matches it (`match_rule`), None if none does: the edge is unmatched. The provenance rule,
    if any, sets the edge's tier instead of the statement rule: the first provenance rule the
    edge's knowledge level and agent type match (`provenance_rule`), when the statement rule
    isn't a skip and the provenance rule's tier is no higher than its (`TIER_RANK`)."""
    rules = tuple(rules)
    statement = match_rule(
        rules, subject_categories, predicate, object_categories, knowledge_level, agent_type
    )
    if statement is None or statement.tier == "skip":
        return statement, None
    lower = provenance_rule(rules, knowledge_level, agent_type)
    if lower is not None and TIER_RANK[lower.tier] <= TIER_RANK[statement.tier]:
        return statement, lower
    return statement, None


def edge_score(edge_slots: dict[str, Any]) -> float | None:
    """The best numeric value among an edge's SCORE_SLOTS; booleans and NaN don't count."""
    best = None
    for slot in SCORE_SLOTS:
        v = edge_slots.get(slot)
        if isinstance(v, bool) or not isinstance(v, int | float) or math.isnan(v):
            continue
        best = float(v) if best is None else max(best, float(v))
    return best


def usable_name(node_id: str, name: str | None) -> str | None:
    """`name`, unless it is only the node's identifier: the whole CURIE, its local part, or the
    local part's own id (Xenbase names genes "XB-GENE-865198"; dictyBase phenotypes are often
    named "0001015"). Such a name says nothing to PubMed or a reader."""
    if not name or not name.strip():
        return None
    # MONDO keeps a retired term's name as "obsolete <name>".
    name = re.sub(r"^obsolete\s+", "", name.strip(), flags=re.I)
    local = node_id.split(":", 1)[-1]
    if not name or name.casefold() in {node_id.casefold(), local.casefold()}:
        return None
    return name


def entity_of(node: Node) -> Entity:
    """The node as step 2 searches for it and step 3 names it. A node whose name is only its
    identifier is named by its first symbol or synonym instead, or left unnamed."""
    slots = extras(node)
    name = usable_name(node.id, node.name)
    synonyms = synonyms_of(slots, name)
    if name is None and synonyms:
        name, synonyms = synonyms[0], synonyms[1:]
    return Entity(
        id=node.id,
        name=name,
        category=node.category,
        equivalent_identifiers=node.equivalent_identifiers,
        synonyms=synonyms,
    )


def synonyms_of(node_slots: dict[str, Any], name: str | None) -> list[str]:
    """The node's symbol, synonyms and full name, deduplicated ignoring case, without its name."""
    seen = {name.casefold()} if name else set()
    out: list[str] = []
    for slot in SYNONYM_SLOTS:
        value = node_slots.get(slot)
        for s in [value] if isinstance(value, str) else value or []:
            if isinstance(s, str) and (s := s.strip()) and s.casefold() not in seen:
                seen.add(s.casefold())
                out.append(s)
    return out


class _Accumulator:
    """A statement being collected from the edges that make it."""

    __slots__ = (
        "subject",
        "predicate",
        "object",
        "qualifiers",
        "negated",
        "rule",
        "edges",
        "sources",
        "pmids",
        "score",
        "own_edges",
        "lowered_by",
        "provenance",
    )

    def __init__(self, edge, rule: str):
        self.subject = edge.subject
        self.predicate = edge.predicate
        self.object = edge.object
        self.qualifiers = qualifiers_of(edge)
        self.negated = is_negated(edge)
        self.rule = rule
        self.edges = 0
        self.sources: set[str] = set()
        self.pmids: set[str] = set()
        self.score: float | None = None
        # Edges whose tier the statement rule set, and the provenance rules that set the rest.
        self.own_edges = 0
        self.lowered_by: set[str] = set()
        # The provenance rule that sets the statement's tier, once `settle` has run.
        self.provenance: str | None = None

    def add(self, edge, score: float | None, lowered_by: str | None = None) -> None:
        self.edges += 1
        self.sources.add(edge.primary_knowledge_source)
        self.pmids.update(p for p in map(pmid_of, edge.publications) if p)
        if score is not None and (self.score is None or score > self.score):
            self.score = score
        if lowered_by is None:
            self.own_edges += 1
        else:
            self.lowered_by.add(lowered_by)

    def settle(self, rules: dict[str, Rule], order: dict[str, int]) -> None:
        """Set the provenance rule that sets the statement's tier: none if the statement rule
        set any of its edges' (its tier is then the higher); else the one with the highest
        tier among those that lowered its edges, then the first in rule order."""
        if self.own_edges or not self.lowered_by:
            self.provenance = None
            return
        self.provenance = min(self.lowered_by, key=lambda r: (-TIER_RANK[rules[r].tier], order[r]))

    @property
    def owner(self) -> str:
        """The rule whose tier, cap and report entry the statement takes: its provenance rule
        if it has one, else its statement rule."""
        return self.provenance or self.rule

    @property
    def pair_id(self) -> str:
        return pair_id(self.subject, self.object)


def _read_categories(input_dir: Path) -> dict[str, tuple[str, ...]]:
    """Node id -> categories, with equal category lists shared to save memory."""
    out: dict[str, tuple[str, ...]] = {}
    shared: dict[tuple[str, ...], tuple[str, ...]] = {}
    for d in read_jsonl(graph_file(input_dir, NODES)):
        value = d.get("category") or ()
        cats = (value,) if isinstance(value, str) else tuple(value)
        out[d["id"]] = shared.setdefault(cats, cats)
    return out


def cap_rank(pair: str, best: float | None, sources: int) -> tuple:
    """A pair's sort key in a capped rule, first kept first: scored before unscored, the best
    score of the rule's statements in it highest first, then the most distinct
    `primary_knowledge_source`s among those statements, then the SHA-256 of the pair id, and
    last the pair id. The hash, stable across runs and machines unlike `hash()`, spreads an
    unscored cap over the graph: by pair id alone it would keep the pairs whose CURIEs sort
    first, one prefix's."""
    digest = hashlib.sha256(pair.encode("utf-8")).hexdigest()
    return (best is None, -(best or 0.0), -sources, digest, pair)


def _apply_caps(
    statements: dict[str, _Accumulator],
    rules: tuple[Rule, ...],
    capped_limit: int,
    min_score: float | None,
) -> dict[str, dict[str, int]]:
    """Drop capped-rule statements below `min_score`, then all but the top `capped_limit`
    pairs of each capped rule. A statement is in the cap of its provenance rule if it has one,
    else of its statement rule (`_Accumulator.owner`). Every pair in the rule's statements
    counts against its cap, a pair another rule keeps too: the statements kept widen what
    review sends. Returns what was dropped, per rule."""
    by_rule: dict[str, list[str]] = defaultdict(list)
    for sid, acc in statements.items():
        by_rule[acc.owner].append(sid)
    dropped: dict[str, dict[str, int]] = {}
    for rule in rules:
        if rule.tier != "capped":
            continue
        counts = dict.fromkeys(
            ("min_score_statements", "min_score_edges", "cap_pairs", "cap_statements", "cap_edges"),
            0,
        )
        sids = by_rule.get(rule.id, [])
        if min_score is not None:
            low = [s for s in sids if (sc := statements[s].score) is None or sc < min_score]
            for sid in low:
                counts["min_score_statements"] += 1
                counts["min_score_edges"] += statements.pop(sid).edges
            sids = [s for s in sids if s in statements]
        pairs: dict[str, list[str]] = defaultdict(list)
        best: dict[str, float | None] = {}
        sources: dict[str, set[str]] = defaultdict(set)
        for sid in sids:
            acc = statements[sid]
            pairs[acc.pair_id].append(sid)
            sources[acc.pair_id] |= acc.sources
            old = best.get(acc.pair_id)
            if acc.score is not None and (old is None or acc.score > old):
                best[acc.pair_id] = acc.score
            else:
                best.setdefault(acc.pair_id, old)
        ranked = sorted(pairs, key=lambda p: cap_rank(p, best[p], len(sources[p])))
        for pid in ranked[max(capped_limit, 0) :]:
            counts["cap_pairs"] += 1
            for sid in pairs[pid]:
                counts["cap_statements"] += 1
                counts["cap_edges"] += statements.pop(sid).edges
        dropped[rule.id] = counts
    return dropped


def select(
    input_dir: Path,
    work: Path,
    *,
    capped_limit: int = config.CAPPED_LIMIT,
    min_score: float | None = None,
    rules_file: Path | None = None,
) -> dict:
    """Run step 1: write the work files and return the scope report."""
    rules = load_rules(rules_file) if rules_file is not None else DEFAULT_RULES
    order = {r.id: i for i, r in enumerate(rules)}
    by_id = {r.id: r for r in rules}
    # Nodes are read twice: categories now, since rules match on them as edges stream past,
    # and full records at the end, only for the nodes of kept pairs.
    categories = _read_categories(input_dir)

    matches: dict[tuple, tuple[Rule | None, Rule | None]] = {}
    statements: dict[str, _Accumulator] = {}
    skipped: Counter[tuple[str, str, str, str]] = Counter()
    edges_read = earlier = missing = 0
    invalid = InvalidEdges()
    for edges_read, row in enumerate(read_jsonl(graph_file(input_dir, EDGES)), 1):
        if edges_read % PROGRESS_EVERY == 0:
            print(f"{STEP}: {edges_read:,} edges read", file=sys.stderr)
        edge = invalid.check(row, edges_read)
        if edge is None:
            continue
        if edge.id and edge.id.startswith(ID_PREFIX):
            earlier += 1
            continue
        s_cats = categories.get(edge.subject)
        o_cats = categories.get(edge.object)
        if s_cats is None or o_cats is None:
            missing += 1
            continue
        key = (s_cats, edge.predicate, o_cats, edge.knowledge_level, edge.agent_type)
        if key not in matches:
            matches[key] = decide(rules, *key)
        rule, lowered_by = matches[key]
        if rule is None or (lowered_by or rule).tier == "skip":
            label = f"skip:{(lowered_by or rule).id}" if rule else "unmatched"
            first = (s_cats[0] if s_cats else "", edge.predicate, o_cats[0] if o_cats else "")
            skipped[(*first, label)] += 1
            continue
        sid = edge_statement_id(edge)
        acc = statements.get(sid)
        if acc is None:
            acc = statements[sid] = _Accumulator(edge, rule.id)
        acc.add(edge, edge_score(extras(edge)), lowered_by.id if lowered_by else None)
    matched_edges: Counter[str] = Counter()
    for acc in statements.values():
        acc.settle(by_id, order)
        matched_edges[acc.owner] += acc.edges
    matched_statements = Counter(acc.owner for acc in statements.values())

    dropped = _apply_caps(statements, rules, capped_limit, min_score)

    pair_statements: dict[str, list[str]] = defaultdict(list)
    for sid, acc in statements.items():
        pair_statements[acc.pair_id].append(sid)
    pairs = []
    for pid in sorted(pair_statements):
        sids = sorted(pair_statements[pid])
        pmids = set().union(*(statements[s].pmids for s in sids))
        rule_ids = sorted({statements[s].rule for s in sids}, key=order.__getitem__)
        pairs.append(
            Pair(
                pair_id=pid,
                nodes=pair_nodes(pid),
                statement_ids=sids,
                source_pmids=sorted(pmids, key=pmid_key),
                rules=rule_ids,
            )
        )
    statement_records = [
        SelectedStatement(
            statement_id=sid,
            pair_id=acc.pair_id,
            subject=acc.subject,
            predicate=acc.predicate,
            object=acc.object,
            qualifiers=acc.qualifiers,
            negated=acc.negated,
            rule=acc.rule,
            provenance_rule=acc.provenance,
            edge_count=acc.edges,
            sources=sorted(acc.sources),
            source_pmids=sorted(acc.pmids, key=pmid_key),
            score=acc.score,
        )
        for sid, acc in sorted(statements.items(), key=lambda kv: (kv[1].pair_id, kv[0]))
    ]

    wanted = {n for p in pairs for n in p.nodes}
    entities = [entity_of(node) for node in read_nodes(input_dir) if node.id in wanted]
    entities.sort(key=lambda e: e.id)

    write_jsonl(work / records.ENTITIES, entities)
    write_jsonl(work / records.STATEMENTS, statement_records)
    write_jsonl(work / records.PAIRS, pairs)

    # A statement counts under its provenance rule if it has one, so a statement rule's counts
    # leave out the statements a provenance rule took.
    per_rule: dict[str, dict[str, Any]] = {}
    for rule in rules:
        if rule.tier == "skip":
            n = sum(c for k, c in skipped.items() if k[3] == f"skip:{rule.id}")
            per_rule[rule.id] = {"tier": rule.tier, "skipped_edges": n}
            continue
        kept = [s for s in statement_records if (s.provenance_rule or s.rule) == rule.id]
        entry: dict[str, Any] = {
            "tier": rule.tier,
            "matched_edges": matched_edges[rule.id],
            "matched_statements": matched_statements[rule.id],
            "edges": sum(s.edge_count for s in kept),
            "statements": len(kept),
            "pairs": len({s.pair_id for s in kept}),
        }
        if rule.tier == "capped":
            d = dropped[rule.id]
            entry["dropped_by_min_score"] = {
                "statements": d["min_score_statements"],
                "edges": d["min_score_edges"],
            }
            entry["dropped_by_cap"] = {
                "pairs": d["cap_pairs"],
                "statements": d["cap_statements"],
                "edges": d["cap_edges"],
            }
        per_rule[rule.id] = entry

    report = {
        "input": str(input_dir),
        "rules_file": str(rules_file) if rules_file is not None else None,
        "capped_limit": capped_limit,
        "min_score": min_score,
        "edges_read": edges_read,
        "earlier_run_skipped": earlier,
        "invalid_edges": invalid.report(),
        "missing_node_skipped": missing,
        "kept": {
            "edges": sum(s.edge_count for s in statement_records),
            "statements": len(statement_records),
            "pairs": len(pairs),
            "entities": len(entities),
            "negated_edges": sum(s.edge_count for s in statement_records if s.negated),
            "misdirected_edges": sum(
                s.edge_count
                for s in statement_records
                if biolink.oriented(categories[s.subject], s.predicate, categories[s.object])
                != (s.predicate, False)
            ),
        },
        "rules": per_rule,
        "skipped": [
            {
                "subject_category": s,
                "predicate": p,
                "object_category": o,
                "edges": n,
                "rule": label,
            }
            for (s, p, o, label), n in sorted(skipped.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
    }
    records.write_json(work / records.SCOPE_REPORT, report)
    return report


def summary(report: dict) -> list[str]:
    kept = report["kept"]
    lines = [
        f"read {report['edges_read']:,} edges; kept {kept['edges']:,} edges "
        f"({kept['negated_edges']:,} negated) as {kept['statements']:,} statements in "
        f"{kept['pairs']:,} pairs ({kept['entities']:,} entities)"
    ]
    for rule_id, r in report["rules"].items():
        if r["tier"] == "skip":
            continue
        line = f"  {rule_id}: {r['edges']:,} edges, {r['statements']:,} statements, "
        line += f"{r['pairs']:,} pairs"
        if r["tier"] == "capped":
            cap, low = r["dropped_by_cap"], r["dropped_by_min_score"]
            line += f" (dropped {cap['pairs']:,} pairs over the cap, "
            line += f"{low['statements']:,} statements under --min-score)"
        lines.append(line)
    by_skip = sum(e["edges"] for e in report["skipped"] if e["rule"] != "unmatched")
    unmatched = sum(e["edges"] for e in report["skipped"] if e["rule"] == "unmatched")
    lines.append(
        f"skipped {report['earlier_run_skipped']:,} added by an earlier step 6, "
        f"{report['missing_node_skipped']:,} with a node missing from {NODES}, "
        f"{by_skip:,} by skip rules, {unmatched:,} unmatched"
    )
    return lines


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--capped-limit",
        type=int,
        default=config.CAPPED_LIMIT,
        help=f"pairs kept per capped rule (gene-gene, gene-phenotype, and text-co-occurrence "
        f"and text-mined for edges mined from the literature), best-scored first "
        f"(default {config.CAPPED_LIMIT:,})",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=None,
        help="drop capped-rule statements whose best score is below this, or that have none "
        "(default: no cutoff)",
    )
    parser.add_argument(
        "--rules",
        type=Path,
        default=None,
        help="JSON file of rules replacing the built-in ones (format in reference/rules.md)",
    )


def run(args: argparse.Namespace) -> int:
    input_dir = Path(args.input)
    for name in (EDGES, NODES):
        if not graph_file(input_dir, name).exists():
            print(f"{STEP}: no {name} in {input_dir}", file=sys.stderr)
            return 1
    try:
        report = select(
            input_dir,
            Path(args.work),
            capped_limit=getattr(args, "capped_limit", config.CAPPED_LIMIT),
            min_score=getattr(args, "min_score", None),
            rules_file=getattr(args, "rules", None),
        )
    except RulesError as e:
        print(f"{STEP}: {e}", file=sys.stderr)
        return 1
    except (ValidationError, json.JSONDecodeError) as e:
        reason = str(e).splitlines()[0]
        print(f"{STEP}: invalid record in {input_dir}: {reason}", file=sys.stderr)
        return 1
    for line in summary(report):
        print(f"{STEP}: {line}")
    warning = invalid_warning(
        STEP, report["invalid_edges"], graph_file(input_dir, EDGES), "skipped"
    )
    if warning:
        print(warning, file=sys.stderr)
    misdirected = report["kept"]["misdirected_edges"]
    if misdirected:
        print(
            f"{STEP}: warning: {misdirected:,} kept edges have a predicate that Biolink's "
            "domain, range and canonical direction don't read from their subject to their "
            "object; they're checked as written, but claims, read Biolink's way round, rarely "
            "reach them",
            file=sys.stderr,
        )
    print(
        f"{STEP}: wrote {records.STATEMENTS}, {records.PAIRS}, {records.ENTITIES} and "
        f"{records.SCOPE_REPORT} in {args.work}"
    )
    return 0
