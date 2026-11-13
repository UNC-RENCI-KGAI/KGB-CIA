import asyncio
import hashlib
import io
import json
import os
import runpy
import shutil
import subprocess
import sys
import tarfile
import types
from pathlib import Path
from typing import get_args, get_origin

import kgbcia_mcp.cli as published_cli_module
import kgbcia_mcp.server as server_module
import pytest
from kgbcia_mcp.artifacts import load_artifacts, resolve_artifacts
from kgbcia_mcp.config import Settings
from kgbcia_mcp.lineage import LineageIndex
from kgbcia_mcp.report_models import (
    ColumnRef as VendoredColumnRef,
)
from kgbcia_mcp.report_models import (
    Mapping as VendoredMapping,
)
from kgbcia_mcp.report_models import (
    Skipped as VendoredSkipped,
)
from kgbcia_mcp.report_models import (
    TransformReport as VendoredTransformReport,
)
from kgbcia_mcp.server import SERVER_INSTRUCTIONS, create_server
from kgbcia_mcp.services.neo4j_service import (
    _READ_TIMEOUT_SECONDS,
    FULLTEXT_INDEX_PROPERTIES,
    Neo4jService,
    ReadResult,
    _classify_neo4j_failure,
    _quality_flags,
    serialize_neo4j_value,
)
from neo4j import GraphDatabase
from neo4j.exceptions import Neo4jError
from neo4j.graph import Graph as Neo4jGraph
from neo4j.graph import Node as Neo4jNode
from neo4j.spatial import CartesianPoint

import agents.mcp_interface.cli as stage_cli_module
from agents.mcp_interface.check import (
    check_graph,
    discover_reports,
    format_check_result,
    resolve_default_input,
)
from agents.mcp_interface.cli import main as stage_main
from shared.schema import ColumnRef, Edge, Mapping, Node, Skipped, TransformReport


def _report() -> TransformReport:
    return TransformReport(
        source="infores:chembl",
        source_version="1.0",
        croissant="sources/chembl/croissant.json",
        mappings=[
            Mapping(
                id="chembl.interacts",
                file="activities.tsv",
                record_set="activities",
                produces="edge",
                subject_category="biolink:SmallMolecule",
                predicate="biolink:directly_physically_interacts_with",
                object_category="biolink:Gene",
                columns={
                    "subject": [ColumnRef(field="compound", name="compound_id")],
                    "object": [ColumnRef(field="target", name="target_id")],
                },
                constants={"knowledge_level": "knowledge_assertion"},
            )
        ],
    )


def _edge(index: int, **changes) -> Edge:
    fields = {
        "id": f"edge-{index}",
        "subject": f"CHEBI:{index + 1}",
        "predicate": "biolink:directly_physically_interacts_with",
        "object": f"NCBIGene:{index + 100}",
        "primary_knowledge_source": "infores:chembl",
        "aggregator_knowledge_source": ["infores:tcrd"],
        "knowledge_level": "knowledge_assertion",
        "agent_type": "data_analysis_pipeline",
        "publications": [f"PMID:{index + 1000}"],
        "kgbcia_source_records": [f"chembl.interacts#row-{index}"],
    }
    fields.update(changes)
    return Edge(**fields)


@pytest.fixture
def toy_graph(tmp_path):
    graph_dir = tmp_path / "graph"
    graph_dir.mkdir()
    nodes = [
        Node(
            id=f"CHEBI:{index + 1}",
            category=["biolink:SmallMolecule"],
            name=f"Compound {index + 1}",
        )
        for index in range(5)
    ] + [
        Node(
            id=f"NCBIGene:{index + 100}",
            category=["biolink:Gene"],
            name=f"Gene {index + 100}",
        )
        for index in range(5)
    ]
    edges = [_edge(index) for index in range(15)]
    (graph_dir / "nodes.jsonl").write_text(
        "".join(f"{node.model_dump_json(exclude_none=True)}\n" for node in nodes)
    )
    (graph_dir / "edges.jsonl").write_text(
        "".join(f"{edge.model_dump_json(exclude_none=True)}\n" for edge in edges)
    )
    report_path = tmp_path / "reports" / "chembl" / "transform_report.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(_report().model_dump_json())
    return graph_dir, report_path, nodes, edges


def test_check_streams_valid_toy_graph(toy_graph, monkeypatch):
    graph_dir, report_path, _, _ = toy_graph
    original = Path.read_text

    def reject_jsonl_read_text(path, *args, **kwargs):
        if path.suffix == ".jsonl":
            raise AssertionError("large graph files must be streamed")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", reject_jsonl_read_text)
    result = check_graph(graph_dir, [report_path])
    assert result.ok
    assert (result.node_count, result.edge_count) == (10, 15)
    assert "prepare_dump.sh" in format_check_result(result)


def test_check_counts_missing_lineage_by_primary_source(toy_graph):
    graph_dir, report_path, _, edges = toy_graph
    values = [edge.model_dump(exclude_none=True) for edge in edges]
    values[0].pop("id")
    values[1].pop("kgbcia_source_records")
    values[2]["kgbcia_source_records"] = "chembl.interacts#row-2"
    values[3]["kgbcia_source_records"] = ["unknown.mapping#row-3"]
    (graph_dir / "edges.jsonl").write_text("".join(f"{json.dumps(value)}\n" for value in values))

    result = check_graph(graph_dir, [report_path])
    assert not result.ok
    assert result.error_counts["missing_edge_id"] == 1
    assert result.error_counts["missing_source_records"] == 1
    assert result.error_counts["non_list_source_records"] == 1
    assert result.error_counts["unknown_mapping_id"] == 1
    assert result.missing_source_records_by_primary_source == {"infores:chembl": 1}
    assert result.unknown_mapping_ids == {"unknown.mapping": 1}


def test_check_rejects_empty_and_malformed_lineage(toy_graph):
    graph_dir, report_path, _, edges = toy_graph
    values = [edge.model_dump(exclude_none=True) for edge in edges]
    values[0]["kgbcia_source_records"] = []
    values[1]["kgbcia_source_records"] = ["missing-separator"]
    values[2]["kgbcia_source_records"] = ["#row"]
    (graph_dir / "edges.jsonl").write_text("".join(f"{json.dumps(value)}\n" for value in values))
    result = check_graph(graph_dir, [report_path])
    assert result.error_counts["empty_source_records"] == 1
    assert result.error_counts["malformed_source_record"] == 2


def test_default_input_and_report_discovery(tmp_path):
    merged = tmp_path / "data" / "transformation" / "merged"
    merged.mkdir(parents=True)
    report = tmp_path / "data" / "transformation" / "chembl" / "transform_report.json"
    report.parent.mkdir()
    report.write_text("{}")
    assert resolve_default_input(tmp_path) == merged
    cleaning = tmp_path / "data" / "cleaning"
    cleaning.mkdir()
    assert resolve_default_input(tmp_path) == cleaning
    assert discover_reports(tmp_path) == [report]


def test_lineage_resolves_merged_records_and_first_hash():
    report = VendoredTransformReport.model_validate(_report().model_dump())
    edge = _edge(1).model_dump(exclude_none=True)
    edge["kgbcia_source_records"] = [
        "chembl.interacts#row-1",
        "chembl.interacts#compound#record#2",
    ]
    result = LineageIndex([report]).resolve_source_records(edge)
    assert result["lineage"][0]["match"] == "exact"
    assert result["lineage"][0]["record_count"] == 2
    assert result["lineage"][0]["record_keys"] == ["compound#record#2", "row-1"]
    assert result["lineage"][0]["file"] == "activities.tsv"


def test_duplicate_mapping_ids_only_exact_match_edge_sources():
    chembl = VendoredTransformReport.model_validate(_report().model_dump())
    unrelated = chembl.model_copy(update={"source": "infores:unrelated"})
    edge = _edge(1).model_dump(exclude_none=True)
    result = LineageIndex([chembl, unrelated]).resolve_source_records(edge)
    exact = [entry for entry in result["lineage"] if entry["match"] == "exact"]
    assert len(exact) == 1
    assert exact[0]["source"] == "infores:chembl"


def test_lineage_unknown_mapping_uses_candidate_fallback():
    report = VendoredTransformReport.model_validate(_report().model_dump())
    edge = _edge(1).model_dump(exclude_none=True)
    edge["kgbcia_source_records"] = ["unknown#row-1"]
    result = LineageIndex([report]).resolve_source_records(
        edge, ["biolink:SmallMolecule"], ["biolink:Gene"]
    )
    assert result["unresolved_source_records"] == ["unknown#row-1"]
    assert result["lineage"][0]["match"] == "candidate"
    assert "not an exact" in result["hint"]


def test_lineage_without_reports_preserves_biolink_provenance():
    result = LineageIndex().resolve_source_records(_edge(1).model_dump(exclude_none=True))
    assert result["lineage"] == []
    assert result["provenance"]["primary_knowledge_source"] == "infores:chembl"
    assert "unavailable" in result["hint"]


@pytest.mark.parametrize(
    ("shared_model", "vendored_model"),
    [
        (ColumnRef, VendoredColumnRef),
        (Mapping, VendoredMapping),
        (Skipped, VendoredSkipped),
        (TransformReport, VendoredTransformReport),
    ],
)
def test_vendored_report_model_fields_match(shared_model, vendored_model):
    assert shared_model.model_fields.keys() == vendored_model.model_fields.keys()
    for name, shared_field in shared_model.model_fields.items():
        vendored_field = vendored_model.model_fields[name]
        assert _annotation_shape(shared_field.annotation) == _annotation_shape(
            vendored_field.annotation
        )
        assert shared_field.is_required() == vendored_field.is_required()


def _annotation_shape(annotation):
    origin = get_origin(annotation)
    if origin in {types.UnionType, None}:
        if origin is None:
            return getattr(annotation, "__name__", repr(annotation))
        return ("union", *(_annotation_shape(item) for item in get_args(annotation)))
    return (origin.__name__, *(_annotation_shape(item) for item in get_args(annotation)))


def _toy_statistics() -> dict:
    return {
        "node_count": 10,
        "edge_count": 15,
        "categories": [
            {"category": "biolink:Gene", "count": 5},
            {"category": "biolink:SmallMolecule", "count": 5},
        ],
        "predicates": [{"predicate": "biolink:directly_physically_interacts_with", "count": 15}],
        "primary_knowledge_sources": [{"primary_knowledge_source": "infores:chembl", "count": 15}],
        "meta_graph": [
            {
                "subject_category": "biolink:SmallMolecule",
                "predicate": "biolink:directly_physically_interacts_with",
                "object_category": "biolink:Gene",
                "count": 15,
            }
        ],
        "properties": [
            {"category": "biolink:Gene", "property": "name", "example": "Gene 100"},
            {
                "category": "biolink:SmallMolecule",
                "property": "name",
                "example": "Compound 1",
            },
        ],
        "property_names": {
            "biolink:Gene": ["id", "name"],
            "biolink:SmallMolecule": ["id", "name"],
        },
        "edge_properties": [
            {"property": "kgbcia_flags", "example": ["wrong_mechanism"]},
            {"property": "kgbcia_flag_reasons", "example": ["PMID:1 reports another action"]},
        ],
        "flag_counts": [{"flag": "wrong_mechanism", "count": 1}],
    }


def _artifact_directory(root: Path) -> Path:
    report_path = root / "reports" / "chembl" / "transform_report.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(_report().model_dump_json())
    (root / "graph_metadata.json").write_text(
        json.dumps({"graph_id": "toy", "statistics": _toy_statistics()})
    )
    (root / "hubs.json").write_text('["NCBIGene:100"]')
    (root / "description.json").write_text(
        json.dumps(
            {
                "headline": {"nodes": 10},
                "graphs": ["/tmp/local"],
                "sources": [{"id": "chembl", "normalized_dir": "/tmp/norm"}],
            }
        )
    )
    manifest = {
        "graph_version": "toy-1",
        "files": [
            "graph_metadata.json",
            "hubs.json",
            "description.json",
            "reports/chembl/transform_report.json",
        ],
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    return root


def _tar_bytes(root: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                archive.add(path, arcname=path.relative_to(root))
    return buffer.getvalue()


def test_load_artifacts_from_directory(tmp_path):
    bundle = load_artifacts(_artifact_directory(tmp_path / "artifacts"))
    assert bundle.manifest.graph_version == "toy-1"
    assert bundle.graph_metadata["graph_id"] == "toy"
    assert bundle.graph_metadata["statistics"]["node_count"] == 10
    assert bundle.hubs == ["NCBIGene:100"]
    assert bundle.reports[0].source == "infores:chembl"
    assert bundle.description["headline"] == {"nodes": 10}


def test_url_artifacts_checksum_and_cache_reuse(tmp_path, monkeypatch):
    source = _artifact_directory(tmp_path / "source")
    archive = _tar_bytes(source)
    checksum = hashlib.sha256(archive).hexdigest()
    calls = 0

    def urlopen(_url):
        nonlocal calls
        calls += 1
        return io.BytesIO(archive)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    first = resolve_artifacts("https://example.test/artifacts.tar", checksum, tmp_path / "cache")
    second = resolve_artifacts("https://example.test/artifacts.tar", checksum, tmp_path / "cache")
    assert first == second
    assert calls == 1
    assert load_artifacts(first).reports


def test_url_artifacts_uses_kgb_cache_dir(tmp_path, monkeypatch):
    source = _artifact_directory(tmp_path / "source")
    archive = _tar_bytes(source)
    checksum = hashlib.sha256(archive).hexdigest()
    monkeypatch.setenv("KGB_CACHE_DIR", str(tmp_path / "kgb-cache"))
    monkeypatch.setattr("urllib.request.urlopen", lambda _url: io.BytesIO(archive))

    resolved = resolve_artifacts("https://example.test/artifacts.tar", checksum)

    assert resolved == tmp_path / "kgb-cache" / "mcp-artifacts" / checksum
    assert (resolved / "manifest.json").is_file()


def test_url_artifacts_reject_checksum_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda _url: io.BytesIO(b"not a tar"))
    with pytest.raises(ValueError, match="checksum mismatch"):
        resolve_artifacts("https://example.test/artifacts.tar", "0" * 64, tmp_path / "cache")


def test_settings_environment_and_overrides():
    settings = Settings.from_env(
        {"neo4j_uri": "neo4j://override", "enable_cypher": True},
        {
            "NEO4J_URI": "neo4j://environment",
            "NEO4J_USER": "reader",
            "NEO4J_PASSWORD": "secret",
            "KGB_MCP_ENABLE_CYPHER": "0",
        },
    )
    assert settings.neo4j_uri == "neo4j://override"
    assert settings.enable_cypher
    settings.require_neo4j()


def test_settings_allow_no_credentials_but_reject_partial_credentials():
    unauthenticated = Settings.from_env({"neo4j_uri": "bolt://example"}, {})
    unauthenticated.require_neo4j()

    partial = Settings.from_env({"neo4j_uri": "bolt://example", "neo4j_user": "reader"}, {})
    with pytest.raises(ValueError, match="must be set together"):
        partial.require_neo4j()


def test_neo4j_service_creates_driver_without_auth(monkeypatch):
    calls = []
    driver = object()
    monkeypatch.setattr(
        "kgbcia_mcp.services.neo4j_service.GraphDatabase.driver",
        lambda *args, **kwargs: calls.append((args, kwargs)) or driver,
    )

    service = Neo4jService("bolt://example", None, None)
    assert service._get_driver() is driver
    assert calls == [(("bolt://example",), {})]


def test_fulltext_assumption_includes_list_property():
    assert FULLTEXT_INDEX_PROPERTIES == ("name", "synonym", "equivalent_identifiers")


def test_quality_flag_pairs_cover_empty_multiple_and_mismatched(caplog):
    assert _quality_flags({}) == []
    assert _quality_flags(
        {
            "edge_id": "edge-1",
            "kgbcia_flags": ["wrong_mechanism", "type_violation"],
            "kgbcia_flag_reasons": ["PMID:1 says the action differs", "range mismatch"],
        }
    ) == [
        {"flag": "wrong_mechanism", "reason": "PMID:1 says the action differs"},
        {"flag": "type_violation", "reason": "range mismatch"},
    ]
    assert _quality_flags(
        {"edge_id": "edge-2", "kgbcia_flags": ["wrong_mechanism"], "kgbcia_flag_reasons": []}
    ) == [{"flag": "wrong_mechanism", "reason": None}]
    assert "mismatched" in caplog.text


def test_neo4j_serializer_handles_nodes_spatial_and_nested_values():
    graph = Neo4jGraph()
    node = Neo4jNode(
        graph,
        "node-1",
        1,
        {"biolink:NamedThing", "biolink:Gene"},
        {"id": "NCBIGene:100", "name": "Gene 100"},
    )
    serialized = serialize_neo4j_value({"node": node, "point": CartesianPoint((1, 2))})
    assert serialized["node"]["id"] == "NCBIGene:100"
    assert serialized["node"]["categories"] == ["biolink:Gene", "biolink:NamedThing"]
    assert serialized["point"] == {"srid": 7203, "x": 1.0, "y": 2.0}


class _FakeRecord:
    def __init__(self, value):
        self.value = value

    def items(self):
        return {"value": self.value}.items()


class _FakeTransaction:
    def run(self, _query, parameters):
        assert isinstance(_query, str)
        assert parameters == {"answer": 42}
        return [_FakeRecord(1), _FakeRecord(2), _FakeRecord(3)]


class _FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute_read(self, callback):
        return callback(_FakeTransaction())


class _FakeDriver:
    def __init__(self):
        self.closed = False

    def session(self, **_kwargs):
        return _FakeSession()

    def close(self):
        self.closed = True


def test_graph_info_and_schema_use_precomputed_statistics(monkeypatch):
    service = Neo4jService(
        "unused",
        "unused",
        "unused",
        driver=_FakeDriver(),
        graph_metadata={"graph_id": "toy", "statistics": _toy_statistics()},
    )

    def reject_aggregate_reads(*_args, **_kwargs):
        raise AssertionError("graph_info and schema must not scan Neo4j")

    monkeypatch.setattr(service, "execute_read", reject_aggregate_reads)
    info = service.get_graph_info()
    schema = service.get_schema()
    assert info["node_count"] == 10
    assert info["edge_count"] == 15
    assert info["primary_knowledge_sources"] == [
        {"primary_knowledge_source": "infores:chembl", "count": 15}
    ]
    assert info["flag_counts"] == [{"flag": "wrong_mechanism", "count": 1}]
    service_with_build = Neo4jService(
        "unused",
        "unused",
        "unused",
        driver=_FakeDriver(),
        graph_metadata={"statistics": _toy_statistics()},
        build_description={"headline": {"nodes": 10}},
    )
    assert service_with_build.get_graph_info()["build"] == {"headline": {"nodes": 10}}
    assert schema["meta_graph"][0]["subject_category"] == "biolink:SmallMolecule"
    assert schema["edge_properties"][0]["property"] == "kgbcia_flags"
    assert schema["flag_values"] == ["wrong_mechanism"]
    assert schema["property_names"]["biolink:Gene"] == ["id", "name"]


def test_single_fuzzy_fulltext_hit_is_not_silently_resolved(monkeypatch):
    service = Neo4jService("unused", "unused", "unused", driver=_FakeDriver())
    monkeypatch.setattr(
        service,
        "search_nodes",
        lambda *_args, **_kwargs: {
            "results": [
                {
                    "id": "NCBIGene:1017",
                    "name": "CDK2",
                    "categories": ["biolink:Gene"],
                    "match": "fulltext",
                    "exact_match": False,
                }
            ]
        },
    )
    assert service._resolve_node("CDK")["status"] == "ambiguous"


def test_neo4j_service_uses_read_transaction_and_caps_results():
    driver = _FakeDriver()
    service = Neo4jService("unused", "unused", "unused", driver=driver)
    result = service.execute_read("RETURN $answer", {"answer": 42}, row_limit=2)
    assert result.rows == ({"value": 1}, {"value": 2})
    assert result.truncated
    service.close()
    assert driver.closed


def test_server_package_does_not_import_repo_packages():
    source_root = Path("agents/mcp_interface/src/kgbcia_mcp")
    for path in source_root.rglob("*.py"):
        text = path.read_text()
        assert "from shared" not in text
        assert "import shared" not in text
        assert "from agents" not in text
        assert "import agents" not in text


class _MockToolService:
    def __init__(self):
        self.calls = []

    def _result(self, name, *args):
        self.calls.append((name, args))
        return {"tool": name, "args": list(args)}

    def get_graph_info(self):
        return self._result("get_graph_info")

    def get_schema(self):
        return self._result("get_schema")

    def search_nodes(self, *args):
        if args[0] == "raise-error":
            raise ValueError("unknown category; valid values: biolink:Gene")
        return self._result("search_nodes", *args)

    def get_node(self, *args):
        return self._result("get_node", *args)

    def summarize_node_edges(self, *args):
        return self._result("summarize_node_edges", *args)

    def get_node_edges(self, *args):
        return self._result("get_node_edges", *args)

    def get_edges_between(self, *args):
        return self._result("get_edges_between", *args)

    def find_paths(self, *args):
        return self._result("find_paths", *args)

    def find_connected_nodes(self, *args):
        return self._result("find_connected_nodes", *args)

    def explain_edge(self, *args):
        return self._result("explain_edge", *args)

    def query_cypher(self, *args):
        return self._result("query_cypher", *args)


def test_every_tool_through_fastmcp_client_with_mocked_service():
    async def exercise():
        from fastmcp import Client

        service = _MockToolService()
        mcp = create_server(service, enable_cypher=True)
        calls = {
            "get_graph_info": {},
            "get_schema": {},
            "search_nodes": {"query": "NCBIGene:1017"},
            "get_node": {"node_id": "NCBIGene:1017"},
            "summarize_node_edges": {"node_id": "NCBIGene:1017"},
            "get_node_edges": {"node_id": "NCBIGene:1017", "limit": 5, "offset": 1},
            "get_edges_between": {
                "source_id": "CHEBI:45783",
                "target_id": "NCBIGene:1017",
                "include_lineage": True,
            },
            "find_paths": {
                "source_id": "CHEBI:45783",
                "target_id": "MONDO:0005148",
                "max_hops": 3,
            },
            "find_connected_nodes": {
                "source_id": "CHEBI:45783",
                "target_category": "biolink:Disease",
            },
            "explain_edge": {"edge_id": "edge-1"},
            "query_cypher": {"cypher": "MATCH (n) RETURN count(n) AS count"},
        }
        async with Client(mcp) as client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools} == set(calls)
            for tool in tools:
                assert "Presentation Guidelines" in (tool.description or "")
                result = await client.call_tool(tool.name, calls[tool.name])
                assert result.data["tool"] == tool.name
        assert len(service.calls) == len(calls)

    asyncio.run(exercise())


def test_server_starts_without_neo4j_and_tools_return_configuration_error(monkeypatch, capsys):
    async def exercise():
        from fastmcp import Client

        driver_calls = []
        monkeypatch.setattr(
            "kgbcia_mcp.services.neo4j_service.GraphDatabase.driver",
            lambda *args, **kwargs: driver_calls.append((args, kwargs)),
        )
        mcp = create_server(settings=Settings(), enable_cypher=True)
        assert driver_calls == []
        async with Client(mcp) as client:
            graph_result = await client.call_tool("get_graph_info")
            cypher_result = await client.call_tool(
                "query_cypher", {"cypher": "MATCH (n) RETURN n LIMIT 1"}
            )
        expected_error = "Neo4j not configured"
        assert "precomputed graph statistics" in graph_result.data["hint"]
        assert cypher_result.data["error"] == expected_error
        assert "NEO4J_URI" in cypher_result.data["hint"]
        assert driver_calls == []

    asyncio.run(exercise())
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Neo4j not configured" in captured.err


def test_run_enters_mcp_transport_without_neo4j_settings(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "fastmcp.FastMCP.run",
        lambda self, **kwargs: calls.append(kwargs),
    )
    server_module.run(settings=Settings())
    assert calls == [{"transport": "stdio", "show_banner": False}]


def test_missing_precomputed_statistics_returns_rebuild_hint(monkeypatch):
    service = Neo4jService("unused", "unused", "unused", driver=_FakeDriver(), graph_metadata={})
    monkeypatch.setattr(
        service,
        "execute_read",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no aggregate scans")),
    )
    info = service.get_graph_info()
    schema = service.get_schema()
    assert "precomputed graph statistics" in info["hint"]
    assert "precomputed graph statistics" in schema["hint"]
    with pytest.raises(ValueError, match="precomputed graph statistics"):
        service._validate_name("category", "biolink:Gene")


def test_unreachable_neo4j_returns_structured_error():
    async def exercise():
        from fastmcp import Client

        class UnreachableDriver:
            def session(self, **_kwargs):
                raise OSError("connection refused")

            def close(self):
                return None

        service = Neo4jService("neo4j://example", "reader", "secret", driver=UnreachableDriver())
        async with Client(create_server(service)) as client:
            result = await client.call_tool("get_node", {"node_id": "NCBIGene:1"})
        assert result.data["error"] == "Neo4j connection failure"
        assert result.data["cause"] == "connection_failure"
        assert "connection refused" in result.data["detail"]
        assert "server is still running" in result.data["hint"]

    asyncio.run(exercise())


def test_neo4j_failure_classifier_distinguishes_timeout_and_query_error():
    class TimeoutFailure(Exception):
        code = "Neo.TransientError.Transaction.TransactionTimedOut"

        def __str__(self):
            return "transaction timed out after 30s"

    class QueryFailure(Exception):
        code = "Neo.ClientError.Statement.SyntaxError"

        def __str__(self):
            return "invalid input"

    timeout = _classify_neo4j_failure(TimeoutFailure())
    query = _classify_neo4j_failure(QueryFailure())
    assert timeout.cause == "timeout"
    assert str(timeout) == "Neo4j query timed out"
    assert query.cause == "query_error"
    assert str(query) == "Neo4j query error"


def test_fastmcp_error_hint_and_optional_cypher_registration():
    async def exercise():
        from fastmcp import Client

        service = _MockToolService()
        mcp = create_server(service, enable_cypher=False)
        async with Client(mcp) as client:
            tools = await client.list_tools()
            assert "query_cypher" not in {tool.name for tool in tools}
            result = await client.call_tool("search_nodes", {"query": "raise-error"})
            assert "valid values" in result.data["hint"]
            assert "get_schema" in result.data["suggestion"]

    asyncio.run(exercise())


def test_server_instructions_define_provenance_workflow():
    assert "search_nodes" in SERVER_INSTRUCTIONS
    assert "include_lineage=true" in SERVER_INSTRUCTIONS
    assert "verbatim" in SERVER_INSTRUCTIONS
    assert "query_cypher" in SERVER_INSTRUCTIONS


def test_query_cypher_rejects_write_before_driver_access():
    service = Neo4jService("unused", "unused", "unused", driver=_FakeDriver())
    with pytest.raises(ValueError, match="write"):
        service.query_cypher("CREATE (n)")


def test_query_cypher_uses_documented_read_timeout(monkeypatch):
    service = Neo4jService("unused", "unused", "unused", driver=_FakeDriver())
    calls = []
    monkeypatch.setattr(service, "_explain_read_only", lambda *_args: None)

    def execute_read(cypher, parameters, *, timeout, row_limit):
        calls.append(
            {
                "cypher": cypher,
                "parameters": parameters,
                "timeout": timeout,
                "row_limit": row_limit,
            }
        )
        return ReadResult(({"answer": 42},), truncated=False)

    monkeypatch.setattr(service, "execute_read", execute_read)
    assert service.query_cypher("RETURN 42 AS answer", limit=1)["results"] == [{"answer": 42}]
    assert calls == [
        {
            "cypher": "CALL { RETURN 42 AS answer } RETURN * LIMIT $kgb_mcp_limit",
            "parameters": {"kgb_mcp_limit": 2},
            "timeout": _READ_TIMEOUT_SECONDS,
            "row_limit": 2,
        }
    ]
    assert _READ_TIMEOUT_SECONDS == 30.0


def test_stage_cli_runs_as_a_direct_script():
    result = subprocess.run(
        [sys.executable, "agents/mcp_interface/cli.py", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "--input" in result.stdout
    assert "ImportError" not in result.stderr


def test_stage_cli_delegates_server_commands(monkeypatch):
    calls = []
    monkeypatch.setattr(stage_cli_module, "server_main", lambda args: calls.append(args) or 7)
    assert stage_main(["serve", "--enable-cypher"]) == 7
    assert stage_main(["hubs", "--top", "10", "--output", "out"]) == 7
    assert calls == [
        ["serve", "--enable-cypher"],
        ["hubs", "--top", "10", "--output", "out"],
    ]


def test_published_cli_delegates_serve_with_overrides(monkeypatch):
    calls = []
    monkeypatch.setattr(
        published_cli_module,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    assert (
        published_cli_module.main(
            ["serve", "--transport", "http", "--port", "9000", "--artifacts", "/tmp/a"]
        )
        == 0
    )
    args, kwargs = calls[0]
    assert args[:3] == ("http", "127.0.0.1", 9000)
    assert kwargs["settings"].artifacts == "/tmp/a"


def test_stage_cli_check_and_default_missing_input(toy_graph, tmp_path, monkeypatch, capsys):
    graph_dir, report_path, _, _ = toy_graph
    repo = tmp_path / "repo"
    discovered = repo / "data" / "transformation" / "chembl" / "transform_report.json"
    discovered.parent.mkdir(parents=True)
    discovered.write_text(report_path.read_text())
    monkeypatch.chdir(repo)
    assert stage_main(["check", "--input", str(graph_dir)]) == 0
    assert "ready for Neo4j dump preparation" in capsys.readouterr().out

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    assert stage_main([]) == 1
    assert "no graph input found" in capsys.readouterr().out


def _service_with_cached_schema(*, hubs=()):
    service = Neo4jService("unused", "unused", "unused", driver=_FakeDriver(), hubs=hubs)
    service.cache_set(
        "schema",
        {
            "categories": [
                "biolink:SmallMolecule",
                "biolink:Gene",
                "biolink:Disease",
            ],
            "predicates": ["biolink:directly_physically_interacts_with"],
            "property_names": {
                "biolink:Gene": ["name", "symbol"],
                "biolink:Disease": ["name"],
            },
            "flag_values": ["wrong_mechanism"],
        },
    )
    return service


def _resolved_node(node_id, category="biolink:Gene"):
    return {
        "status": "found",
        "node": {"id": node_id, "name": node_id, "categories": [category]},
    }


def test_tool_pagination_totals_truncation_and_hard_cap(monkeypatch):
    async def exercise():
        from fastmcp import Client

        service = _service_with_cached_schema()
        monkeypatch.setattr(service, "_resolve_node", lambda node_id: _resolved_node(node_id))
        observed = []

        def execute(cypher, parameters=None, **kwargs):
            observed.append((cypher, parameters, kwargs))
            rows = tuple(
                {
                    "neighbor": {
                        "id": f"NCBIGene:{index}",
                        "name": f"Gene {index}",
                        "categories": ["biolink:Gene"],
                    },
                    "edge": {
                        "edge_id": f"edge-{index}",
                        "predicate": "biolink:directly_physically_interacts_with",
                        "primary_knowledge_source": "infores:chembl",
                        "publication_count": 1,
                    },
                    "direction": "outgoing",
                    "publication_count": 1,
                }
                for index in range(6)
            )
            return ReadResult(rows, False)

        monkeypatch.setattr(service, "execute_read", execute)
        async with Client(create_server(service)) as client:
            result = await client.call_tool(
                "get_node_edges",
                {"node_id": "NCBIGene:1017", "limit": 5, "offset": 7},
            )
            at_cap = await client.call_tool(
                "get_node_edges",
                {"node_id": "NCBIGene:1017", "limit": 500},
            )
            over_cap = await client.call_tool(
                "get_node_edges",
                {"node_id": "NCBIGene:1017", "limit": 501},
            )
        assert len(result.data["results"]) == 5
        assert result.data["offset"] == 7
        assert result.data["total"] == 13
        assert result.data["truncated"] is True
        assert result.data["total_is_lower_bound"] is True
        assert observed[0][1]["fetch"] == 6
        assert observed[0][2]["row_limit"] == 6
        assert len(at_cap.data["results"]) == 6
        assert "error" not in at_cap.data
        assert "between 1 and 500" in over_cap.data["hint"]

    asyncio.run(exercise())


def test_tool_direction_and_neighbor_filter_validation(monkeypatch):
    async def exercise():
        from fastmcp import Client

        service = _service_with_cached_schema()
        monkeypatch.setattr(service, "_resolve_node", lambda node_id: _resolved_node(node_id))
        observed = []

        def execute(cypher, parameters=None, **_kwargs):
            observed.append((cypher, parameters))
            return ReadResult((), False)

        monkeypatch.setattr(service, "execute_read", execute)
        async with Client(create_server(service)) as client:
            valid = await client.call_tool(
                "get_node_edges",
                {
                    "node_id": "NCBIGene:1017",
                    "direction": "outgoing",
                    "predicate": "biolink:directly_physically_interacts_with",
                    "neighbor_category": "biolink:Gene",
                    "neighbor_where": {"symbol": "CDK2"},
                    "exclude_flags": ["wrong_mechanism"],
                },
            )
            bad_direction = await client.call_tool(
                "get_node_edges",
                {"node_id": "NCBIGene:1017", "direction": "sideways"},
            )
            bad_predicate = await client.call_tool(
                "get_node_edges",
                {"node_id": "NCBIGene:1017", "predicate": "biolink:not_real"},
            )
            bad_property = await client.call_tool(
                "get_node_edges",
                {
                    "node_id": "NCBIGene:1017",
                    "neighbor_category": "biolink:Gene",
                    "neighbor_where": {"not_real": "x"},
                },
            )
        assert valid.data["results"] == []
        query, parameters = observed[0]
        assert "-[r]->" in query
        assert "neighbor.`symbol` = $neighbor_value" in query
        assert "kgbcia_flags" in query
        assert parameters["predicate"] == "biolink:directly_physically_interacts_with"
        assert parameters["neighbor_category"] == "biolink:Gene"
        assert parameters["neighbor_value"] == "CDK2"
        assert parameters["exclude_flags"] == ["wrong_mechanism"]
        assert "direction must be" in bad_direction.data["hint"]
        assert "unknown predicate" in bad_predicate.data["hint"]
        assert "unknown property" in bad_property.data["hint"]

    asyncio.run(exercise())


def test_path_tools_enforce_hop_limits_and_exclude_hubs(monkeypatch):
    async def exercise():
        from fastmcp import Client

        service = _service_with_cached_schema(hubs=("NCBIGene:HUB",))

        def resolve(node_id):
            category = "biolink:SmallMolecule" if node_id == "CHEBI:1" else "biolink:Disease"
            return _resolved_node(node_id, category)

        monkeypatch.setattr(service, "_resolve_node", resolve)
        observed = []

        def execute(cypher, parameters=None, **_kwargs):
            observed.append((cypher, parameters))
            if "target:`biolink:NamedThing`" in cypher:
                depth = 2 if "[*2]" in cypher else 1
                if depth == 2:
                    return ReadResult(
                        (
                            {
                                "target": _resolved_node("MONDO:1", "biolink:Disease")["node"],
                                "path": {"nodes": [], "edges": []},
                            },
                        ),
                        False,
                    )
                return ReadResult((), False)
            if "[*2]" in cypher:
                return ReadResult(({"path": {"nodes": [], "edges": []}},), False)
            return ReadResult((), False)

        monkeypatch.setattr(service, "execute_read", execute)
        async with Client(create_server(service)) as client:
            paths = await client.call_tool(
                "find_paths",
                {
                    "source_id": "CHEBI:1",
                    "target_id": "MONDO:1",
                    "max_hops": 3,
                    "exclude_flags": ["wrong_mechanism"],
                },
            )
            connected = await client.call_tool(
                "find_connected_nodes",
                {
                    "source_id": "CHEBI:1",
                    "target_category": "biolink:Disease",
                    "max_hops": 3,
                    "exclude_flags": ["wrong_mechanism"],
                },
            )
            bad_paths = await client.call_tool(
                "find_paths",
                {"source_id": "CHEBI:1", "target_id": "MONDO:1", "max_hops": 5},
            )
            bad_connected = await client.call_tool(
                "find_connected_nodes",
                {
                    "source_id": "CHEBI:1",
                    "target_category": "biolink:Disease",
                    "max_hops": 4,
                },
            )
        assert paths.data["hops"] == 2
        assert connected.data["hops"] == 2
        assert "between 1 and 4" in bad_paths.data["hint"]
        assert "between 1 and 3" in bad_connected.data["hint"]
        assert observed
        for query, parameters in observed:
            assert "nodes(p)[1..-1]" in query
            assert "kgbcia_flags" in query
            assert parameters["hubs"] == ["NCBIGene:HUB"]
            assert parameters["exclude_flags"] == ["wrong_mechanism"]

    asyncio.run(exercise())


def test_lineage_tools_cover_direct_edges_and_exact_and_candidate_explanations(monkeypatch):
    async def exercise():
        from fastmcp import Client

        report = VendoredTransformReport.model_validate(_report().model_dump())
        service = _service_with_cached_schema()
        service.lineage = LineageIndex([report])

        def resolve(node_id):
            category = "biolink:SmallMolecule" if node_id.startswith("CHEBI:") else "biolink:Gene"
            return _resolved_node(node_id, category)

        monkeypatch.setattr(service, "_resolve_node", resolve)

        def edge(source_record):
            return {
                "edge_id": "edge-1",
                "subject": "CHEBI:1",
                "predicate": "biolink:directly_physically_interacts_with",
                "object": "NCBIGene:1017",
                "primary_knowledge_source": "infores:chembl",
                "aggregator_knowledge_source": ["infores:tcrd"],
                "knowledge_level": "knowledge_assertion",
                "agent_type": "data_analysis_pipeline",
                "publications": ["PMID:1"],
                "publication_count": 1,
                "kgbcia_source_records": [source_record],
                "kgbcia_flags": ["wrong_mechanism"],
                "kgbcia_flag_reasons": ["PMID:20371707 reports a different action"],
                "flags": [
                    {
                        "flag": "wrong_mechanism",
                        "reason": "PMID:20371707 reports a different action",
                    }
                ],
            }

        def execute(cypher, parameters=None, **_kwargs):
            if "CASE WHEN startNode" in cypher:
                return ReadResult(
                    (
                        {
                            "edge": edge("chembl.interacts#row-1"),
                            "direction": "outgoing",
                            "publication_count": 1,
                        },
                    ),
                    False,
                )
            source_record = (
                "chembl.interacts#row-1"
                if parameters["edge_id"] == "edge-exact"
                else "unknown.mapping#row-2"
            )
            return ReadResult(
                (
                    {
                        "edge": edge(source_record),
                        "subject_categories": ["biolink:SmallMolecule"],
                        "object_categories": ["biolink:Gene"],
                    },
                ),
                False,
            )

        monkeypatch.setattr(service, "execute_read", execute)
        async with Client(create_server(service)) as client:
            direct = await client.call_tool(
                "get_edges_between",
                {
                    "source_id": "CHEBI:1",
                    "target_id": "NCBIGene:1017",
                    "include_lineage": True,
                },
            )
            exact = await client.call_tool("explain_edge", {"edge_id": "edge-exact"})
            candidate = await client.call_tool("explain_edge", {"edge_id": "edge-candidate"})
        direct_lineage = direct.data["results"][0]["lineage"]["lineage"]
        assert direct_lineage[0]["match"] == "exact"
        assert direct_lineage[0]["record_keys"] == ["row-1"]
        assert direct.data["results"][0]["edge"]["flags"][0]["flag"] == "wrong_mechanism"
        assert exact.data["edge"]["flags"][0]["reason"].startswith("PMID:20371707")
        assert exact.data["lineage"][0]["match"] == "exact"
        assert candidate.data["lineage"][0]["match"] == "candidate"
        assert candidate.data["unresolved_source_records"] == ["unknown.mapping#row-2"]
        assert "not an exact" in candidate.data["hint"]

    asyncio.run(exercise())


def test_make_dev_artifacts_creates_synthetic_lineage(tmp_path):
    module = runpy.run_path(
        Path(__file__).parents[1] / "agents/mcp_interface/deploy/make_dev_artifacts.py"
    )
    input_dir = tmp_path / "sample"
    input_dir.mkdir()
    nodes = [
        {"id": "CHEBI:1", "category": ["biolink:SmallMolecule", "biolink:NamedThing"]},
        {"id": "NCBIGene:2", "category": ["biolink:Gene", "biolink:NamedThing"]},
    ]
    edges = [
        {
            "subject": "CHEBI:1",
            "predicate": "biolink:related_to",
            "object": "NCBIGene:2",
            "primary_knowledge_source": "infores:example",
        }
        for _ in range(25)
    ]
    (input_dir / "nodes.jsonl").write_text("".join(json.dumps(node) + "\n" for node in nodes))
    (input_dir / "edges.jsonl").write_text("".join(json.dumps(edge) + "\n" for edge in edges))

    output_dir = tmp_path / "synthetic"
    counts = module["create_dev_artifacts"](input_dir, output_dir)

    assert counts == {"nodes": 2, "edges": 25, "reports": 1}
    report_path = next((output_dir / "reports").glob("**/transform_report.json"))
    report = json.loads(report_path.read_text())
    assert check_graph(output_dir, [report_path]).ok
    assert report["source_version"] == "synthetic"
    mapping = report["mappings"][0]
    assert (mapping["subject_category"], mapping["object_category"]) == (
        "biolink:SmallMolecule",
        "biolink:Gene",
    )
    rewritten = [json.loads(line) for line in (output_dir / "edges.jsonl").read_text().splitlines()]
    assert all(edge["id"].startswith("synthetic-") for edge in rewritten)
    assert all(isinstance(edge["kgbcia_source_records"], list) for edge in rewritten)
    assert rewritten[0]["kgbcia_flags"] == ["wrong_mechanism"]
    assert len(rewritten[1]["kgbcia_flags"]) == 2
    assert len(rewritten[2]["kgbcia_flags"]) != len(rewritten[2]["kgbcia_flag_reasons"])
    assert len(rewritten[-1]["kgbcia_source_records"]) == 2


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")
def test_helm_chart_lints_and_templates_all_values_files():
    chart = Path(__file__).parents[1] / "agents/mcp_interface/deploy/helm/kgb-mcp"
    rendered = {}
    for values_name in (
        "values.yaml",
        "values-dev.yaml",
        "values-aws.yaml",
        "../../aws/values-aws-test.yaml",
    ):
        values = chart / values_name
        lint = subprocess.run(
            ["helm", "lint", str(chart), "-f", str(values)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert lint.returncode == 0, lint.stdout + lint.stderr
        template = subprocess.run(
            ["helm", "template", "kgb-mcp", str(chart), "-f", str(values)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert template.returncode == 0, template.stdout + template.stderr
        assert "/data/import/neo4j.dump" in template.stdout
        assert ".kgbcia-load.sha256" in template.stdout
        rendered[values_name] = template.stdout

    assert "name: NEO4J_AUTH\n              valueFrom:" in rendered["values.yaml"]
    assert "kgb-mcp-create-user" in rendered["values.yaml"]
    for values_name in ("values-aws.yaml", "../../aws/values-aws-test.yaml"):
        assert "name: NEO4J_AUTH\n              value: none" in rendered[values_name]
        assert "secretKeyRef:" not in rendered[values_name]
        assert "kgb-mcp-create-user" not in rendered[values_name]

    no_auth_mcp = subprocess.run(
        [
            "helm",
            "template",
            "kgb-mcp",
            str(chart),
            "-f",
            str(chart / "values-aws.yaml"),
            "--set",
            "mcp.enabled=true",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert no_auth_mcp.returncode == 0, no_auth_mcp.stdout + no_auth_mcp.stderr
    assert "name: NEO4J_USER" not in no_auth_mcp.stdout
    assert "name: NEO4J_PASSWORD" not in no_auth_mcp.stdout


requires_neo4j = pytest.mark.skipif(
    not os.getenv("NEO4J_URI") or not os.getenv("KGB_MCP_ARTIFACTS"),
    reason="set NEO4J_URI and KGB_MCP_ARTIFACTS to run the Neo4j integration smoke test",
)


@requires_neo4j
def test_integration_smoke_against_configured_neo4j():
    """Exercise the deployed graph, provenance, and read-only enforcement."""

    settings = Settings.from_env()
    settings.require_neo4j()
    bundle = load_artifacts(settings.artifacts or "", settings.artifacts_sha256)
    service = Neo4jService(
        settings.neo4j_uri,
        settings.neo4j_user,
        settings.neo4j_password,
        settings.neo4j_database,
        lineage=LineageIndex(bundle.reports),
        graph_metadata=bundle.graph_metadata,
        hubs=server_module._hub_ids(bundle.hubs),
    )
    try:
        info = service.get_graph_info()
        schema = service.get_schema()
        assert info["node_count"] > 0
        assert info["edge_count"] > 0
        assert "biolink:NamedThing" not in schema["categories"]
        assert schema["predicates"]

        edge_row = service.execute_read(
            """
            MATCH (source:`biolink:NamedThing`)-[edge]->(target:`biolink:NamedThing`)
            RETURN source.id AS source_id, target.id AS target_id, edge.id AS edge_id,
                   [label IN labels(target) WHERE label <> $anchor][0] AS target_category
            LIMIT 1
            """,
            {"anchor": "biolink:NamedThing"},
            row_limit=1,
        ).rows[0]
        source_id = edge_row["source_id"]
        target_id = edge_row["target_id"]
        edge_id = edge_row["edge_id"]

        assert service.search_nodes(source_id)["results"]
        assert service.get_node(source_id)["node"]["id"] == source_id
        summary = service.summarize_node_edges(source_id)
        assert summary["total"] > 0
        assert " → " in summary["results"][0]["reads_as"]
        node_edges = service.get_node_edges(source_id)
        assert node_edges["results"]
        assert source_id in node_edges["results"][0]["reads_as"]
        assert node_edges["results"][0]["edge"]["primary_knowledge_source"]
        direct = service.get_edges_between(source_id, target_id, include_lineage=True)
        assert direct["results"]
        assert direct["results"][0]["lineage"]["provenance"]["primary_knowledge_source"]
        assert service.find_paths(source_id, target_id, max_hops=1)["results"]
        assert service.find_connected_nodes(source_id, edge_row["target_category"], max_hops=1)[
            "results"
        ]
        explanation = service.explain_edge(edge_id)
        assert explanation["provenance"]["primary_knowledge_source"]
        assert service.query_cypher("MATCH (n:`biolink:NamedThing`) RETURN count(n) AS node_count")[
            "results"
        ]
        with pytest.raises(ValueError, match="write and administrative"):
            service.query_cypher("CREATE (:`biolink:NamedThing` {id: 'forbidden'})")
    finally:
        service.close()

    driver_options = (
        {"auth": (settings.neo4j_user, settings.neo4j_password)} if settings.neo4j_user else {}
    )
    driver = GraphDatabase.driver(settings.neo4j_uri, **driver_options)
    try:
        with pytest.raises(Neo4jError):
            with driver.session(database=settings.neo4j_database) as session:
                session.execute_write(
                    lambda transaction: transaction.run(
                        "CREATE (:`biolink:NamedThing` {id: 'kgbcia:forbidden-write'})"
                    ).consume()
                )
    finally:
        driver.close()


def test_dump_scripts_pin_images_and_create_idempotent_indexes():
    root = Path(__file__).parents[1] / "agents/mcp_interface/deploy"
    preparation = (root / "prepare_dump.sh").read_text()
    indexes = (root / "extra_indexes.cypher").read_text()
    assert "neo4j:5.26.31-community" in preparation
    orion_image = (
        "ghcr.io/robokopu24/orion@sha256:"
        "913006ca778f3279e1563078bb25801714db62778e3e35d6e33062eb057b6994"
    )
    assert orion_image in preparation
    assert "orion-neo4j-dump /input/nodes.jsonl /input/edges.jsonl /work/orion" in preparation
    assert "--artifacts-only" in preparation
    assert "--existing-dump" in preparation
    assert "--description" in preparation
    assert 'cp "$existing_dump" "$output_dir/kgb.dump"' in preparation
    assert "description.json" in preparation
    assert 'value.pop("graphs", None)' in preparation
    assert 'item.pop("normalized_dir", None)' in preparation
    assert "kgbcia-mcp hubs" in preparation
    assert "precomputed meta_graph rows" in preparation
    assert "category_order" in preparation
    assert "primary_knowledge_sources" in preparation
    assert "MAX_STRING = 100" in preparation
    assert "MAX_LIST = 3" in preparation
    assert "cypher-shell --format plain" in preparation
    assert "sed '1d'" in preparation
    assert 'docker exec "$container_name" mkdir -p /data/dump-export' in preparation
    assert 'neo4j-admin database dump "$DATABASE" --to-path=/data/dump-export' in preparation
    assert 'docker cp "$dump_container:/data/dump-export/$DATABASE.dump"' in preparation
    assert "kgbcia_relationship_id_" in preparation
    assert "CREATE FULLTEXT INDEX kgbcia_node_fulltext IF NOT EXISTS" in indexes
    assert "CALL db.awaitIndexes()" in indexes


def test_check_accepts_augmentor_edges_without_source_records(toy_graph):
    graph_dir, report_path, _, edges = toy_graph
    values = [edge.model_dump(exclude_none=True) for edge in edges]
    proposed = {
        "id": "kgbcia-augmentation:0123456789abcdef",
        "subject": "CHEBI:1",
        "predicate": "biolink:directly_physically_interacts_with",
        "object": "NCBIGene:100",
        "primary_knowledge_source": "infores:kgbcia-augmentor",
        "knowledge_level": "knowledge_assertion",
        "agent_type": "text_mining_agent",
        "publications": ["PMID:12345"],
        "original_edge_ids": ["edge-0"],
    }
    # Each of these lacks one condition, so it must still fail as missing lineage.
    no_publications = {**proposed, "id": "kgbcia-augmentation:1", "publications": []}
    no_originals = {**proposed, "id": "kgbcia-augmentation:2"}
    no_originals.pop("original_edge_ids")
    other_source = {
        **proposed,
        "id": "kgbcia-augmentation:3",
        "primary_knowledge_source": "infores:chembl",
    }
    values += [proposed, no_publications, no_originals, other_source]
    (graph_dir / "edges.jsonl").write_text("".join(f"{json.dumps(value)}\n" for value in values))

    result = check_graph(graph_dir, [report_path])

    assert result.augmentor_edges_without_records == 1
    assert result.error_counts["missing_source_records"] == 3
    assert result.missing_source_records_by_primary_source == {
        "infores:chembl": 1,
        "infores:kgbcia-augmentor": 2,
    }
    assert not result.ok
    assert "augmentor edges without kgbcia_source_records" in format_check_result(result)


def test_check_passes_graph_whose_only_unrecorded_edges_are_augmentor_proposals(toy_graph):
    graph_dir, report_path, _, edges = toy_graph
    values = [edge.model_dump(exclude_none=True) for edge in edges]
    values.append(
        {
            "id": "kgbcia-augmentation:fedcba9876543210",
            "subject": "CHEBI:2",
            "predicate": "biolink:directly_physically_interacts_with",
            "object": "NCBIGene:101",
            "primary_knowledge_source": "infores:kgbcia-augmentor",
            "publications": ["PMID:67890"],
            "original_edge_ids": ["edge-1"],
        }
    )
    (graph_dir / "edges.jsonl").write_text("".join(f"{json.dumps(value)}\n" for value in values))

    result = check_graph(graph_dir, [report_path])

    assert result.ok
    assert result.edge_count == 16
    assert result.augmentor_edges_without_records == 1


def test_reads_as_puts_the_anchor_on_the_correct_end():
    from kgbcia_mcp.services.neo4j_service import _node_label, _reads_as

    cdk2 = _node_label({"id": "NCBIGene:1017", "name": "CDK2"})
    assert cdk2 == "CDK2 (NCBIGene:1017)"
    assert _node_label({"id": "X:1"}) == "X:1"
    assert (
        _reads_as(cdk2, "biolink:expressed_in", "biolink:AnatomicalEntity", "outgoing")
        == "CDK2 (NCBIGene:1017) → biolink:expressed_in → biolink:AnatomicalEntity"
    )
    assert (
        _reads_as(cdk2, "biolink:has_participant", "biolink:Pathway", "incoming")
        == "biolink:Pathway → biolink:has_participant → CDK2 (NCBIGene:1017)"
    )
