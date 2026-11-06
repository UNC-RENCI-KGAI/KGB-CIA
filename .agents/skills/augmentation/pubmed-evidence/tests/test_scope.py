"""Step 1: which edges are kept, how they become statements and pairs, and the report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
from pubmed_evidence import config, graphio, records, scope
from pubmed_evidence.graphio import pair_id, write_jsonl
from pubmed_evidence.records import Entity, Pair, Statement
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
    statements = list(records.read_records(work / records.STATEMENTS, Statement))
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
