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
            _node("CHEMBL.TARGET:Y", resolved=False),
            _node("OBA:VT0001253", resolved=False),
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
            _edge("CHEMBL.TARGET:X", "UniProtKB:A"),  # one unresolved end: kept, flagged
            _edge("CHEMBL.TARGET:X", "CHEMBL.TARGET:Y"),  # both ends unresolved: dropped
            _edge("UniProtKB:A", "OBA:VT0001253"),  # a broken id: dropped though A resolves
            _edge("UniProtKB:A", "UniProtKB:C", primary_knowledge_source=""),
            {"subject": "UniProtKB:A", "object": "UniProtKB:B"},
        ],
    )
    return src


def test_collect_drops_and_flags(graph, tmp_path):
    out = tmp_path / "clean"
    report = garbage.collect(graph, out)

    assert report.edges_in == 9 and report.edges_out == 4
    assert report.dropped_edges == {
        "malformed": 1,
        "no_primary_knowledge_source": 1,
        "dangling": 1,
        "unresolved_both_ends": 1,
        "broken_id": 1,
    }
    assert report.flags == {
        "unresolved_node": 1,
        "self_loop": 1,
        "knowledge_level_not_provided": 1,
    }
    # Y and the OBA trait lost every edge; X keeps its edge to A, so it stays
    assert report.dropped_nodes == {"unresolved": 2, "orphan": 1}
    assert report.unresolved_nodes_by_prefix == {"CHEMBL.TARGET": 2, "OBA": 1}

    kept_nodes = {n["id"] for n in _read(out / "nodes.jsonl")}
    assert kept_nodes == {"UniProtKB:A", "UniProtKB:B", "UniProtKB:C", "CHEMBL.TARGET:X"}
    flagged = _read(out / "flagged_edges.jsonl")
    assert [f["flags"] for f in flagged if f["subject"] == "CHEMBL.TARGET:X"] == [
        ["unresolved_node"]
    ]
    edges = _read(out / "edges.jsonl")
    assert all(e["kgbcia_source_records"] == ["m#1"] for e in edges)

    written = json.loads((out / "cleaning_report.json").read_text())
    assert written["edges"] == {"in": 9, "out": 4}
    assert written["dropped_edges_by_source"]["infores:test"]["dangling"] == 1


def test_attention_names_sources_losing_most_edges(tmp_path):
    src = tmp_path / "merged"
    src.mkdir()
    _write(
        src / "nodes.jsonl",
        [_node("UniProtKB:A"), _node("ko:K1", resolved=False), _node("ko:K2", resolved=False)],
    )
    _write(
        src / "edges.jsonl",
        [
            _edge("ko:K1", "ko:K2", primary_knowledge_source="infores:kegg"),
            _edge(
                "ko:K1",
                "ko:K2",
                primary_knowledge_source="infores:kegg",
                predicate="biolink:related_to",
            ),
            _edge("UniProtKB:A", "UniProtKB:A"),
        ],
    )
    report = garbage.collect(src, tmp_path / "clean")
    [kegg] = report.attention()
    assert kegg["source"] == "infores:kegg" and kegg["share_dropped"] == 1.0
    assert kegg["unresolved_prefixes"] == {"ko": 4}  # two unresolved ends per edge
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
    assert report.edges_out == 3
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
    assert "Edges: 9 in, 4 out" in capsys.readouterr().out


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
    # treats -> Procedure is a promoted drop; participates_in's violation stays a flag
    assert report.edges_out == 4
    assert report.dropped_edges == {"type_violation:biolink:treats->biolink:Procedure": 1}
    assert report.flags["type_violation"] == 1
    assert report.type_violations_by_predicate == {
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
    assert written["type_violations_by_category"] == {
        "biolink:participates_in": {"domain: biolink:Gene": 1},
    }
    assert written["dropped_edges"] == {"type_violation:biolink:treats->biolink:Procedure": 1}

    flagged = _read(tmp_path / "out" / "flagged_edges.jsonl")
    assert written["flagged_edges"] == len(flagged) == 4  # 1 violation, 2 opposing, 1 self loop
    assert not any(f["object"] == "UMLS:P" for f in flagged), "a dropped edge is not flagged"
    violation = next(f for f in flagged if "type_violation" in f["flags"])
    assert violation == {
        "subject": "NCBIGene:1",
        "predicate": "biolink:participates_in",
        "object": "REACT:1",
        "primary_knowledge_source": "infores:test",
        "flags": ["type_violation"],
        "subject_categories": ["biolink:Gene"],
        "object_categories": ["biolink:Pathway"],
        "violates": "domain",
    }
    # a flagged line is a valid drop-list line, so a reviewed subset can be promoted to a drop
    drop = tmp_path / "promote.jsonl"
    _write(drop, [violation])
    assert garbage.load_drop_list(drop) == {
        ("NCBIGene:1", "biolink:participates_in", "REACT:1", "infores:test")
    }


def test_opposing_counts_edges_and_includes_narrower_predicates(tmp_path):
    chem = {
        "id": "CHEBI:1",
        "category": ["biolink:SmallMolecule", "biolink:NamedThing"],
        "equivalent_identifiers": ["CHEBI:1"],
    }
    disease = {
        "id": "MONDO:1",
        "category": ["biolink:Disease", "biolink:NamedThing"],
        "equivalent_identifiers": ["MONDO:1"],
    }
    treats = {"predicate": "biolink:treats"}
    edges = [
        _edge("CHEBI:1", "MONDO:1", **treats, primary_knowledge_source="infores:a"),
        _edge("CHEBI:1", "MONDO:1", **treats, primary_knowledge_source="infores:b"),
        _edge("CHEBI:1", "MONDO:1", predicate="biolink:ameliorates_condition"),
        _edge("CHEBI:1", "MONDO:1", predicate="biolink:contraindicated_in"),
    ]
    graph = tmp_path / "g"
    graph.mkdir()
    _write(graph / "nodes.jsonl", [chem, disease])
    _write(graph / "edges.jsonl", edges)
    report = garbage.collect(graph, tmp_path / "out")
    assert report.flags["opposing_predicate"] == 4, "every edge counted, not 2 per pair"
    assert report.flags_by_source["infores:a"]["opposing_predicate"] == 1
    [pair] = report.opposing_pairs()
    assert pair["predicates"] == [
        "biolink:ameliorates_condition",
        "biolink:treats",
        "biolink:contraindicated_in",
    ]


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
    assert [(c["subject_categories"], c["object_categories"], c["edges"]) for c in rare] == [
        (["biolink:Gene"], ["biolink:Procedure"], 1)
    ]
    assert rare[0]["example"] == ("X:2", "biolink:interacts_with", "T:1")
    assert report.flags["rare_category_combination"] == 1
    assert report.flags_by_source["infores:test"]["rare_category_combination"] == 1
    [flagged] = _read(tmp_path / "out" / "flagged_edges.jsonl")
    assert flagged["object"] == "T:1" and flagged["flags"] == ["rare_category_combination"]


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


def test_most_specific_can_be_more_than_one_category():
    from agents.cleaning import biolink_types as bt

    drug = (
        "biolink:SmallMolecule",
        "biolink:Drug",
        "biolink:MolecularEntity",
        "biolink:MolecularMixture",
        "biolink:ChemicalEntity",
        "biolink:ChemicalOrDrugOrTreatment",
        "biolink:NamedThing",
    )
    assert bt.most_specific(drug) == ("biolink:Drug", "biolink:SmallMolecule")
    # order in the list doesn't matter: the first category isn't assumed to be the most specific
    assert bt.most_specific(tuple(reversed(drug))) == ("biolink:Drug", "biolink:SmallMolecule")
    assert bt.most_specific(("biolink:Disease", "biolink:NamedThing")) == ("biolink:Disease",)
    assert bt.most_specific(("biolink:NotACategory",)) == ("biolink:NotACategory",)
    assert bt.most_specific(()) == ()


def test_combinations_key_on_every_most_specific_category(tmp_path):
    both = ["biolink:NamedThing", "biolink:SmallMolecule", "biolink:Drug", "biolink:ChemicalEntity"]
    nodes = [
        {"id": "CHEBI:1", "category": both, "equivalent_identifiers": ["CHEBI:1"]},
        {
            "id": "UMLS:Ph",
            "category": ["biolink:NamedThing", "biolink:Phenomenon"],
            "equivalent_identifiers": ["UMLS:Ph"],
        },
    ]
    graph = tmp_path / "g"
    graph.mkdir()
    _write(graph / "nodes.jsonl", nodes)
    _write(graph / "edges.jsonl", [_edge("CHEBI:1", "UMLS:Ph", predicate="biolink:treats")])
    report = garbage.collect(graph, tmp_path / "out")
    assert report.combinations == {
        (("biolink:Drug", "biolink:SmallMolecule"), "biolink:treats", ("biolink:Phenomenon",)): 1
    }
    [flagged] = _read(tmp_path / "out" / "flagged_edges.jsonl")
    assert flagged["subject_categories"] == ["biolink:Drug", "biolink:SmallMolecule"]
    assert flagged["object_categories"] == ["biolink:Phenomenon"]
    written = json.loads((tmp_path / "out" / "cleaning_report.json").read_text())
    assert written["type_violations_by_category"] == {
        "biolink:treats": {"range: biolink:Phenomenon": 1}
    }


def _typed(node_id, *categories):
    return {
        "id": node_id,
        "category": list(categories) + ["biolink:NamedThing"],
        "equivalent_identifiers": [node_id],
    }


def test_promoted_type_violation_drops_treats_to_procedure(tmp_path):
    nodes = [
        _typed("CHEBI:1", "biolink:SmallMolecule", "biolink:ChemicalEntity"),
        _typed("MONDO:1", "biolink:Disease", "biolink:DiseaseOrPhenotypicFeature"),
        _typed("UMLS:P", "biolink:Procedure"),
        _typed("UMLS:Ph", "biolink:Phenomenon"),
        _typed("UMLS:D", "biolink:Drug", "biolink:SmallMolecule", "biolink:ChemicalEntity"),
    ]
    edges = [
        _edge("CHEBI:1", "MONDO:1", predicate="biolink:treats"),  # fits
        _edge("CHEBI:1", "UMLS:P", predicate="biolink:treats"),  # promoted drop
        _edge("CHEBI:1", "UMLS:Ph", predicate="biolink:treats"),  # violation, stays a flag
        _edge("CHEBI:1", "UMLS:D", predicate="biolink:treats"),  # the drug's synonym: drop
    ]
    graph = tmp_path / "g"
    graph.mkdir()
    _write(graph / "nodes.jsonl", nodes)
    _write(graph / "edges.jsonl", edges)
    report = garbage.collect(graph, tmp_path / "out")
    assert report.dropped_edges == {
        "type_violation:biolink:treats->biolink:Procedure": 1,
        "type_violation:biolink:treats->biolink:Drug": 1,
    }
    assert report.edges_out == 2
    assert report.flags["type_violation"] == 1  # treats -> Phenomenon
    assert report.type_violations_by_category == {
        "biolink:treats": {"range: biolink:Phenomenon": 1}
    }
    assert report.dropped_nodes == {"orphan": 2}  # UMLS:P and UMLS:D lost their only edge
    kept = {(e["subject"], e["object"]) for e in _read(tmp_path / "out" / "edges.jsonl")}
    assert kept == {("CHEBI:1", "MONDO:1"), ("CHEBI:1", "UMLS:Ph")}


def test_promoted_type_violation_matches_any_most_specific_category():
    cats = ("biolink:Drug", "biolink:SmallMolecule", "biolink:ChemicalEntity", "biolink:NamedThing")
    specific = ("biolink:Drug", "biolink:SmallMolecule")
    disease = ("biolink:Disease", "biolink:NamedThing")
    assert (
        garbage.promoted_type_violation("biolink:treats", cats, cats, specific, specific)
        == "type_violation:biolink:treats->biolink:Drug"
    )
    assert (
        garbage.promoted_type_violation("biolink:treats", cats, disease, specific, disease[:1])
        is None
    )
    # a violation whose predicate has no promotion stays a flag
    assert (
        garbage.promoted_type_violation(
            "biolink:has_phenotype", cats, disease, specific, disease[:1]
        )
        is None
    )


def test_unresolved_nodes_tsv(graph, tmp_path):
    report = garbage.collect(graph, tmp_path / "out")
    rows = (tmp_path / "out" / "unresolved_nodes.tsv").read_text().splitlines()
    assert rows[0] == "node\tprefix\tname\tdropped_edges\tkept_edges\tsources\tpredicates"
    assert rows[1:] == [
        "CHEMBL.TARGET:X\tCHEMBL.TARGET\t\t1\t1\tinfores:test\tbiolink:interacts_with",
        "CHEMBL.TARGET:Y\tCHEMBL.TARGET\t\t1\t0\tinfores:test\tbiolink:interacts_with",
        "OBA:VT0001253\tOBA\t\t1\t0\tinfores:test\tbiolink:interacts_with",
    ]
    written = json.loads((tmp_path / "out" / "cleaning_report.json").read_text())
    assert written["dropped_edges_by_source_prefix"] == [
        {"source": "infores:test", "prefix": "CHEMBL.TARGET", "edges": 2},
        {"source": "infores:test", "prefix": "OBA", "edges": 1},
    ]
    assert report.dropped_edges_by_source_prefix[("infores:test", "CHEMBL.TARGET")] == 2


def test_broken_ids():
    assert garbage.is_broken_id("OBA:VT0001253")
    assert garbage.is_broken_id("phenopacket.store:PMID_1_individual_II-3")
    assert not garbage.is_broken_id("OBA:0000001")  # a real OBA id is only unknown
    assert not garbage.is_broken_id("REACT:R-HSA-1")
