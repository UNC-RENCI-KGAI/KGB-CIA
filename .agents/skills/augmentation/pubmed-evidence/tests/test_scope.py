"""Step 1: which edges are kept, how they become statements and pairs, and the report."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pytest
from pubmed_evidence import config, graphio, label, records, scope
from pubmed_evidence.graphio import pair_id, write_jsonl
from pubmed_evidence.records import Entity, Pair, SelectedStatement
from toy import (
    ABL1,
    ASPIRIN,
    CML,
    EDGES,
    IMATINIB,
    MDM2,
    NODES,
    SEIZURE,
    TP53,
)

from shared import schema
from shared.schema import Edge, Node

GENE_GENE = pair_id(TP53, MDM2)
GENE_PHENOTYPE = pair_id(TP53, SEIZURE)


def _run(graph: Path, work: Path, **kw):
    report = scope.select(graph, work, **kw)
    statements = list(records.read_records(work / records.STATEMENTS, SelectedStatement))
    pairs = {p.pair_id: p for p in records.read_records(work / records.PAIRS, Pair)}
    entities = {e.id: e for e in records.read_records(work / records.ENTITIES, Entity)}
    return report, statements, pairs, entities


def test_duplicate_statement_is_one_statement_with_both_sources(toy_graph, tmp_path):
    _, statements, pairs, _ = _run(toy_graph, tmp_path / "work")
    treats = [s for s in statements if s.predicate == "biolink:treats" and s.subject == IMATINIB]
    assert len(treats) == 1
    t = treats[0]
    assert (t.subject, t.object, t.rule) == (IMATINIB, CML, "chemical-treats-disease")
    assert t.edge_count == 2
    assert t.sources == ["infores:chembl", "infores:drugcentral"]
    assert t.source_pmids == ["11287972"]
    assert pairs[pair_id(IMATINIB, CML)].statement_ids == [t.statement_id]


def test_qualified_edge_is_its_own_statement(toy_graph, tmp_path):
    _, statements, pairs, _ = _run(toy_graph, tmp_path / "work")
    chem_gene = [s for s in statements if s.pair_id == pair_id(IMATINIB, ABL1)]
    assert {s.rule for s in chem_gene} == {"chemical-affects-gene"}
    affects = next(s for s in chem_gene if s.predicate == "biolink:affects")
    assert affects.qualifiers == {
        "object_aspect_qualifier": "activity",
        "object_direction_qualifier": "decreased",
        "qualified_predicate": "biolink:causes",
    }
    pair = pairs[pair_id(IMATINIB, ABL1)]
    assert sorted(pair.statement_ids) == sorted(s.statement_id for s in chem_gene)
    assert len(pair.statement_ids) == 2
    assert pair.source_pmids == ["10592173"]
    assert pair.nodes == tuple(sorted((IMATINIB, ABL1)))


def test_statement_ids_match_the_shared_schema():
    """`shared.schema.Statement.id` is the id select gives a statement."""
    for edge in EDGES:
        qualifiers = graphio.qualifiers_of(edge)
        statement = schema.Statement(
            subject=edge.subject,
            predicate=edge.predicate,
            object=edge.object,
            qualified_predicate=qualifiers.pop("qualified_predicate", None),
            qualifiers=qualifiers,
            negated=graphio.is_negated(edge),
        )
        assert statement.id == graphio.edge_statement_id(edge)


def test_skipped_edges_are_reported_and_negated_edges_kept(toy_graph, tmp_path):
    report, statements, pairs, entities = _run(toy_graph, tmp_path / "work")
    assert {s.predicate for s in statements}.isdisjoint(
        {"biolink:participates_in", "biolink:expressed_in", "biolink:orthologous_to"}
    )
    assert report["edges_read"] == len(EDGES)
    assert "negated_skipped" not in report
    assert report["missing_node_skipped"] == 0
    # The negated source edge is checked like any other.
    [negated] = [s for s in statements if s.negated]
    assert (negated.subject, negated.rule) == (ASPIRIN, "chemical-treats-disease")
    assert negated.statement_id == graphio.edge_statement_id(EDGES[-1])
    assert pairs[pair_id(ASPIRIN, CML)].statement_ids == [negated.statement_id]
    assert ASPIRIN in entities
    skipped = {(e["predicate"], e["rule"]): e for e in report["skipped"]}
    assert set(skipped) == {
        ("biolink:participates_in", "skip:pathway-go"),
        ("biolink:expressed_in", "skip:expression"),
        ("biolink:orthologous_to", "skip:by-construction"),
    }
    expressed = skipped[("biolink:expressed_in", "skip:expression")]
    assert expressed["subject_category"] == "biolink:Gene"
    assert expressed["object_category"] == "biolink:AnatomicalEntity"
    assert expressed["edges"] == 1
    assert report["rules"]["pathway-go"] == {"tier": "skip", "skipped_edges": 1}
    kept = report["kept"]
    assert kept == {
        "edges": 9,
        "statements": 8,
        "pairs": 6,
        "entities": 7,
        "negated_edges": 1,
        "misdirected_edges": 0,
    }
    assert "kept 9 edges (1 negated)" in scope.summary(report)[0]


def test_a_negated_edge_is_its_own_statement_beside_the_positive_one(toy_graph, tmp_path):
    update = {"negated": False, "primary_knowledge_source": "infores:x"}
    positive = EDGES[-1].model_copy(update=update)
    graph = tmp_path / "graph"
    write_jsonl(graph / "nodes.jsonl", NODES)
    write_jsonl(graph / "edges.jsonl", [*EDGES, positive])
    _, statements, pairs, _ = _run(graph, tmp_path / "work")
    aspirin = {s.negated: s for s in statements if s.subject == ASPIRIN}
    assert set(aspirin) == {True, False}
    assert aspirin[True].statement_id != aspirin[False].statement_id
    assert aspirin[False].statement_id == graphio.edge_statement_id(positive)
    assert sorted(pairs[pair_id(ASPIRIN, CML)].statement_ids) == sorted(
        s.statement_id for s in aspirin.values()
    )


def test_an_earlier_runs_new_edges_are_skipped(toy_graph, tmp_path):
    # A qualified edge step 6 added: its source statement is checked, never the edge itself.
    added = EDGES[2].model_copy(
        update={"id": "kgbcia-augmentation:0123456789abcdef", "causal_mechanism_qualifier": "x"}
    )
    graph = tmp_path / "graph"
    write_jsonl(graph / "nodes.jsonl", NODES)
    write_jsonl(graph / "edges.jsonl", [*EDGES, added])
    report, statements, _, _ = _run(graph, tmp_path / "work")
    assert report["earlier_run_skipped"] == 1 and report["edges_read"] == len(EDGES) + 1
    assert report["kept"]["statements"] == 8
    assert not any(s.qualifiers.get("causal_mechanism_qualifier") for s in statements)
    assert "1 added by an earlier step 6" in " ".join(scope.summary(report))


def test_gene_gene_pair_deduped_across_directions(toy_graph, tmp_path):
    report, statements, pairs, _ = _run(toy_graph, tmp_path / "work")
    gene_gene = [s for s in statements if s.rule == "gene-gene"]
    assert {s.pair_id for s in gene_gene} == {GENE_GENE}
    assert len(pairs[GENE_GENE].statement_ids) == 2
    assert max(s.score for s in gene_gene) == 0.999
    assert sorted(s.score for s in gene_gene) == [0.62, 0.999]
    assert report["rules"]["gene-gene"]["pairs"] == 1


def test_capped_limit_zero_drops_capped_pairs_only(toy_graph, tmp_path):
    report, statements, pairs, entities = _run(toy_graph, tmp_path / "work", capped_limit=0)
    assert GENE_GENE not in pairs and GENE_PHENOTYPE not in pairs
    assert {s.rule for s in statements} == {
        "chemical-treats-disease",
        "chemical-affects-gene",
        "gene-disease",
    }
    assert TP53 not in entities and SEIZURE not in entities
    assert report["rules"]["gene-gene"]["dropped_by_cap"] == {
        "pairs": 1,
        "statements": 2,
        "edges": 2,
    }
    assert report["rules"]["gene-phenotype"]["statements"] == 0
    assert report["rules"]["gene-disease"]["statements"] == 1


def test_min_score_drops_low_and_unscored_capped_statements(toy_graph, tmp_path):
    report, statements, pairs, _ = _run(toy_graph, tmp_path / "work", min_score=0.9)
    assert GENE_PHENOTYPE not in pairs  # no score
    gene_gene = [s for s in statements if s.rule == "gene-gene"]
    assert [s.score for s in gene_gene] == [0.999]
    assert pairs[GENE_GENE].statement_ids == [gene_gene[0].statement_id]
    assert report["rules"]["gene-gene"]["dropped_by_min_score"] == {"statements": 1, "edges": 1}
    assert report["rules"]["gene-phenotype"]["dropped_by_min_score"]["statements"] == 1
    # Query rules have no score and are untouched.
    assert pair_id(IMATINIB, CML) in pairs and pair_id(ABL1, CML) in pairs


def _graph(directory: Path, nodes: list[Node], edges: list[Edge]) -> Path:
    write_jsonl(directory / "nodes.jsonl", nodes)
    write_jsonl(directory / "edges.jsonl", edges)
    return directory


def _edge(subject, predicate, object, **kw) -> Edge:
    fields = dict(
        primary_knowledge_source="infores:string",
        knowledge_level="knowledge_assertion",
        agent_type="automated_agent",
    )
    return Edge(subject=subject, predicate=predicate, object=object, **{**fields, **kw})


def test_capped_rule_keeps_best_scored_pairs(tmp_path):
    genes = [Node(id=f"NCBIGene:{i}", category=["biolink:Gene"], name=f"G{i}") for i in range(5)]
    edges = [
        _edge("NCBIGene:0", "biolink:interacts_with", "NCBIGene:1", score=0.5),
        _edge("NCBIGene:0", "biolink:interacts_with", "NCBIGene:2", combined_score=0.9),
        _edge("NCBIGene:0", "biolink:interacts_with", "NCBIGene:3", score=True),  # not a score
        # Step 6's literature support, not a source's confidence.
        _edge("NCBIGene:1", "biolink:interacts_with", "NCBIGene:3", lit_support_score=9),
        _edge("NCBIGene:4", "biolink:physically_interacts_with", "NCBIGene:0", score=0.7),
        _edge("NCBIGene:0", "biolink:physically_interacts_with", "NCBIGene:4", score=0.2),
    ]
    graph = _graph(tmp_path / "g", genes, edges)
    report, statements, pairs, _ = _run(graph, tmp_path / "work", capped_limit=2)
    assert set(pairs) == {pair_id("NCBIGene:0", "NCBIGene:2"), pair_id("NCBIGene:0", "NCBIGene:4")}
    # The 0-4 pair keeps both directions; its best score is 0.7.
    assert len(pairs[pair_id("NCBIGene:0", "NCBIGene:4")].statement_ids) == 2
    assert report["rules"]["gene-gene"]["dropped_by_cap"]["pairs"] == 3
    assert all(s.score is not None for s in statements)


def test_capped_ties_go_to_more_sources_then_a_hash_of_the_pair(tmp_path):
    """Unscored pairs are kept by the number of sources among the rule's statements in them,
    then by the SHA-256 of the pair id: by the pair id alone, an unscored cap would keep the
    CURIEs that sort first."""
    genes = [Node(id=f"NCBIGene:{i}", category=["biolink:Gene"], name=f"G{i}") for i in range(10)]
    hub = "NCBIGene:9"
    edges = [_edge(f"NCBIGene:{i}", "biolink:interacts_with", hub) for i in range(1, 7)]
    # The same statement from a second source.
    edges.append(
        _edge("NCBIGene:6", "biolink:interacts_with", hub, primary_knowledge_source="infores:x")
    )
    graph = _graph(tmp_path / "g", genes, edges)
    report, _, pairs, _ = _run(graph, tmp_path / "work", capped_limit=3)
    singles = [pair_id(f"NCBIGene:{i}", hub) for i in range(1, 6)]
    by_hash = sorted(singles, key=lambda p: hashlib.sha256(p.encode()).hexdigest())
    assert by_hash[:2] != sorted(singles)[:2]
    assert set(pairs) == {pair_id("NCBIGene:6", hub), *by_hash[:2]}
    assert report["rules"]["gene-gene"]["dropped_by_cap"]["pairs"] == 3
    # Scored pairs come first, best first, and the sources break a tie in score too.
    ranks = {
        "a|b": scope.cap_rank("a|b", 0.8, 1),
        "x|y": scope.cap_rank("x|y", 0.8, 2),
        "m|n": scope.cap_rank("m|n", 0.9, 1),
        "c|d": scope.cap_rank("c|d", None, 5),
    }
    assert sorted(ranks, key=ranks.__getitem__) == ["m|n", "x|y", "a|b", "c|d"]


def test_symmetric_predicate_matches_reversed_and_missing_nodes(tmp_path):
    nodes = [
        Node(id="CHEBI:1", category=["biolink:SmallMolecule"], name="c"),
        Node(id="UniProtKB:P1", category=["biolink:Protein"], name="p", symbol="PRT1"),
        Node(id="MONDO:1", category=["biolink:Disease"], name="d"),
    ]
    edges = [
        _edge("UniProtKB:P1", "biolink:binds", "CHEBI:1"),  # reversed, symmetric predicate
        _edge("MONDO:1", "biolink:affects", "CHEBI:1"),  # matches no rule
        _edge("CHEBI:1", "biolink:affects", "NCBIGene:404"),  # object not in nodes.jsonl
    ]
    graph = _graph(tmp_path / "g", nodes, edges)
    report, statements, pairs, entities = _run(graph, tmp_path / "work")
    assert [(s.rule, s.subject) for s in statements] == [("chemical-affects-gene", "UniProtKB:P1")]
    assert entities["UniProtKB:P1"].synonyms == ["PRT1"]
    assert report["missing_node_skipped"] == 1
    assert report["skipped"] == [
        {
            "subject_category": "biolink:Disease",
            "predicate": "biolink:affects",
            "object_category": "biolink:SmallMolecule",
            "edges": 1,
            "rule": "unmatched",
        }
    ]


def test_text_mined_edges_are_capped_whatever_their_predicate(tmp_path):
    """eRAM writes a co-mention as `gene_associated_with_condition`, under the gene-disease
    rule's `associated_with`; its knowledge level and agent type say what it is. The default
    provenance rules cap such edges, and a rules file can skip them instead."""
    nodes = [
        Node(id="NCBIGene:1", category=["biolink:Gene"], name="g"),
        Node(id="NCBIGene:2", category=["biolink:Gene"], name="h"),
        Node(id="MONDO:1", category=["biolink:Disease"], name="d"),
        Node(id="CHEBI:1", category=["biolink:SmallMolecule"], name="c"),
    ]
    associated = "biolink:gene_associated_with_condition"
    mined = dict(knowledge_level="text_co_occurrence", agent_type="text_mining_agent")
    read = dict(knowledge_level="statistical_association", agent_type="text_mining_agent")
    edges = [
        _edge("NCBIGene:1", associated, "MONDO:1", **mined),
        _edge("NCBIGene:1", associated, "MONDO:1", **read),
        _edge(
            "NCBIGene:1",
            associated,
            "MONDO:1",
            knowledge_level="knowledge_assertion",
            agent_type="manual_agent",
        ),
        _edge("CHEBI:1", "biolink:treats", "MONDO:1", **mined),
        _edge("NCBIGene:2", associated, "MONDO:1", **read),
    ]
    graph = _graph(tmp_path / "g", nodes, edges)
    report, statements, pairs, _ = _run(graph, tmp_path / "work")
    by_subject = {s.subject: s for s in statements}
    # The curated assertion keeps its statement in the query tier, with the mined edges counted.
    curated = by_subject["NCBIGene:1"]
    assert (curated.rule, curated.provenance_rule, curated.edge_count) == ("gene-disease", None, 3)
    # Mined alone, a statement is capped by the first provenance rule its edges match.
    treats, read_only = by_subject["CHEBI:1"], by_subject["NCBIGene:2"]
    assert (treats.rule, treats.provenance_rule) == (
        "chemical-treats-disease",
        "text-co-occurrence",
    )
    assert (read_only.rule, read_only.provenance_rule) == ("gene-disease", "text-mined")
    assert pairs[treats.pair_id].rules == ["chemical-treats-disease"]
    rules = report["rules"]
    assert {k: rules["text-co-occurrence"][k] for k in ("tier", "edges", "pairs")} == {
        "tier": "capped",
        "edges": 1,
        "pairs": 1,
    }
    assert (rules["text-mined"]["matched_edges"], rules["text-mined"]["statements"]) == (1, 1)
    assert (rules["gene-disease"]["matched_edges"], rules["gene-disease"]["statements"]) == (3, 1)
    assert rules["chemical-treats-disease"]["statements"] == 0
    assert report["skipped"] == []
    assert any(line.startswith("  text-co-occurrence: 1 edges") for line in scope.summary(report))

    # Set to skip, the provenance rules skip what they match, as the defaults did before.
    as_skips = [
        r.model_dump(mode="json") | ({"tier": "skip"} if r.is_provenance else {})
        for r in scope.DEFAULT_RULES
    ]
    rules_file = _rules_file(tmp_path, as_skips)
    report, statements, _, _ = _run(graph, tmp_path / "work2", rules_file=rules_file)
    [kept] = statements
    assert (kept.rule, kept.edge_count, kept.provenance_rule) == ("gene-disease", 1, None)
    assert report["rules"]["text-co-occurrence"] == {"tier": "skip", "skipped_edges": 2}
    assert report["rules"]["text-mined"] == {"tier": "skip", "skipped_edges": 2}
    assert {(e["predicate"], e["rule"], e["edges"]) for e in report["skipped"]} == {
        (associated, "skip:text-co-occurrence", 1),
        (associated, "skip:text-mined", 2),
        ("biolink:treats", "skip:text-co-occurrence", 1),
    }
    assert "4 by skip rules" in scope.summary(report)[-1]


def test_a_text_mined_gene_disease_edge_is_capped_as_gene_disease(tmp_path):
    """The provenance rule sets the edge's tier, and its statement rule what its pair's
    claims may say: the same predicates as for a curated gene-disease edge."""
    nodes = [
        Node(id="NCBIGene:1", category=["biolink:Gene"], name="g"),
        Node(id="MONDO:1", category=["biolink:Disease"], name="d"),
    ]
    edge = _edge("NCBIGene:1", "biolink:gene_associated_with_condition", "MONDO:1", **MINED)
    report, [s], pairs, _ = _run(_graph(tmp_path / "g", nodes, [edge]), tmp_path / "work")
    assert (s.rule, s.provenance_rule) == ("gene-disease", "text-co-occurrence")
    pair = pairs[s.pair_id]
    assert pair.rules == ["gene-disease"]
    claimed = label.predicates_of(scope.DEFAULT_RULES, pair.rules)
    assert claimed and claimed == label.predicates_of(scope.DEFAULT_RULES, ["gene-disease"])
    assert report["rules"]["text-co-occurrence"]["pairs"] == 1
    assert report["rules"]["gene-disease"]["matched_statements"] == 0


def test_match_rule_honours_knowledge_level_and_agent_type():
    rules = (
        scope.Rule(
            id="curated",
            tier="query",
            predicates=("biolink:treats",),
            knowledge_level=("knowledge_assertion",),
            agent_type=("manual_agent",),
        ),
        scope.Rule(id="rest", tier="skip", predicates=("biolink:treats",)),
    )
    chem, disease = ("biolink:SmallMolecule",), ("biolink:Disease",)

    def match(level, agent):
        return scope.match_rule(rules, chem, "biolink:treats", disease, level, agent).id

    assert match("knowledge_assertion", "manual_agent") == "curated"
    # Both must hold: a rule's fields are all conditions, like its categories.
    assert match("knowledge_assertion", "text_mining_agent") == "rest"
    assert match("prediction", "manual_agent") == "rest"
    assert match(None, None) == "rest"


def test_a_provenance_rule_lowers_the_tier_its_statement_rule_set():
    """A provenance rule picks no edge itself; it applies to the edges a statement rule keeps,
    when its tier is no higher, and its place among the statement rules doesn't matter."""
    gene_disease = scope.Rule(
        id="gene-disease",
        tier="query",
        subject=("biolink:Gene",),
        predicates=("biolink:associated_with",),
        object=("biolink:Disease",),
    )
    gene_gene = scope.Rule(
        id="gene-gene", tier="capped", predicates=("biolink:interacts_with",), symmetric=True
    )
    go = scope.Rule(id="go", tier="skip", predicates=("biolink:located_in",))
    mined = scope.Rule(id="mined", tier="capped", knowledge_level=("text_co_occurrence",))
    raise_ = scope.Rule(id="raise", tier="query", agent_type=("text_mining_agent",))
    gene, disease, cc = ("biolink:Gene",), ("biolink:Disease",), ("biolink:CellularComponent",)
    mined_edge = ("text_co_occurrence", "text_mining_agent")

    def decide(rules, subject, predicate, object, made=mined_edge):
        statement, lowered = scope.decide(rules, subject, predicate, object, *made)
        return (statement.id if statement else None, lowered.id if lowered else None)

    associated = (gene, "biolink:gene_associated_with_condition", disease)
    for rules in [(mined, gene_disease, go), (gene_disease, go, mined)]:
        assert decide(rules, *associated) == ("gene-disease", "mined")
        assert decide(rules, *associated, made=("knowledge_assertion", None)) == (
            "gene-disease",
            None,
        )
        # A skip statement rule keeps its own name in the report.
        assert decide(rules, gene, "biolink:located_in", cc) == ("go", None)
        # No statement rule: unmatched, whatever the provenance.
        assert decide(rules, disease, "biolink:affects", gene) == (None, None)
        assert scope.match_rule(rules, *associated, *mined_edge) is gene_disease
    # A provenance rule never raises a tier, and the first one that matches decides.
    rules = (raise_, mined, gene_gene)
    assert decide(rules, gene, "biolink:interacts_with", gene) == ("gene-gene", None)
    rules = (mined, raise_, gene_gene)
    assert decide(rules, gene, "biolink:interacts_with", gene) == ("gene-gene", "mined")


def _rules_file(directory: Path, rules: list[dict]) -> Path:
    path = directory / "rules.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rules))
    return path


GENE_DISEASE_RULE = {
    "id": "gene-disease",
    "tier": "query",
    "subject": ["biolink:GeneOrGeneProduct"],
    "predicates": ["biolink:associated_with"],
    "object": ["biolink:Disease"],
}
# Placed after the statement rule: a provenance rule's place among them doesn't matter.
MINED_RULES = [
    GENE_DISEASE_RULE,
    {"id": "mined", "tier": "capped", "knowledge_level": ["text_co_occurrence"]},
]
MINED = dict(
    primary_knowledge_source="infores:eram",
    knowledge_level="text_co_occurrence",
    agent_type="text_mining_agent",
)
CURATED = dict(
    primary_knowledge_source="infores:omim",
    knowledge_level="knowledge_assertion",
    agent_type="manual_agent",
)


def test_a_statement_with_curated_and_mined_edges_takes_the_higher_tier(tmp_path):
    nodes = [
        Node(id="NCBIGene:1", category=["biolink:Gene"], name="g"),
        Node(id="MONDO:1", category=["biolink:Disease"], name="d"),
    ]
    predicate = "biolink:gene_associated_with_condition"
    edges = [
        _edge("NCBIGene:1", predicate, "MONDO:1", score=0.3, **MINED),
        _edge("NCBIGene:1", predicate, "MONDO:1", publications=["PMID:7"], **CURATED),
    ]
    graph = _graph(tmp_path / "g", nodes, edges)
    rules = _rules_file(tmp_path, MINED_RULES)
    report, statements, pairs, _ = _run(graph, tmp_path / "work", rules_file=rules)
    [s] = statements
    assert (s.rule, s.provenance_rule) == ("gene-disease", None)
    # Both edges count, the mined one included.
    assert (s.edge_count, s.sources, s.source_pmids, s.score) == (
        2,
        ["infores:eram", "infores:omim"],
        ["7"],
        0.3,
    )
    assert pairs[s.pair_id].rules == ["gene-disease"]
    gene_disease = report["rules"]["gene-disease"]
    assert (gene_disease["matched_edges"], gene_disease["edges"]) == (2, 2)
    assert report["rules"]["mined"]["matched_statements"] == 0
    # Without the curated edge, the statement is the provenance rule's, still under its own
    # statement rule.
    graph = _graph(tmp_path / "g2", nodes, edges[:1])
    _, [s], _, _ = _run(graph, tmp_path / "work2", rules_file=rules)
    assert (s.rule, s.provenance_rule, s.edge_count) == ("gene-disease", "mined", 1)


def test_a_capped_provenance_rule_keeps_its_best_scored_pairs(tmp_path):
    """The mined pairs have a cap of their own, ranked by score, and a pair the query rule
    keeps too takes a place in it: its mined statements widen what review sends."""
    genes = [Node(id=f"NCBIGene:{i}", category=["biolink:Gene"], name=f"G{i}") for i in range(5)]
    disease = Node(id="MONDO:1", category=["biolink:Disease"], name="d")
    associated = "biolink:gene_associated_with_condition"
    edges = [
        _edge("NCBIGene:1", associated, "MONDO:1", score=0.9, **MINED),
        _edge("NCBIGene:2", associated, "MONDO:1", score=0.5, **MINED),
        _edge("NCBIGene:3", associated, "MONDO:1", score=0.7, **MINED),
        # Pair 4 has a curated statement, and a mined one with the best score.
        _edge("NCBIGene:4", associated, "MONDO:1", **CURATED),
        _edge("NCBIGene:4", "biolink:correlated_with", "MONDO:1", score=0.95, **MINED),
    ]
    graph = _graph(tmp_path / "g", [*genes, disease], edges)
    rules = _rules_file(tmp_path, MINED_RULES)
    report, statements, pairs, _ = _run(graph, tmp_path / "work", rules_file=rules, capped_limit=2)
    kept = {pair_id(f"NCBIGene:{i}", "MONDO:1") for i in (1, 4)}
    assert set(pairs) == kept
    four = pairs[pair_id("NCBIGene:4", "MONDO:1")]
    assert len(four.statement_ids) == 2 and four.rules == ["gene-disease"]
    assert {(s.rule, s.provenance_rule) for s in statements} == {
        ("gene-disease", None),
        ("gene-disease", "mined"),
    }
    assert report["rules"]["mined"] == {
        "tier": "capped",
        "matched_edges": 4,
        "matched_statements": 4,
        "edges": 2,
        "statements": 2,
        "pairs": 2,
        "dropped_by_min_score": {"statements": 0, "edges": 0},
        "dropped_by_cap": {"pairs": 2, "statements": 2, "edges": 2},
    }
    # The statement rule's counts leave out what the provenance rule took.
    gene_disease = report["rules"]["gene-disease"]
    assert (gene_disease["matched_edges"], gene_disease["statements"]) == (1, 1)
    assert any(line.startswith("  mined: 2 edges") for line in scope.summary(report))
    # --min-score applies to a provenance rule's cap too, and never to a query rule.
    report, _, pairs, _ = _run(graph, tmp_path / "work2", rules_file=rules, min_score=0.8)
    assert report["rules"]["mined"]["dropped_by_min_score"] == {"statements": 2, "edges": 2}
    assert set(pairs) == kept


def test_a_text_mined_go_edge_is_skipped_by_its_statement_rule(tmp_path):
    """A skip statement rule names the edges it skips whatever their provenance."""
    nodes = [
        Node(id="NCBIGene:1", category=["biolink:Gene"], name="g"),
        Node(id="GO:0005634", category=["biolink:CellularComponent"], name="nucleus"),
    ]
    edge = _edge("NCBIGene:1", "biolink:located_in", "GO:0005634", **MINED)
    report, statements, _, _ = _run(_graph(tmp_path / "g", nodes, [edge]), tmp_path / "work")
    assert statements == []
    [skipped] = report["skipped"]
    assert (skipped["rule"], skipped["edges"]) == ("skip:pathway-go", 1)
    assert report["rules"]["pathway-go"] == {"tier": "skip", "skipped_edges": 1}


def test_rules_file_can_match_on_provenance(toy_graph, tmp_path):
    rules = tmp_path / "rules.json"
    rules.write_text(
        json.dumps(
            [
                {"id": "automated", "tier": "skip", "agent_type": ["automated_agent"]},
                {
                    "id": "gene-disease",
                    "tier": "query",
                    "subject": ["biolink:GeneOrGeneProduct"],
                    "predicates": ["biolink:associated_with"],
                    "object": ["biolink:Disease"],
                },
            ]
        )
    )
    report, statements, _, _ = _run(toy_graph, tmp_path / "work", rules_file=rules)
    # The toy gene-disease edge is DisGeNET's, by an automated agent.
    assert statements == []
    assert report["rules"]["automated"] == {"tier": "skip", "skipped_edges": 1}
    assert report["rules"]["gene-disease"]["matched_edges"] == 0


def test_misdirected_edges_are_kept_and_counted(tmp_path, capsys):
    """Claims are read in the subject and object order Biolink's domain and range give; a
    source edge that isn't is reported, not dropped."""
    nodes = [
        Node(id="NCBIGene:1", category=["biolink:Gene"], name="g"),
        Node(id="MONDO:1", category=["biolink:Disease"], name="d"),
        Node(id="MONDO:2", category=["biolink:Disease"], name="e"),
    ]
    edges = [
        _edge("NCBIGene:1", "biolink:condition_associated_with_gene", "MONDO:1"),
        _edge("NCBIGene:1", "biolink:gene_associated_with_condition", "MONDO:2"),
    ]
    graph = _graph(tmp_path / "g", nodes, edges)
    work = tmp_path / "work"
    report, statements, _, _ = _run(graph, work)
    assert {s.predicate for s in statements} == {e.predicate for e in edges}
    assert report["kept"]["misdirected_edges"] == 1
    args = argparse.Namespace(input=graph, work=work)
    assert scope.run(args) == 0
    assert "warning: 1 kept edges have a predicate" in capsys.readouterr().err


def test_entities_have_synonyms_without_their_name(toy_graph, tmp_path):
    _, _, _, entities = _run(toy_graph, tmp_path / "work")
    assert entities[IMATINIB].synonyms == ["Gleevec", "STI571", "STI-571"]
    assert entities[IMATINIB].equivalent_identifiers[1] == "MESH:D000068877"
    assert entities[ABL1].synonyms == ["ABL proto-oncogene 1, non-receptor tyrosine kinase"]
    assert entities[CML].synonyms == ["chronic myeloid leukemia", "CML"]
    assert entities[MDM2].synonyms == []
    assert entities[CML].category == ["biolink:Disease"]


def test_synonyms_of_handles_strings_lists_and_case():
    slots = {"symbol": "abl1", "synonym": "Abl", "full_name": ["ABL1", " c-Abl "]}
    assert scope.synonyms_of(slots, "ABL1") == ["Abl", "c-Abl"]


def test_outputs_are_deterministic(toy_graph, tmp_path):
    scope.select(toy_graph, tmp_path / "a")
    scope.select(toy_graph, tmp_path / "b")
    for name in (records.ENTITIES, records.STATEMENTS, records.PAIRS, records.SCOPE_REPORT):
        a = (tmp_path / "a" / name).read_text()
        b = (tmp_path / "b" / name).read_text()
        assert a.replace(str(tmp_path / "a"), "") == b.replace(str(tmp_path / "b"), "")
    pairs = [json.loads(line)["pair_id"] for line in (tmp_path / "a" / records.PAIRS).open()]
    assert pairs == sorted(pairs)


def test_rules_file_replaces_the_defaults(toy_graph, tmp_path):
    rules = tmp_path / "rules.json"
    rule = {
        "id": "only-treats",
        "tier": "query",
        "subject": ["biolink:ChemicalEntity"],
        "predicates": ["biolink:treats"],
        "object": ["biolink:Disease"],
    }
    rules.write_text(json.dumps({"rules": [rule]}))
    report, statements, _, _ = _run(toy_graph, tmp_path / "work", rules_file=rules)
    assert {s.rule for s in statements} == {"only-treats"}
    assert report["rules_file"] == str(rules)
    assert {e["rule"] for e in report["skipped"]} == {"unmatched"}


@pytest.mark.parametrize(
    ("content", "problem"),
    [
        ("not json", "can't read"),
        ("[]", "non-empty JSON list"),
        ('[{"id": "x", "tier": "maybe", "predicates": ["biolink:treats"]}]', "tier"),
        ('[{"id": "x", "tier": "query", "predicates": ["biolink:cures"]}]', "not in Biolink"),
        ('[{"id": "x", "tier": "skip", "knowledge_level": ["text_mined"]}]', "knowledge_level"),
        ('[{"id": "x", "tier": "skip", "agent_type": ["robot"]}]', "agent_type"),
        ('[{"id": "x", "tier": "skip"}]', "needs predicates"),
        # A provenance rule (no predicates) picks no edges, so it can't name what they join.
        (
            '[{"id": "x", "tier": "capped", "agent_type": ["text_mining_agent"],'
            ' "subject": ["biolink:Gene"]}]',
            "provenance rule .* can't set subject, object or symmetric",
        ),
        (
            '[{"id": "x", "tier": "skip", "knowledge_level": ["text_co_occurrence"],'
            ' "symmetric": true}]',
            "provenance rule",
        ),
    ],
)
def test_bad_rules_file(tmp_path, content, problem):
    path = tmp_path / "rules.json"
    path.write_text(content)
    with pytest.raises(scope.RulesError, match=problem):
        scope.load_rules(path)


def _args(**kw) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    scope.add_arguments(parser)
    args = parser.parse_args([])
    for key, value in kw.items():
        setattr(args, key, value)
    return args


def test_add_arguments_defaults():
    args = _args()
    assert args.capped_limit == config.CAPPED_LIMIT
    assert args.min_score is None
    assert args.rules is None


def test_run_writes_work_files_and_summary(toy_graph, tmp_path, capsys):
    work = tmp_path / "work"
    assert scope.run(_args(input=toy_graph, output=tmp_path / "out", work=work)) == 0
    out = capsys.readouterr().out
    assert out.startswith("select: read 12 edges")
    assert all(line.startswith("select: ") for line in out.splitlines())
    for name in (records.ENTITIES, records.STATEMENTS, records.PAIRS, records.SCOPE_REPORT):
        assert (work / name).exists()


def test_run_fails_cleanly(tmp_path, toy_graph, capsys):
    assert scope.run(_args(input=tmp_path / "missing", work=tmp_path / "work")) == 1
    assert "no edges.jsonl" in capsys.readouterr().err
    bad = tmp_path / "rules.json"
    bad.write_text("[]")
    assert scope.run(_args(input=toy_graph, work=tmp_path / "work", rules=bad)) == 1
    assert "select: rules file" in capsys.readouterr().err


def test_reads_orion_file_names(tmp_path):
    graph = tmp_path / "orion"
    write_jsonl(graph / "normalized_nodes.jsonl", NODES)
    write_jsonl(graph / "normalized_edges.jsonl.gz", EDGES)
    report = scope.select(graph, tmp_path / "work")
    assert report["edges_read"] == len(EDGES)


@pytest.mark.parametrize(
    "node, name, synonyms",
    [
        # Xenbase names genes by their own id; the symbol is the real name.
        (
            Node(
                id="Xenbase:XB-GENE-865198",
                category=["biolink:Gene"],
                name="XB-GENE-865198",
                symbol="nog4.S",
            ),
            "nog4.S",
            [],
        ),
        # dictyBase phenotypes are often named by their number alone.
        (
            Node(id="DDPHENO:0001015", category=["biolink:PhenotypicFeature"], name="0001015"),
            None,
            [],
        ),
        (Node(id="NCBIGene:25", category=["biolink:Gene"], name="ABL1", symbol="ABL1"), "ABL1", []),
        (
            Node(id="X:1", category=["biolink:Gene"], name="X:1", synonym=["alpha", "beta"]),
            "alpha",
            ["beta"],
        ),
    ],
)
def test_identifier_names_are_not_names(node, name, synonyms):
    entity = scope.entity_of(node)
    assert (entity.name, entity.synonyms) == (name, synonyms)


def test_obsolete_prefix_is_dropped_from_names():
    node = Node(id="MONDO:0017288", category=["biolink:Disease"], name="obsolete DICER1 syndrome")
    assert scope.entity_of(node).name == "DICER1 syndrome"
