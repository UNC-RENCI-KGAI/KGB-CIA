import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).parents[1] / ".agents/skills/filter/describe-graph/scripts/describe_graph.py"
)
spec = importlib.util.spec_from_file_location("describe_graph", SCRIPT)
dg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dg)


def _workspace(root, source_id, nodes, edges=0, spo=(), fetch="stable"):
    """A minimal ORION workspace: meta.json, QC file and normalized nodes."""
    data = root / source_id / source_id / "data" / "v1"
    data.mkdir(parents=True)
    meta = {"source_id": source_id, "source_version": "v1", "fetch_status": fetch, "parsings": {}}
    if fetch != "stable":
        meta["fetch_error"] = "HTTP Error 403: Forbidden"
    else:
        meta["parsings"]["p1"] = {
            "parsing_status": "stable",
            "normalizations": {
                "n1": {
                    "normalization_status": "stable",
                    "normalization_time": "11-09-26 00:00:00",
                    "normalization_info": {
                        "final_normalized_nodes": len(nodes),
                        "final_normalized_edges": edges,
                        "node_normalization_failures": sum(1 for _, ok in nodes if not ok),
                    },
                }
            },
        }
        meta["builds"] = {
            "b1": {
                "parsing_version": "p1",
                "normalization_version": "n1",
                "identifier": f"infores:{source_id.lower()}",
                "name": source_id,
            }
        }
        qc = {
            "node_types": [{"type": ["biolink:Gene"], "count": len(nodes)}],
            "predicate_totals": {"biolink:interacts_with": edges},
            "s-p-o_types": [
                {"subject_type": [s], "predicate": p, "object_type": [o], "count": n}
                for s, p, o, n in spo
            ],
            "edges_with_publications": {"biolink:interacts_with": edges // 2},
        }
        (data / f"{source_id}_b1.json").write_text(json.dumps(qc))
        norm = data / "parsed_p1" / "normalized_n1"
        norm.mkdir(parents=True)
        (norm / "normalized_nodes.jsonl").write_text(
            "".join(
                json.dumps({"id": i, "equivalent_identifiers": [i] if ok else []}) + "\n"
                for i, ok in nodes
            )
        )
    (data / f"{source_id}.meta.json").write_text(json.dumps(meta))


@pytest.fixture
def graphs(tmp_path):
    root = tmp_path / "graphs"
    gene = ("biolink:Gene", "biolink:interacts_with", "biolink:Gene", 10)
    _workspace(
        root,
        "Kgbcia_user",
        [("G:1", True), ("G:2", True), ("G:3", True), ("X:9", False)],
        edges=4,
        spo=[gene],
    )
    _workspace(
        root, "Kgbcia_big", [("G:1", True), ("G:2", True), ("G:7", True)], edges=10, spo=[gene]
    )
    _workspace(root, "Kgbcia_small", [("G:3", True), ("G:8", True)], edges=2, spo=[gene])
    _workspace(root, "Kgbcia_blocked", [], fetch="failed")
    return root


def test_describe_from_metadata(graphs):
    d = dg.describe(graphs)
    assert d["sources_built"] == 3 and d["sources_total"] == 4
    assert d["totals"]["edges"] == 16
    assert d["totals"]["basis"].startswith("sum over sources")
    assert d["spo"][0] == {
        "subject": "biolink:Gene",
        "predicate": "biolink:interacts_with",
        "object": "biolink:Gene",
        "edges": 30,
    }
    blocked = next(s for s in d["sources"] if s["source_id"] == "Kgbcia_blocked")
    assert "403" in blocked["problem"]
    assert "overlap" not in d


def test_user_overlap_and_recommendation(graphs):
    user = dg.describe(graphs, user=["Kgbcia_user"])["overlap"]["user"]
    assert user["nodes"] == 3  # the unresolved X:9 can't connect, so it isn't counted
    assert user["connected_nodes"] == 3
    assert user["shared_with"] == {"Kgbcia_big": 2, "Kgbcia_small": 1}
    assert [r["source_id"] for r in user["recommended_sources"]] == ["Kgbcia_big", "Kgbcia_small"]
    assert user["recommended_sources"][-1]["cumulative"] == 3


def test_cleaning_report_sets_totals(graphs, tmp_path):
    cleaned = tmp_path / "clean"
    cleaned.mkdir()
    (cleaned / "cleaning_report.json").write_text(
        json.dumps(
            {
                "nodes": {"in": 9, "out": 6},
                "edges": {"in": 16, "out": 12},
                "dropped_edges": {"unresolved_node": 4},
            }
        )
    )
    totals = dg.describe(graphs, cleaned=cleaned)["totals"]
    assert totals["basis"] == "cleaned graph" and totals["edges"] == 12


def test_main_writes_all_outputs(graphs, tmp_path):
    out = tmp_path / "out"
    assert dg.main([str(graphs), "--user", "Kgbcia_user", "--output", str(out)]) == 0
    page = (out / "overview.html").read_text()
    assert "How your data connects" in page and "<script>" in page
    assert "100% connected so far" in (out / "description.md").read_text()


def test_unknown_user_source(graphs):
    with pytest.raises(SystemExit, match="Kgbcia_nope"):
        dg.describe(graphs, user=["Kgbcia_nope"])


def test_labels():
    assert dg.label("biolink:SequenceVariant") == "Sequence variant"
    assert dg.label("biolink:genetically_associated_with") == "genetically associated with"
    assert dg.short(12_841_209) == "12.8M"
