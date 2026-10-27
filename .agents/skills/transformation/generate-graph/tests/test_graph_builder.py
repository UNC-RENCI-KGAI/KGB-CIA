"""Observable builder behavior using synthetic parsers and offline normalization responses."""

from __future__ import annotations

import gzip
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from shared.schema import Graph

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_graph.py"
SPEC = importlib.util.spec_from_file_location("generate_graph_builder", SCRIPT)
builder = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = builder
SPEC.loader.exec_module(builder)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Tests must not call the live NodeNorm service")

    monkeypatch.setattr(builder.nodenorm, "normalize", blocked)


@pytest.fixture
def vocabulary():
    return builder.Vocabulary(
        {
            "version": "synthetic-test",
            "classes": {
                "biolink:NamedThing": [],
                "biolink:Gene": ["biolink:NamedThing"],
                "biolink:Protein": ["biolink:NamedThing"],
            },
            "predicates": ["biolink:related_to"],
        }
    )


def record(kind, data, row=1):
    return {
        "kind": kind,
        "data": data,
        "location": {"file": "input.csv", "record_set": "entities", "record": row},
    }


def node(identifier, row=1, **kwargs):
    return record("node", {"id": identifier, "category": "biolink:Gene", **kwargs}, row)


def edge(subject, obj, row=1, **kwargs):
    return record(
        "edge",
        {
            "subject": subject,
            "object": obj,
            "predicate": "biolink:related_to",
            "provenance": {
                "source": "demo",
                "file": "input.csv",
                "columns": ["gene", "target"],
                "evidence": ["PMID:1"],
            },
            **kwargs,
        },
        row,
    )


def parser_file(tmp_path, records, name="parser.py", tail="", version=1):
    path = tmp_path / name
    path.write_text(
        f"PARSER_API_VERSION = {version!r}\n"
        "def parse(source_files, metadata):\n"
        f"    yield from {records!r}\n" + tail
    )
    return path


def response(identifier, types=None, label="Normalized gene"):
    return {
        "id": {"identifier": identifier, "label": label},
        "type": types or ["biolink:NamedThing", "biolink:Gene"],
    }


def test_normalization_merges_nodes_rewrites_edges_and_preserves_lists(tmp_path, vocabulary):
    records = [
        edge("X:1", "LOCAL:2"),  # The interface permits edges before their nodes.
        node("X:1", properties={"xref": ["A:1", "B:2"]}),
        node("ALIAS:1", properties={"symbol": "GENE"}),
        node("LOCAL:2"),
    ]
    graph, report, snapshot = builder.build_graph(
        parser_file(tmp_path, records),
        {},
        {},
        vocabulary,
        "demo",
        replay={"X:1": response("NCBIGene:1"), "ALIAS:1": response("NCBIGene:1"), "LOCAL:2": None},
    )
    assert {n.id for n in graph.nodes} == {"NCBIGene:1", "LOCAL:2"}
    normalized = next(n for n in graph.nodes if n.id == "NCBIGene:1")
    assert normalized.original_ids == ["ALIAS:1", "X:1"]
    assert normalized.properties == {"xref": ["A:1", "B:2"], "symbol": "GENE"}
    assert normalized.name == "Normalized gene"
    assert graph.edges[0].subject == "NCBIGene:1"
    assert graph.edges[0].object == "LOCAL:2"
    assert graph.edges[0].provenance.evidence == ["PMID:1"]
    assert '"record": 1' in graph.edges[0].provenance.notes
    assert next(n for n in graph.nodes if n.id == "LOCAL:2").original_ids == ["LOCAL:2"]
    assert report["status"] == "complete"
    assert report["counts"]["unresolved_ids"] == 1
    assert snapshot["LOCAL:2"] is None
    assert Graph.model_validate_json(graph.model_dump_json()) == graph


def test_strict_policy_excludes_unresolved_nodes_and_dangling_edges(tmp_path, vocabulary):
    graph, report, _ = builder.build_graph(
        parser_file(tmp_path, [node("X:1"), node("LOCAL:2"), edge("X:1", "LOCAL:2")]),
        {},
        {},
        vocabulary,
        "demo",
        policy="strict",
        replay={"X:1": response("NCBIGene:1"), "LOCAL:2": None},
    )
    assert [n.id for n in graph.nodes] == ["NCBIGene:1"]
    assert graph.edges == []
    assert report["status"] == "partial"
    assert {d["code"] for d in report["diagnostics"]} == {"unresolved_id", "missing_endpoint"}


@pytest.mark.parametrize(
    ("mapped", "types", "expected"),
    [
        ("biolink:NamedThing", ["biolink:Gene", "biolink:NamedThing"], "biolink:NamedThing"),
        ("biolink:Protein", ["biolink:NamedThing", "biolink:Gene"], "biolink:Gene"),
        ("biolink:NamedThing", ["biolink:Protein", "biolink:Gene"], "biolink:Gene"),
    ],
)
def test_category_rule_uses_source_then_hierarchy_then_stable_tie(
    vocabulary, mapped, types, expected
):
    assert vocabulary.category(mapped, types) == expected
    assert vocabulary.category(mapped, list(reversed(types))) == expected


def test_record_conflicts_and_malformed_rows_do_not_stop_build(tmp_path, vocabulary):
    records = [
        node("X:1", properties={"score": 1}),
        node("ALIAS:1", row=2, properties={"score": 2}),
        node("INVALID:1", row=3, category="biolink:DoesNotExist"),
        node("LOCAL:2", row=4),
        edge("X:1", "LOCAL:2", row=4, properties={"scores": [1.0, 2.0]}),
        edge("ALIAS:1", "LOCAL:2", row=5),
        record("node", {"category": "biolink:Gene"}, row=6),
    ]
    graph, report, _ = builder.build_graph(
        parser_file(tmp_path, records),
        {},
        {},
        vocabulary,
        "demo",
        replay={"X:1": response("NCBIGene:1"), "ALIAS:1": response("NCBIGene:1"), "LOCAL:2": None},
    )
    assert len(graph.nodes) == 2
    assert len(graph.edges) == 1
    assert graph.edges[0].properties["scores"] == [1.0, 2.0]
    assert report["status"] == "partial"
    invalid = [d for d in report["diagnostics"] if d["code"] == "invalid_record"]
    assert {d["location"]["record"] for d in invalid} == {3, 6}
    assert {d["code"] for d in report["diagnostics"]} >= {"node_conflict", "missing_endpoint"}


def test_invalid_provenance_is_reported_and_other_assertions_survive(tmp_path, vocabulary):
    bad = edge("X:1", "X:1")
    bad["data"]["provenance"]["columns"] = []
    graph, report, _ = builder.build_graph(
        parser_file(tmp_path, [node("X:1"), bad, edge("X:1", "X:1", row=2)]),
        {},
        {},
        vocabulary,
        "demo",
        replay={"X:1": None},
    )
    assert len(graph.edges) == 1
    assert report["diagnostics"][0]["code"] == "invalid_record"


def test_distinct_assertion_lineage_is_preserved_and_exact_duplicates_are_removed(
    tmp_path, vocabulary
):
    graph, report, _ = builder.build_graph(
        parser_file(
            tmp_path,
            [node("X:1"), edge("X:1", "X:1"), edge("X:1", "X:1"), edge("X:1", "X:1", row=2)],
        ),
        {},
        {},
        vocabulary,
        "demo",
        replay={"X:1": None},
    )
    assert len(graph.edges) == 2
    assert report["counts"]["duplicate_edges"] == 1


def test_normalization_batches_every_distinct_id_and_replays_identically(tmp_path, vocabulary):
    parser = parser_file(tmp_path, [node("X:2"), node("X:1"), node("X:1"), node("X:3")])
    calls = []

    def normalize(ids, *, url):
        calls.append((ids, url))
        return {identifier: response("CANON:" + identifier[-1]) for identifier in ids}

    graph, report, snapshot = builder.build_graph(
        parser,
        {},
        {},
        vocabulary,
        "demo",
        batch_size=2,
        url="https://example.test/nodenorm",
        normalizer=normalize,
    )
    assert calls == [
        (["X:1", "X:2"], "https://example.test/nodenorm"),
        (["X:3"], "https://example.test/nodenorm"),
    ]
    replayed, repeated_report, _ = builder.build_graph(
        parser,
        {},
        {},
        vocabulary,
        "demo",
        replay=snapshot,
    )
    assert replayed == graph
    assert repeated_report == report


@pytest.mark.parametrize("bad_response", [[], {"X:1": {"id": "bad"}}, {"X:1": False}])
def test_malformed_service_response_is_structural(tmp_path, vocabulary, bad_response):
    with pytest.raises(builder.BuildError):
        builder.build_graph(
            parser_file(tmp_path, [node("X:1")]),
            {},
            {},
            vocabulary,
            "demo",
            normalizer=lambda *args, **kwargs: bad_response,
        )


def test_service_outage_never_falls_back_to_lenient_retention(tmp_path, vocabulary):
    def unavailable(*args, **kwargs):
        raise ConnectionError("offline")

    with pytest.raises(builder.BuildError, match="Normalization service failed"):
        builder.build_graph(
            parser_file(tmp_path, [node("X:1")]),
            {},
            {},
            vocabulary,
            "demo",
            normalizer=unavailable,
        )


@pytest.mark.parametrize("case", ["crash", "version", "envelope", "missing_file", "replay"])
def test_structural_failures_are_not_record_diagnostics(tmp_path, vocabulary, case):
    parser = parser_file(
        tmp_path,
        [node("X:1")],
        version=2 if case == "version" else 1,
        tail="    raise RuntimeError('parser crashed')\n" if case == "crash" else "",
    )
    if case == "envelope":
        parser = parser_file(tmp_path, [{"kind": "unsupported"}], name="invalid.py")
    files = {"missing": tmp_path / "missing.csv"} if case == "missing_file" else {}
    with pytest.raises(builder.BuildError):
        builder.build_graph(
            parser,
            files,
            {},
            vocabulary,
            "demo",
            replay={} if case == "replay" else {"X:1": None},
        )


@pytest.mark.parametrize("compressed", [False, True])
def test_manifest_resolves_paths_executes_csv_parser_and_reports_structural_failures(
    tmp_path,
    vocabulary,
    monkeypatch,
    compressed,
):
    (tmp_path / "input.csv").write_text("id,name\nX:1,First\n,bad\nX:2,Second\n")
    metadata_name = "metadata.json.gz" if compressed else "metadata.json"
    metadata_text = json.dumps({"record_set": "genes"})
    if compressed:
        with gzip.open(tmp_path / metadata_name, "wt") as stream:
            stream.write(metadata_text)
    else:
        (tmp_path / metadata_name).write_text(metadata_text)
    (tmp_path / "vocabulary.json").write_text(
        json.dumps(
            {
                "version": vocabulary.version,
                "classes": vocabulary.classes,
                "predicates": sorted(vocabulary.predicates),
            }
        )
    )
    (tmp_path / "normalization.json").write_text(json.dumps({"X:1": None, "X:2": None}))
    (tmp_path / "csv_parser.py").write_text("""import csv
PARSER_API_VERSION = 1
def parse(source_files, metadata):
    with source_files["table"].open() as stream:
        for row_number, row in enumerate(csv.DictReader(stream), 2):
            location = {"file": "input.csv", "record_set": metadata["croissant"]["record_set"],
                        "record": row_number}
            if not row["id"]:
                yield {"kind": "diagnostic", "location": location,
                       "data": {"code": "missing_id", "message": "Missing identifier",
                                "skipped": True}}
                continue
            yield {"kind": "node", "location": location,
                   "data": {"id": row["id"], "category": "biolink:Gene", "name": row["name"]}}
""")
    source = {
        "name": "demo",
        "parser": "csv_parser.py",
        "metadata": {"croissant": metadata_name},
        "source_files": {"table": "input.csv"},
        "normalization_responses": "normalization.json",
    }
    manifest = {"biolink": "vocabulary.json", "output_dir": "output", "sources": [source]}
    manifest_path = tmp_path / "build.json"
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.chdir(tmp_path.parent)
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), str(manifest_path)],
        cwd=tmp_path.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    output = tmp_path / "output" / "demo"
    graph = Graph.model_validate_json((output / "graph.json").read_text())
    assert [n.name for n in graph.nodes] == ["First", "Second"]
    report = json.loads((output / "report.json").read_text())
    assert report["status"] == "partial"
    assert report["manifest"]["normalization_mode"] == "replay"
    assert report["manifest"]["source_sha256"]["table"] == builder.checksum(tmp_path / "input.csv")
    assert json.loads((output / "normalization.json").read_text()) == {"X:1": None, "X:2": None}
    manifest["sources"].append({**source, "name": "broken", "parser": "missing.py"})
    manifest_path.write_text(json.dumps(manifest))
    assert builder.main([str(manifest_path)]) == 1
    summary = json.loads((tmp_path / "output" / "build_report.json").read_text())
    assert [item["status"] for item in summary["sources"]] == ["partial", "failed"]
    assert summary["sources"][1]["graph"] is None
    assert not (tmp_path / "output" / "broken" / "graph.json").exists()
