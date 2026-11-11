import importlib.util
import json
from pathlib import Path

import pytest

from agents.filter import subgraph
from agents.filter.cli import main

FIND = Path(__file__).parents[1] / ".agents/skills/filter/focus-subgraph/scripts/find_nodes.py"
spec = importlib.util.spec_from_file_location("find_nodes", FIND)
find_nodes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(find_nodes)

NODES = [
    ("D:1", "Alzheimer disease", "biolink:Disease"),
    ("D:2", "early-onset Alzheimer disease", "biolink:Disease"),
    ("D:3", "familial early-onset Alzheimer disease", "biolink:Disease"),
    ("G:1", "APP", "biolink:Gene"),
    ("G:2", "PSEN1", "biolink:Gene"),
    ("C:1", "donepezil", "biolink:SmallMolecule"),
    ("G:9", "TP53", "biolink:Gene"),
    ("K:1", "ABL1 kinase", "biolink:Protein"),
    ("K:2", "kinase pathway", "biolink:Pathway"),
    ("U:1", "screen hit 1", "biolink:SmallMolecule"),
] + [(f"H:{i}", f"partner {i}", "biolink:Gene") for i in range(5)]


def _edge(s, p, o, source="infores:curated"):
    return {"subject": s, "predicate": f"biolink:{p}", "object": o,
            "primary_knowledge_source": source}  # fmt: skip


EDGES = [
    _edge("D:2", "subclass_of", "D:1"),
    _edge("D:3", "subclass_of", "D:2"),
    _edge("G:1", "genetically_associated_with", "D:1"),
    _edge("G:2", "genetically_associated_with", "D:3"),
    _edge("C:1", "treats", "D:1"),
    _edge("G:1", "interacts_with", "G:9"),
    _edge("U:1", "interacts_with", "K:1", source="infores:my-screen"),
    _edge("K:1", "interacts_with", "G:1"),
] + [_edge("G:9", "interacts_with", f"H:{i}") for i in range(5)]


@pytest.fixture
def graph(tmp_path):
    d = tmp_path / "graph"
    d.mkdir()
    (d / "nodes.jsonl").write_text(
        "".join(json.dumps({"id": i, "name": n, "category": [c]}) + "\n" for i, n, c in NODES)
    )
    (d / "edges.jsonl").write_text("".join(json.dumps(e) + "\n" for e in EDGES))
    return d


def _focus(tmp_path, **kw):
    focus = {"name": "test", "request": "a request", **kw}
    path = tmp_path / "focus.json"
    path.write_text(json.dumps(focus))
    return path


def _ids(path):
    return {json.loads(line)["id"] for line in open(path / "nodes.jsonl")}


def test_focus_problems_are_all_reported(tmp_path):
    path = tmp_path / "f.json"
    path.write_text(json.dumps({"name": "Bad", "seeds": [{"ids": [], "name": "x"}], "hops": 9}))
    with pytest.raises(subgraph.FocusError) as e:
        subgraph.load_focus(path)
    for problem in ("name:", "request:", "exactly one of", "hops:"):
        assert problem in str(e.value)


def test_disease_and_subclasses_one_hop(graph, tmp_path):
    seeds = [
        {"ids": ["D:1"]},
        {"linked_to": ["D:1"], "predicate": "biolink:subclass_of", "transitive": True},
    ]
    focus = subgraph.load_focus(_focus(tmp_path, seeds=seeds))
    report = subgraph.extract(focus, graph, tmp_path / "out")
    assert report["seeds"] == 3
    assert report["seed_rules"][1]["nodes"] == 2  # D:2 and D:3, through D:2
    assert _ids(tmp_path / "out") == {"D:1", "D:2", "D:3", "G:1", "G:2", "C:1"}
    assert report["edges"] == 5  # the G:1-G:9 edge leaves the subgraph


def test_not_transitive_takes_direct_subclasses_only(graph, tmp_path):
    seeds = [{"linked_to": ["D:1"], "predicate": "biolink:subclass_of"}]
    focus = subgraph.load_focus(_focus(tmp_path, seeds=seeds, hops=0))
    subgraph.extract(focus, graph, tmp_path / "out")
    assert _ids(tmp_path / "out") == {"D:2"}


def test_hubs_are_kept_but_not_expanded(graph, tmp_path):
    focus = subgraph.load_focus(_focus(tmp_path, seeds=[{"ids": ["G:1"]}], hops=2, max_degree=3))
    report = subgraph.extract(focus, graph, tmp_path / "out")
    ids = _ids(tmp_path / "out")
    assert "G:9" in ids and not any(i.startswith("H:") for i in ids)
    assert report["hubs_not_expanded"] == {"G:9": 6}


def test_seed_hubs_expand_and_are_reported(graph, tmp_path):
    # G:9 has 6 edges, over max_degree=3, but it is a seed, so its partners are reached
    focus = subgraph.load_focus(_focus(tmp_path, seeds=[{"ids": ["G:9"]}], max_degree=3))
    report = subgraph.extract(focus, graph, tmp_path / "out")
    assert {f"H:{i}" for i in range(5)} <= _ids(tmp_path / "out")
    assert report["seed_hubs"] == {"G:9": 6}
    assert report["hubs_not_expanded"] == {}
    assert "G:9" in subgraph.summary(report)


def test_name_rule_matches_word_starts_not_substrings(tmp_path):
    d = tmp_path / "g"
    d.mkdir()
    names = ["ALK", "ALKBH5", "tyrosine kinases", "kinase", "pseudokinase", "TALK1"]
    (d / "nodes.jsonl").write_text(
        "".join(json.dumps({"id": f"N:{i}", "name": n}) + "\n" for i, n in enumerate(names))
    )
    (d / "edges.jsonl").write_text("")
    for text, expected in (("ALK", {"N:0", "N:1"}), ("kinase", {"N:2", "N:3"})):
        focus = subgraph.load_focus(_focus(tmp_path, seeds=[{"name": text}], hops=0))
        seeds, _ = subgraph.find_seeds(focus, d / "nodes.jsonl", d / "edges.jsonl")
        assert seeds == expected


def test_name_rule_with_category(graph, tmp_path):
    seeds = [{"name": "kinase", "category": "biolink:Protein"}]
    focus = subgraph.load_focus(_focus(tmp_path, seeds=seeds, hops=0))
    report = subgraph.extract(focus, graph, tmp_path / "out")
    assert report["seed_rules"][0]["examples"] == ["ABL1 kinase [K:1]"]  # not the pathway


def test_users_own_source_with_allowed_sources(graph, tmp_path):
    focus = subgraph.load_focus(
        _focus(
            tmp_path, seeds=[{"source": "infores:my-screen"}], hops=1, sources=["infores:my-screen"]
        )  # fmt: skip
    )
    report = subgraph.extract(focus, graph, tmp_path / "out")
    assert _ids(tmp_path / "out") == {"U:1", "K:1"}
    assert report["edges_by_source"] == {"infores:my-screen": 1}


def test_cli_without_focus_is_the_whole_graph(graph, tmp_path, capsys):
    out = tmp_path / "whole"
    assert main(["--input", str(graph), "--output", str(out)]) == 0
    assert _ids(out) == {i for i, _, _ in NODES}
    assert "whole graph" in capsys.readouterr().out


def test_cli_prefers_the_cleaned_graph(graph, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cleaning = tmp_path / "data/cleaning"
    cleaning.mkdir(parents=True)
    for name in ("nodes.jsonl", "edges.jsonl"):
        (cleaning / name).write_text((graph / name).read_text())
    focus = _focus(tmp_path, seeds=[{"ids": ["C:1"]}], hops=0)
    assert main(["--focus", str(focus)]) == 0
    report = json.loads((tmp_path / "data/filter/test/focus_report.json").read_text())
    assert report["input"] == str(cleaning.resolve())


def test_cli_without_a_graph(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main([]) == 1
    assert "run the transformation" in capsys.readouterr().out


def test_find_nodes(graph, capsys):
    find_nodes.main(["alzheimer", str(graph)])
    out = capsys.readouterr().out
    assert "3 nodes match 'alzheimer'" in out
    find_nodes.main(["--around", "D:1", str(graph)])
    out = capsys.readouterr().out
    assert "<- biolink:subclass_of  1 edges from infores:curated; e.g. D:2" in out


def _small_graph(tmp_path, nodes, edges):
    """A graph from {id: node fields} and (subject, predicate, object, source) tuples."""
    d = tmp_path / "small"
    d.mkdir(exist_ok=True)
    (d / "nodes.jsonl").write_text(
        "".join(json.dumps({"id": i, **fields}) + "\n" for i, fields in nodes.items())
    )
    (d / "edges.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "subject": s,
                    "predicate": f"biolink:{p}",
                    "object": o,
                    "primary_knowledge_source": src,
                }  # fmt: skip
            )
            + "\n"
            for s, p, o, src in edges
        )
    )
    return d


def test_hops_zero_keeps_every_source_between_seeds_unless_sources_limits(tmp_path):
    # "my dataset" at hops 0 still keeps another source's edge between two of its nodes
    d = _small_graph(
        tmp_path,
        {"A": {"name": "a"}, "B": {"name": "b"}},
        [
            ("A", "interacts_with", "B", "infores:mine"),
            ("A", "interacts_with", "B", "infores:other"),
        ],
    )
    seeds = [{"source": "infores:mine"}]
    both = subgraph.load_focus(_focus(tmp_path, seeds=seeds, hops=0))
    assert subgraph.extract(both, d, tmp_path / "o1")["edges"] == 2
    only = subgraph.load_focus(_focus(tmp_path, seeds=seeds, hops=0, sources=["infores:mine"]))
    assert subgraph.extract(only, d, tmp_path / "o2")["edges_by_source"] == {"infores:mine": 1}


def test_linked_to_finds_subjects_so_a_pathways_members_need_a_hop(tmp_path):
    # pathway has_participant gene: the pathway is the subject, so linked_to can't find the genes
    d = _small_graph(
        tmp_path,
        {"P": {"name": "pathway"}, "G1": {"name": "g1"}, "G2": {"name": "g2"}},
        [
            ("P", "has_participant", "G1", "infores:kegg"),
            ("P", "has_participant", "G2", "infores:kegg"),
        ],
    )
    by_link = [{"linked_to": ["P"], "predicate": "biolink:has_participant"}]
    focus = subgraph.load_focus(_focus(tmp_path, seeds=by_link, hops=0))
    assert subgraph.extract(focus, d, tmp_path / "o1")["seeds"] == 0
    by_hop = subgraph.load_focus(
        _focus(tmp_path, seeds=[{"ids": ["P"]}], hops=1, predicates=["biolink:has_participant"])
    )
    assert subgraph.extract(by_hop, d, tmp_path / "o2")["nodes"] == 3


def test_ids_missing_from_the_graph_are_not_an_error(graph, tmp_path):
    focus = subgraph.load_focus(_focus(tmp_path, seeds=[{"ids": ["G:1", "X:404"]}], hops=0))
    report = subgraph.extract(focus, graph, tmp_path / "out")
    assert report["seeds"] == 1 and report["seed_rules"][0]["nodes"] == 1


def test_seed_hubs_is_empty_at_hops_zero(graph, tmp_path):
    # nothing expands at hops 0, so an oversized seed is not reported: the skill says so
    focus = subgraph.load_focus(_focus(tmp_path, seeds=[{"ids": ["G:9"]}], hops=0, max_degree=3))
    assert subgraph.extract(focus, graph, tmp_path / "out")["seed_hubs"] == {}


def test_hub_degree_counts_only_edges_that_pass_the_filters(tmp_path):
    # N has 4 edges in all, so it is a hub at max_degree 2, but only 1 passes `sources`
    d = _small_graph(
        tmp_path,
        {k: {"name": k} for k in ("S", "N", "X", "Y", "Z")},
        [("S", "interacts_with", "N", "infores:a")]
        + [("N", "interacts_with", other, "infores:b") for other in "XYZ"],
    )
    base = {"seeds": [{"ids": ["S"]}], "hops": 2, "max_degree": 2}
    limited = subgraph.load_focus(_focus(tmp_path, **base, sources=["infores:a"]))
    assert subgraph.extract(limited, d, tmp_path / "o1")["hubs_not_expanded"] == {}
    unlimited = subgraph.load_focus(_focus(tmp_path, **base))
    assert subgraph.extract(unlimited, d, tmp_path / "o2")["hubs_not_expanded"] == {"N": 4}


def test_category_rule_matches_membership_not_the_first_entry(tmp_path):
    # the merge sorts each category list, so a protein's first entry is BiologicalEntity
    d = _small_graph(
        tmp_path,
        {
            "K:1": {
                "name": "ABL1 kinase",
                "category": ["biolink:BiologicalEntity", "biolink:Protein"],
            },
            "K:2": {"name": "kinase pathway", "category": ["biolink:Pathway"]},
        },
        [],
    )
    focus = subgraph.load_focus(
        _focus(tmp_path, seeds=[{"name": "kinase", "category": "biolink:Protein"}], hops=0)
    )
    assert subgraph.extract(focus, d, tmp_path / "out")["seeds"] == 1


def test_the_same_focus_file_gives_the_same_subgraph(graph, tmp_path):
    seeds = [{"ids": ["D:1"]}, {"linked_to": ["D:1"], "predicate": "biolink:subclass_of"}]
    focus = subgraph.load_focus(_focus(tmp_path, seeds=seeds))
    subgraph.extract(focus, graph, tmp_path / "first")
    subgraph.extract(focus, graph, tmp_path / "second")
    for name in ("nodes.jsonl", "edges.jsonl"):
        assert (tmp_path / "first" / name).read_text() == (tmp_path / "second" / name).read_text()


def test_seeds_without_edges_are_kept(graph, tmp_path):
    focus = subgraph.load_focus(_focus(tmp_path, seeds=[{"ids": ["G:2"]}], hops=0))
    subgraph.extract(focus, graph, tmp_path / "out")
    assert _ids(tmp_path / "out") == {"G:2"}  # its only edge goes to D:3, which is not a seed
