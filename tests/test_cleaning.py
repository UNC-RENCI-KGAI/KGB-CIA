import json

import pytest

from agents.cleaning import garbage
from agents.cleaning.cli import main

RESOLVED = ["biolink:Protein", "biolink:NamedThing"]


def _node(node_id, resolved=True):
    return {
        "id": node_id,
        "category": RESOLVED if resolved else ["biolink:NamedThing"],
        "equivalent_identifiers": [node_id] if resolved else [],
    }


def _edge(subject, obj, **kw):
    edge = {
        "subject": subject,
        "predicate": "biolink:interacts_with",
        "object": obj,
        "primary_knowledge_source": "infores:test",
        "knowledge_level": "knowledge_assertion",
        "agent_type": "manual_agent",
        "kgbcia_source_records": ["m#1"],
    }
    edge.update(kw)
    return edge


def _write(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def _read(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def graph(tmp_path):
    src = tmp_path / "merged"
    src.mkdir()
    _write(
        src / "nodes.jsonl",
        [
            _node("UniProtKB:A"),
            _node("UniProtKB:B"),
            _node("UniProtKB:C"),
            _node("CHEMBL.TARGET:X", resolved=False),
            _node("UniProtKB:ORPHAN"),
        ],
    )
    _write(
        src / "edges.jsonl",
        [
            _edge("UniProtKB:A", "UniProtKB:B"),
            _edge("UniProtKB:B", "UniProtKB:C", knowledge_level="not_provided"),
            _edge("UniProtKB:A", "UniProtKB:A"),
            _edge("UniProtKB:A", "UniProtKB:MISSING"),
            _edge("CHEMBL.TARGET:X", "UniProtKB:A"),
            _edge("UniProtKB:A", "UniProtKB:C", primary_knowledge_source=""),
            {"subject": "UniProtKB:A", "object": "UniProtKB:B"},
        ],
    )
    return src


def test_collect_drops_and_flags(graph, tmp_path):
    out = tmp_path / "clean"
    report = garbage.collect(graph, out)

    assert report.edges_in == 7 and report.edges_out == 3
    assert report.dropped_edges == {
        "malformed": 1,
        "no_primary_knowledge_source": 1,
        "dangling": 1,
        "unresolved_node": 1,
    }
    assert report.flags == {"self_loop": 1, "knowledge_level_not_provided": 1}
    assert report.dropped_nodes == {"unresolved": 1, "orphan": 1}
    assert report.unresolved_nodes_by_prefix == {"CHEMBL.TARGET": 1}

    kept_nodes = {n["id"] for n in _read(out / "nodes.jsonl")}
    assert kept_nodes == {"UniProtKB:A", "UniProtKB:B", "UniProtKB:C"}
    edges = _read(out / "edges.jsonl")
    assert all(e["kgbcia_source_records"] == ["m#1"] for e in edges)

    written = json.loads((out / "cleaning_report.json").read_text())
    assert written["edges"] == {"in": 7, "out": 3}
    assert written["dropped_edges_by_source"]["infores:test"]["dangling"] == 1


def test_attention_names_sources_losing_most_edges(tmp_path):
    src = tmp_path / "merged"
    src.mkdir()
    _write(src / "nodes.jsonl", [_node("UniProtKB:A"), _node("ko:K1", resolved=False)])
    _write(
        src / "edges.jsonl",
        [
            _edge("ko:K1", "UniProtKB:A", primary_knowledge_source="infores:kegg"),
            _edge(
                "ko:K1",
                "UniProtKB:A",
                primary_knowledge_source="infores:kegg",
                predicate="biolink:related_to",
            ),
            _edge("UniProtKB:A", "UniProtKB:A"),
        ],
    )
    report = garbage.collect(src, tmp_path / "clean")
    [kegg] = report.attention()
    assert kegg["source"] == "infores:kegg" and kegg["share_dropped"] == 1.0
    assert kegg["unresolved_prefixes"] == {"ko": 2}
    assert "infores:kegg: 100% of 2 edges dropped; unresolved prefixes: ko" in garbage.summary(
        report
    )


def test_drop_lists_folder_matches_on_edge_key_not_id(graph, tmp_path):
    out = tmp_path / "clean"
    (out / "drop_lists").mkdir(parents=True)
    _write(
        out / "drop_lists" / "evidence_check.jsonl",
        [{**_edge("UniProtKB:A", "UniProtKB:B"), "id": "uuid-from-another-build"}],
    )
    report = garbage.collect(graph, out)
    assert report.dropped_edges["drop_list:evidence_check"] == 1
    assert report.edges_out == 2
    assert report.settings["drop_lists"] == {"evidence_check": 1}


def test_drop_list_line_needs_full_key(tmp_path):
    drop = tmp_path / "bad.jsonl"
    _write(drop, [{"subject": "UniProtKB:A", "object": "UniProtKB:B"}])
    with pytest.raises(ValueError, match="bad.jsonl:1"):
        garbage.load_drop_list(drop)


def test_cli_without_input_explains(tmp_path, capsys):
    assert main(["--input", str(tmp_path / "nowhere"), "--output", str(tmp_path / "o")]) == 1
    assert "run the transformation stage first" in capsys.readouterr().out


def test_cli_runs(graph, tmp_path, capsys):
    assert main(["--input", str(graph), "--output", str(tmp_path / "clean")]) == 0
    assert "Edges: 7 in, 3 out" in capsys.readouterr().out


def test_type_violation_flags_treats_to_procedure(tmp_path):
    nodes = [
        {
            "id": "CHEBI:1",
            "category": ["biolink:SmallMolecule", "biolink:ChemicalEntity", "biolink:NamedThing"],
            "equivalent_identifiers": ["CHEBI:1"],
        },
        {
            "id": "MONDO:1",
            "category": [
                "biolink:Disease",
                "biolink:DiseaseOrPhenotypicFeature",
                "biolink:NamedThing",
            ],
            "equivalent_identifiers": ["MONDO:1"],
        },
        {
            "id": "UMLS:P",
            "category": ["biolink:Procedure", "biolink:NamedThing"],
            "equivalent_identifiers": ["UMLS:P"],
        },
        {
            "id": "NCBIGene:1",
            "category": ["biolink:Gene", "biolink:NamedThing"],
            "equivalent_identifiers": ["NCBIGene:1"],
        },
        {
            "id": "REACT:1",
            "category": ["biolink:Pathway", "biolink:NamedThing"],
            "equivalent_identifiers": ["REACT:1"],
        },
    ]
    edges = [
        _edge("CHEBI:1", "MONDO:1", predicate="biolink:treats"),
        _edge("CHEBI:1", "UMLS:P", predicate="biolink:treats"),
        _edge("CHEBI:1", "MONDO:1", predicate="biolink:contraindicated_in"),
        _edge("NCBIGene:1", "REACT:1", predicate="biolink:participates_in"),
        _edge("MONDO:1", "MONDO:1", predicate="biolink:subclass_of"),
    ]
    graph = tmp_path / "g"
    graph.mkdir()
    _write(graph / "nodes.jsonl", nodes)
    _write(graph / "edges.jsonl", edges)
    report = garbage.collect(graph, tmp_path / "out")
    assert report.edges_out == 5, "type violations are flags, not drops"
    assert report.flags["type_violation"] == 2
    assert report.type_violations_by_predicate == {
        "biolink:treats": 1,
        "biolink:participates_in": 1,
    }
    written = json.loads((tmp_path / "out" / "cleaning_report.json").read_text())
    assert written["opposing_predicates"] == [
        {
            "subject": "CHEBI:1",
            "object": "MONDO:1",
            "predicates": ["biolink:treats", "biolink:contraindicated_in"],
        }
    ]
    assert written["flags"]["opposing_predicate"] == 2
    assert written["rare_category_combinations"] == []  # too few edges per predicate


def test_rare_category_combination(tmp_path):
    nodes = [
        {
            "id": f"X:{i}",
            "category": ["biolink:Gene", "biolink:NamedThing"],
            "equivalent_identifiers": [f"X:{i}"],
        }
        for i in range(3)
    ]
    nodes.append(
        {
            "id": "T:1",
            "category": ["biolink:Procedure", "biolink:NamedThing"],
            "equivalent_identifiers": ["T:1"],
        }
    )
    edges = [_edge("X:0", "X:1", predicate="biolink:interacts_with") for _ in range(1500)]
    edges.append(_edge("X:2", "T:1", predicate="biolink:interacts_with"))
    graph = tmp_path / "g"
    graph.mkdir()
    _write(graph / "nodes.jsonl", nodes)
    _write(graph / "edges.jsonl", edges)
    report = garbage.collect(graph, tmp_path / "out")
    rare = report.rare_combinations()
    assert [(c["subject_category"], c["object_category"], c["edges"]) for c in rare] == [
        ("biolink:Gene", "biolink:Procedure", 1)
    ]
    assert rare[0]["example"] == ("X:2", "biolink:interacts_with", "T:1")


def test_violation_rules():
    from agents.cleaning import biolink_types as bt

    disease, procedure = ["biolink:Disease", "biolink:NamedThing"], ["biolink:Procedure"]
    chemical = ["biolink:SmallMolecule", "biolink:ChemicalEntity"]
    assert bt.violation("biolink:treats", chemical, disease) is None
    assert bt.violation("biolink:treats", chemical, procedure) == "range"
    assert bt.violation("biolink:treats", disease, disease) == "domain"
    assert bt.violation("biolink:related_to", procedure, procedure) is None  # named thing
    # Biolink stricter than the graph: reported by predicate, never dropped
    assert (
        bt.violation("biolink:participates_in", ["biolink:Gene"], ["biolink:Pathway"]) == "domain"
    )
    assert bt.violation("biolink:subclass_of", disease, disease) is None  # ontology class
    assert bt.violation("biolink:not_real", disease, disease) is None
    assert bt.violation("biolink:treats", [], []) is None  # unknown categories aren't judged
