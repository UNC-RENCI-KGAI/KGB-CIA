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
