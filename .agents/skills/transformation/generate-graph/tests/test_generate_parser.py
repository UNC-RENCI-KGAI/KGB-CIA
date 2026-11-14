"""Metadata-driven code generation and native ORION loader integration, with synthetic data."""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import io
import json
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from shared.schema import Edge, Node, TransformReport

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SPEC = importlib.util.spec_from_file_location("generate_parser", SCRIPTS / "generate_parser.py")
generator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generator)


def field(name, slot, *, prefix=None, category=None, extract=None, file_id="file", value=None):
    result = {
        "@id": "rs/" + name,
        "name": name,
        "equivalentProperty": "biolink:" + slot,
        "dataType": ["sc:Text"] + ([category] if category else []),
    }
    if value is not None:
        result["value"] = value
    else:
        result["source"] = {"fileObject": {"@id": file_id}, "extract": extract or {"column": name}}
    if prefix:
        result["annotation"] = [{"name": "id_prefix", "value": prefix}]
    return result


def group(name, role, category, columns):
    return {
        "@id": "rs/" + name,
        "name": name,
        "equivalentProperty": role,
        "dataType": [category],
        "subField": columns,
    }


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / "synthetic_source"
    root.mkdir()
    gene = group(
        "gene", "biolink:subject", "biolink:Gene", [field("gene_id", "id", prefix="NCBIGene")]
    )
    disease = group(
        "disease", "biolink:object", "biolink:Disease", [field("disease_id", "id", prefix="MONDO")]
    )
    doc = {
        "@context": {},
        "name": "Synthetic source",
        "version": "1",
        "distribution": [
            {
                "@id": "file",
                "@type": "cr:FileObject",
                "name": "records.tsv",
                "contentUrl": "https://example.test/records.tsv",
                "encodingFormat": "text/tab-separated-values",
                "csvw:dialect": {"csvw:delimiter": "\t", "csvw:header": True},
            }
        ],
        "recordSet": [
            {
                "@id": "rs",
                "name": "Association records",
                "rcv:planLayout": "association",
                "dataType": ["biolink:GeneToDiseaseAssociation"],
                "field": [
                    gene,
                    disease,
                    field("predicate", "predicate", value="biolink:associated_with"),
                    field("primary", "primary_knowledge_source", value="infores:synthetic"),
                    field("publications", "publications"),
                ],
            }
        ],
    }
    path = root / "croissant.semantic.json"
    path.write_text(json.dumps(doc))
    (root / "semantics_plan.json").write_text(
        json.dumps({"kb": "shared-kb", "biolink_version": "v4.4.6"})
    )
    return path, doc


def save(bundle):
    path, document = bundle
    path.write_text(json.dumps(document))
    return path


def load_generated(path):
    spec = importlib.util.spec_from_file_location(
        "test_generated_parser_" + path.parent.parent.name, path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.GeneratedLoader


@pytest.fixture
def native_orion(monkeypatch, tmp_path):
    pytest.importorskip(
        "orion.loader_interface",
        reason="Native integration needs Python 3.12+ and robokop-orion==2.0.13",
    )
    monkeypatch.setenv("ORION_STORAGE", str(tmp_path / "orion-storage"))

    def no_network(*args, **kwargs):
        pytest.fail("Native parser tests must not download data or call NodeNorm")

    monkeypatch.setattr("urllib.request.urlretrieve", no_network)
    monkeypatch.setattr("urllib.request.urlopen", no_network)
    monkeypatch.setattr("requests.sessions.Session.request", no_network)


def run_loader(bundle, tmp_path, monkeypatch, native_orion, text):
    path = save(bundle)
    output = tmp_path / "generated"
    result = generator.generate(path, output)
    data = tmp_path / "records.tsv"
    data.write_text(text)
    bindings = tmp_path / "files.json"
    bindings.write_text(json.dumps({"file": str(data)}))
    monkeypatch.setenv("KGB_CIA_SOURCE_FILES", str(bindings))
    workspace = tmp_path / "parsed"
    workspace.mkdir()
    loader = load_generated(output / result["parser"])(source_data_dir=str(workspace))
    loader.get_data()
    load_metadata = loader.load(str(workspace / "nodes.jsonl"), str(workspace / "edges.jsonl"))
    nodes = [json.loads(line) for line in (workspace / "nodes.jsonl").read_text().splitlines()]
    edges = [json.loads(line) for line in (workspace / "edges.jsonl").read_text().splitlines()]
    report = TransformReport.model_validate_json((workspace / "transform_report.json").read_text())
    return nodes, edges, report, load_metadata, workspace


def test_compiles_explicit_semantics_without_layout_id_guessing(bundle):
    config = generator.compile_bundle(bundle[0])
    rs = config["record_sets"][0]
    assert rs["layout"] == "association"
    assert [e["role"] for e in rs["entities"]] == ["biolink:subject", "biolink:object"]
    assert config["infores"] == "infores:synthetic"
    assert not config["requires_infores"]


def test_sources_with_same_plan_kb_get_distinct_packages(bundle, tmp_path):
    other = tmp_path / "other_source"
    other.mkdir()
    path = other / "croissant.semantic.json"
    path.write_text(json.dumps(bundle[1]))
    (other / "semantics_plan.json").write_text(json.dumps({"kb": "shared-kb"}))
    assert (
        generator.compile_bundle(path)["source_id"]
        != generator.compile_bundle(bundle[0])["source_id"]
    )


def test_missing_source_identity_is_explicit(bundle):
    bundle[1]["recordSet"][0]["field"] = bundle[1]["recordSet"][0]["field"][:-2]
    config = generator.compile_bundle(save(bundle))
    assert config["requires_infores"]
    assert (
        generator.compile_bundle(bundle[0], infores="infores:confirmed")["infores"]
        == "infores:confirmed"
    )
    with pytest.raises(ValueError, match="infores"):
        generator.compile_bundle(bundle[0], infores="synthetic")


def test_source_identity_comes_from_the_metadatas_rcv_infores(bundle, tmp_path):
    # the metadata's own id wins over the old single-constant inference
    bundle[1]["rcv:infores"] = "infores:metadata-named"
    config = generator.compile_bundle(save(bundle))
    assert config["infores"] == "infores:metadata-named" and not config["infores_proposed"]
    # an explicit --infores still wins
    assert generator.compile_bundle(bundle[0], infores="infores:confirmed")["infores"] == (
        "infores:confirmed"
    )
    # a proposed id (not yet in the infores catalog) is used, marked, and doesn't block the run
    bundle[1]["recordSet"][0]["field"] = bundle[1]["recordSet"][0]["field"][:-2]
    bundle[1].update({"rcv:infores": "infores:proposed-kb", "rcv:inforesProposed": True})
    result = generator.generate(save(bundle), tmp_path / "out")
    assert result["infores"] == "infores:proposed-kb" and result["infores_proposed"]
    assert result["status"] != "needs_infores"
    bundle[1]["rcv:infores"] = "proposed-kb"
    with pytest.raises(ValueError, match="rcv:infores"):
        generator.compile_bundle(save(bundle))


@pytest.mark.parametrize("reason", ["missing_extract", "unknown_reader", "join", "missing_id"])
def test_unsupported_inputs_are_reported_without_inventing_mappings(bundle, reason):
    path, doc = bundle
    rs = doc["recordSet"][0]
    if reason == "missing_extract":
        for entity in rs["field"][:2]:
            entity["subField"][0]["source"].pop("extract")
    elif reason == "unknown_reader":
        doc["distribution"][0]["encodingFormat"] = "application/xml"
        doc["distribution"][0].pop("csvw:dialect")
    elif reason == "join":
        rs["field"][1]["subField"][0]["source"]["fileObject"]["@id"] = "other-file"
    else:
        rs["field"][0]["subField"][0].pop("equivalentProperty")
        rs["field"][1]["subField"][0].pop("equivalentProperty")
    config = generator.compile_bundle(save(bundle))
    assert config["record_sets"] == []
    assert config["skipped"]


def test_generated_package_is_portable_and_versioned(bundle, tmp_path):
    out = tmp_path / "generated"
    assert generator.main([str(bundle[0].parent), str(out)]) == 0
    registry = json.loads((out / "registry.json").read_text())
    entry = next(iter(registry.values()))
    text = (out / entry["parser"]).read_text()
    assert "SemanticSourceLoader" in text
    assert "robokop-orion==2.0.13" in text
    support = next(out.glob("_kgbcia_support_*"))
    assert (support / "schema.py").is_file()
    assert (support / "orion_runtime.py").is_file()
    assert (out / "run_orion.py").is_file()


def test_json_root_alignment_is_preserved_once(bundle):
    path, doc = bundle
    rs = doc["recordSet"][0]
    for entity in rs["field"][:2]:
        column = entity["subField"][0]
        column["source"]["extract"] = {"jsonPath": "$.records[*]." + column["name"]}
    rs["field"][-1]["source"]["extract"] = {"jsonPath": "$.records[*].publications"}
    doc["distribution"][0]["encodingFormat"] = "application/json"
    config = generator.compile_bundle(save(bundle))
    record_set = config["record_sets"][0]
    assert record_set["root"] == ["records", "*"]
    assert record_set["entities"][0]["fields"][0]["tokens"] == ["gene_id"]


def test_gzip_metadata_is_supported(bundle, tmp_path):
    path = bundle[0].with_suffix(".json.gz")
    with gzip.open(path, "wt") as stream:
        json.dump(bundle[1], stream)
    assert len(generator.compile_bundle(path)["record_sets"]) == 1


def test_native_orion_writes_flat_edges_and_report_lineage(
    bundle, tmp_path, monkeypatch, native_orion
):
    nodes, edges, report, counts, _ = run_loader(
        bundle,
        tmp_path,
        monkeypatch,
        native_orion,
        "gene_id\tdisease_id\tpublications\n1017\t0001\tPMID:123\n",
    )
    assert counts["source_nodes"] == 2
    assert counts["source_edges"] == 1
    assert {Node.model_validate(n).id for n in nodes} == {"NCBIGene:1017", "MONDO:0001"}
    edge = Edge.model_validate(edges[0])
    assert edge.knowledge_level == "not_provided"
    assert edge.primary_knowledge_source == "infores:synthetic"
    assert edge.publications == ["PMID:123"]
    assert "properties" not in edges[0] and "provenance" not in edges[0]
    pointer = edges[0]["kgbcia_source_records"][0]
    mapping_id, key = pointer.split("#", 1)
    mapping = next(m for m in report.mappings if m.id == mapping_id)
    assert mapping.produces == "edge" and mapping.file == "file"
    assert mapping.columns["subject"][0].field == "rs/gene_id"
    assert key == "2"


def test_native_missing_endpoint_skips_record_and_continues(
    bundle, tmp_path, monkeypatch, native_orion
):
    nodes, edges, report, counts, _ = run_loader(
        bundle,
        tmp_path,
        monkeypatch,
        native_orion,
        "gene_id\tdisease_id\tpublications\n\t0001\t\n1017\t0002\t\n",
    )
    assert len(edges) == 1
    assert counts["diagnostic_count"] >= 1
    assert any("missing endpoint" in item.reason for item in report.skipped)
    assert len(nodes) == 3


def test_association_without_a_predicate_is_reported_at_generation(bundle):
    rs = bundle[1]["recordSet"][0]
    rs["field"] = [f for f in rs["field"] if f["name"] != "predicate"]
    config = generator.compile_bundle(save(bundle))
    assert config["record_sets"][0]["predicate_mapped"] is False
    assert any(s["ref"] == "rs" and "no predicate" in s["reason"] for s in config["skipped"])


def test_native_association_without_a_predicate_emits_nodes_only(
    bundle, tmp_path, monkeypatch, native_orion
):
    rs = bundle[1]["recordSet"][0]
    rs["field"] = [f for f in rs["field"] if f["name"] != "predicate"]
    nodes, edges, _, counts, _ = run_loader(
        bundle,
        tmp_path,
        monkeypatch,
        native_orion,
        "gene_id\tdisease_id\tpublications\n1017\t0001\t\n1018\t0002\t\n",
    )
    assert len(nodes) == 4 and edges == []
    # not one "predicate: Input should be a valid string" diagnostic per row
    assert counts["diagnostic_count"] == 0


def test_native_rows_reporting_absence_build_nothing(bundle, tmp_path, monkeypatch, native_orion):
    level = field("level", "unused")
    level["equivalentProperty"] = "rcv:synthetic/level"
    level["annotation"] = [
        {"name": "absent_value_0", "value": "0"},
        {"name": "absent_value_1", "value": ""},
        {"name": "absent_value_2", "value": "Not detected"},
    ]
    bundle[1]["recordSet"][0]["field"].append(level)
    nodes, edges, report, counts, _ = run_loader(
        bundle,
        tmp_path,
        monkeypatch,
        native_orion,
        "gene_id\tdisease_id\tpublications\tlevel\n"
        "1017\t0001\t\t2.5\n"
        "1018\t0002\t\t0.0\n"
        "1019\t0003\t\tNot detected\n"
        "1020\t0004\t\t\n",
    )
    assert [e["subject"] for e in edges] == ["NCBIGene:1017"]
    assert {n["id"] for n in nodes} == {"NCBIGene:1017", "MONDO:0001"}
    assert counts["diagnostic_count"] == 3
    assert any("level reports absence" in s.reason for s in report.skipped)


def test_native_absence_threshold_and_group(bundle, tmp_path, monkeypatch, native_orion):
    level = field("level", "unused")
    level["equivalentProperty"] = "rcv:synthetic/level"
    level["annotation"] = [
        {"name": "absent_value_0", "value": ""},
        {"name": "absent_below", "value": "1"},
    ]
    good = field("good", "unused")
    bad = field("bad", "unused")
    for column in (good, bad):
        column["equivalentProperty"] = "rcv:synthetic/" + column["name"]
        column["annotation"] = [
            {"name": "absent_value_0", "value": ""},
            {"name": "absent_group", "value": "prognosis"},
        ]
    bundle[1]["recordSet"][0]["field"] += [level, good, bad]
    _, edges, _, counts, _ = run_loader(
        bundle,
        tmp_path,
        monkeypatch,
        native_orion,
        "gene_id\tdisease_id\tpublications\tlevel\tgood\tbad\n"
        "1017\t0001\t\t2.5\tyes\t\n"  # one of the group is enough to keep it
        "1018\t0002\t\t0.9\tyes\t\n"  # under the cutoff
        "1019\t0003\t\t\tyes\t\n"  # empty
        "1020\t0004\t\t3\t\t\n",  # the whole group is empty
    )
    assert [e["subject"] for e in edges] == ["NCBIGene:1017"]
    assert counts["diagnostic_count"] == 3


def test_native_each_filled_entity_makes_an_edge(bundle, tmp_path, monkeypatch, native_orion):
    second = group(
        "second", "biolink:object", "biolink:Disease", [field("other_id", "id", prefix="MONDO")]
    )
    bundle[1]["recordSet"][0]["field"].append(second)
    _, edges, _, _, _ = run_loader(
        bundle,
        tmp_path,
        monkeypatch,
        native_orion,
        "gene_id\tdisease_id\tother_id\tpublications\n"
        "1017\t0001\t0002\t\n"  # both columns filled
        "1018\t\t0003\t\n"  # the alternative only
        "1019\t\t\t\n",  # neither
    )
    assert sorted((e["subject"], e["object"]) for e in edges) == [
        ("NCBIGene:1017", "MONDO:0001"),
        ("NCBIGene:1017", "MONDO:0002"),
        ("NCBIGene:1018", "MONDO:0003"),
    ]


def test_one_unknown_list_item_keeps_the_rest_of_the_cell(native_orion):
    spec = importlib.util.spec_from_file_location("orion_runtime", SCRIPTS / "orion_runtime.py")
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    field_ = {"slot": "id", "extract": {"column": "c"}, "list_separator": ";"}
    field_["codes"] = {"a": "GO:1", "b": "GO:2"}
    skipped = []
    assert runtime.values(field_, {"c": "a;zzz;b"}, {}, skipped) == ["GO:1", "GO:2"]
    assert skipped == ["Unmapped code 'zzz'"]
    with pytest.raises(ValueError):  # a value on its own has nothing to keep
        runtime.values(field_, {"c": "zzz"}, {}, [])


def test_native_diagnostics_keep_examples_then_a_count(bundle, tmp_path, monkeypatch, native_orion):
    rows = "".join(f"\t{n:04}\t\n" for n in range(250))
    _, _, _, counts, workspace = run_loader(
        bundle, tmp_path, monkeypatch, native_orion, "gene_id\tdisease_id\tpublications\n" + rows
    )
    assert counts["diagnostic_count"] == 500  # no gene id, so no edge, on every row
    lines = [json.loads(line) for p in workspace.rglob("diagnostics.jsonl") for line in p.open()]
    examples = [line for line in lines if "count" not in line]
    totals = {line["reason"]: line["count"] for line in lines if "count" in line}
    assert len(examples) == 200
    assert totals == {
        "Entity gene has no identifier": 250,
        "Relationship has a missing endpoint": 250,
    }


def test_native_stale_columns_are_dropped_and_reported(bundle, tmp_path, monkeypatch, native_orion):
    nodes, edges, report, counts, _ = run_loader(
        bundle, tmp_path, monkeypatch, native_orion, "gene_id\tdisease_id\n1017\t0001\n"
    )
    assert len(edges) == 1
    assert edges[0].get("publications", []) == []
    assert any(s.ref == "rs/publications" and "absent" in s.reason for s in report.skipped)


def test_native_missing_file_fails_without_synthetic_output(
    bundle, tmp_path, monkeypatch, native_orion
):
    out = tmp_path / "generated"
    result = generator.generate(bundle[0], out)
    bindings = tmp_path / "files.json"
    bindings.write_text(json.dumps({"file": str(tmp_path / "does-not-exist.tsv")}))
    monkeypatch.setenv("KGB_CIA_SOURCE_FILES", str(bindings))
    workspace = tmp_path / "parsed"
    workspace.mkdir()
    loader = load_generated(out / result["parser"])(source_data_dir=str(workspace))
    with pytest.raises(FileNotFoundError):
        loader.get_data()
    assert not (workspace / "nodes.jsonl").exists()


def test_native_primary_key_is_used_in_lineage(bundle, tmp_path, monkeypatch, native_orion):
    bundle[1]["recordSet"][0]["key"] = {"@id": "rs/gene_id"}
    _, edges, _, _, _ = run_loader(
        bundle,
        tmp_path,
        monkeypatch,
        native_orion,
        "gene_id\tdisease_id\tpublications\n1017\t0001\t\n",
    )
    assert edges[0]["kgbcia_source_records"][0].endswith('#["1017"]')


def test_native_json_records_remain_aligned(bundle, tmp_path, monkeypatch, native_orion):
    rs = bundle[1]["recordSet"][0]
    for entity in rs["field"][:2]:
        leaf = entity["subField"][0]
        leaf["source"]["extract"] = {"jsonPath": "$.records[*]." + leaf["name"]}
    rs["field"][-1]["source"]["extract"] = {"jsonPath": "$.records[*].publications"}
    bundle[1]["distribution"][0]["encodingFormat"] = "application/json"
    _, edges, _, _, _ = run_loader(
        bundle,
        tmp_path,
        monkeypatch,
        native_orion,
        json.dumps(
            {
                "records": [
                    {"gene_id": "1", "disease_id": "0001", "publications": ["PMID:1"]},
                    {"gene_id": "2", "disease_id": "0002", "publications": ["PMID:2"]},
                ]
            }
        ),
    )
    assert [(e["subject"], e["object"]) for e in edges] == [
        ("NCBIGene:1", "MONDO:0001"),
        ("NCBIGene:2", "MONDO:0002"),
    ]
    assert edges[1]["publications"] == ["PMID:2"]


def test_native_entity_rows_and_role_edges_keep_entry_publications_separate(
    bundle,
    tmp_path,
    monkeypatch,
    native_orion,
):
    rs = bundle[1]["recordSet"][0]
    gene, disease, predicate, primary, pubs = rs["field"]
    rs["dataType"] = ["biolink:Gene"]
    disease["equivalentProperty"] = "biolink:related_to"
    rs["field"] = gene["subField"] + [disease, primary, pubs]
    nodes, edges, _, _, _ = run_loader(
        bundle,
        tmp_path,
        monkeypatch,
        native_orion,
        "gene_id\tdisease_id\tpublications\n1\t0001\tPMID:1\n",
    )
    assert len(edges) == 1
    assert edges[0]["predicate"] == "biolink:related_to"
    assert not edges[0].get("publications")
    assert next(n for n in nodes if n["id"] == "NCBIGene:1")["publications"] == ["PMID:1"]


def test_native_headerless_source_uses_declared_order(bundle, tmp_path, monkeypatch, native_orion):
    dist = bundle[1]["distribution"][0]
    dist["rcv:columnOrder"] = "field_order"
    dist["rcv:columnNames"] = ["gene_id", "disease_id", "publications"]
    dist["csvw:dialect"]["csvw:header"] = False
    _, edges, _, _, _ = run_loader(bundle, tmp_path, monkeypatch, native_orion, "1\t0001\tPMID:1\n")
    assert len(edges) == 1
    assert edges[0]["kgbcia_source_records"][0].endswith("#1")


@pytest.mark.parametrize("declared", ["#gene_id", "gene_id"])
def test_native_header_in_comments_keeps_a_declared_comment_mark(
    bundle, tmp_path, monkeypatch, native_orion, declared
):
    """A header row behind the comment mark ("#GeneID" in ClinVar) matches the column the
    croissant declares, whether it names the column with the mark or without it."""
    dist = bundle[1]["distribution"][0]
    dist["csvw:dialect"]["csvw:commentPrefix"] = "#"
    dist["rcv:headerInComments"] = True
    gene_id = bundle[1]["recordSet"][0]["field"][0]["subField"][0]
    gene_id["source"]["extract"]["column"] = declared
    _, edges, _, _, _ = run_loader(
        bundle,
        tmp_path,
        monkeypatch,
        native_orion,
        "# a comment line\n#gene_id\tdisease_id\tpublications\n1\t0001\tPMID:1\n",
    )
    assert [(e["subject"], e["object"]) for e in edges] == [("NCBIGene:1", "MONDO:0001")]


def test_native_prefix_code_table_is_executed(bundle, tmp_path, monkeypatch, native_orion):
    doc = bundle[1]
    doc["@context"]["NCBIGene"] = "https://example.test/gene/"
    leaf = doc["recordSet"][0]["field"][0]["subField"][0]
    leaf["annotation"] = [
        {
            "@id": "rs/gene_id/prefix",
            "name": "id_prefix",
            "source": {"fileObject": {"@id": "file"}, "extract": {"column": "database"}},
            "references": {"field": {"@id": "codes/name"}},
        }
    ]
    doc["recordSet"].append(
        {
            "@id": "codes",
            "dataType": "sc:Enumeration",
            "field": [{"@id": "codes/name"}, {"@id": "codes/url"}],
            "data": [{"codes/name": "gene", "codes/url": "https://example.test/gene/"}],
        }
    )
    nodes, edges, _, _, _ = run_loader(
        bundle,
        tmp_path,
        monkeypatch,
        native_orion,
        "gene_id\tdisease_id\tpublications\tdatabase\n1\t0001\t\tgene\n",
    )
    assert edges[0]["subject"] == "NCBIGene:1"


def test_native_fileset_lineage_includes_member_name(bundle, tmp_path, monkeypatch, native_orion):
    dist = bundle[1]["distribution"][0]
    dist["@type"] = "cr:FileSet"
    out = tmp_path / "generated"
    result = generator.generate(save(bundle), out)
    paths = [tmp_path / name for name in ("first.tsv", "second.tsv")]
    for path in paths:
        path.write_text("gene_id\tdisease_id\tpublications\n1\t0001\t\n")
    bindings = tmp_path / "files.json"
    bindings.write_text(json.dumps({"file": [str(p) for p in paths]}))
    monkeypatch.setenv("KGB_CIA_SOURCE_FILES", str(bindings))
    workspace = tmp_path / "parsed"
    workspace.mkdir()
    loader = load_generated(out / result["parser"])(source_data_dir=str(workspace))
    loader.get_data()
    loader.load(str(workspace / "nodes.jsonl"), str(workspace / "edges.jsonl"))
    edges = [json.loads(line) for line in (workspace / "edges.jsonl").read_text().splitlines()]
    assert len(edges) == 2
    assert {e["kgbcia_source_records"][0].split("#")[1] for e in edges} == {
        "first.tsv:2",
        "second.tsv:2",
    }


@pytest.mark.parametrize("succeeded", [True, False])
def test_runner_uses_native_pipeline_and_safe_normalization_defaults(
    bundle,
    tmp_path,
    monkeypatch,
    native_orion,
    succeeded,
):
    from orion import ingest_pipeline, normalization

    monkeypatch.setattr(normalization, "get_current_node_norm_version", lambda: "test-api-version")
    monkeypatch.setattr(normalization, "get_current_babel_version", lambda: "test-babel-version")

    out = tmp_path / "generated"
    assert generator.main([str(bundle[0]), str(out)]) == 0
    spec = importlib.util.spec_from_file_location("test_orion_runner", SCRIPTS / "run_orion.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    called = []

    def run(self, source_id, *, normalization_scheme):
        called.append(source_id)
        assert normalization_scheme.strict is False
        assert normalization_scheme.conflation is False
        assert normalization_scheme.edge_normalization_version == "v4.4.6"
        assert (tmp_path / "storage").is_dir()
        metadata = self.load_parser_metadata(source_id)
        assert metadata["identifier"] == "infores:synthetic"
        return "native-build-version" if succeeded else False

    monkeypatch.setattr(ingest_pipeline.IngestPipeline, "run_pipeline", run)
    monkeypatch.setattr(
        ingest_pipeline.IngestPipeline,
        "load_parser_metadata",
        ingest_pipeline.IngestPipeline.load_parser_metadata,
    )
    code = runner.main([str(out), "Kgbcia_synthetic_source", str(tmp_path / "storage")])
    assert code == (0 if succeeded else 1)
    assert called == ["Kgbcia_synthetic_source"]


@pytest.mark.parametrize("media", ["application/json", "application/x-ndjson"])
def test_independent_json_arrays_are_rejected_for_both_formats(bundle, media):
    doc = bundle[1]
    rs = doc["recordSet"][0]
    rs["field"] = rs["field"][:-1]
    for entity, path in zip(rs["field"][:2], ["$.genes[*].id", "$.diseases[*].id"], strict=True):
        entity["subField"][0]["source"]["extract"] = {"jsonPath": path}
    doc["distribution"][0]["encodingFormat"] = media
    config = generator.compile_bundle(save(bundle))
    assert not config["record_sets"]
    assert any("alignment is ambiguous" in s["reason"] for s in config["skipped"])


@pytest.mark.parametrize("component", ["orion_runtime.py", "schema.py"])
def test_incremental_generation_preserves_previous_support_snapshot(
    bundle, tmp_path, monkeypatch, component
):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("orion_runtime.py", "run_orion.py"):
        shutil.copyfile(SCRIPTS / name, scripts / name)
    repo = tmp_path / "repo"
    (repo / "shared").mkdir(parents=True)
    shutil.copyfile(generator.REPO_ROOT / "shared/schema.py", repo / "shared/schema.py")
    monkeypatch.setattr(generator, "SCRIPT_DIR", scripts)
    monkeypatch.setattr(generator, "REPO_ROOT", repo)
    output = tmp_path / "generated"
    first = generator.generate(bundle[0], output)
    first_parser = output / first["parser"]
    first_code = first_parser.read_bytes()
    first_config = json.loads(first_parser.with_name("semantic_mapping.json").read_text())
    old_support = next(output.glob("_kgbcia_support_*"))
    old_bytes = (old_support / component).read_bytes()
    changed = scripts / component if component == "orion_runtime.py" else repo / "shared/schema.py"
    changed.write_bytes(changed.read_bytes() + b"\nSNAPSHOT_MARKER = 'new'\n")
    other = tmp_path / "another_source"
    other.mkdir()
    other_path = other / "croissant.semantic.json"
    other_path.write_text(json.dumps(bundle[1]))
    generator.generate(other_path, output)
    assert first_parser.read_bytes() == first_code
    assert (old_support / component).read_bytes() == old_bytes
    assert len(list(output.glob("_kgbcia_support_*"))) == 2
    hash_key = "runtime_sha256" if component == "orion_runtime.py" else "schema_sha256"
    assert hashlib.sha256(old_bytes).hexdigest() == first_config[hash_key]
    regenerated = generator.generate(bundle[0], output)
    assert (output / regenerated["parser"]).read_bytes() != first_code


@pytest.mark.parametrize("nulls", ["-", ["-", "NA"]])
def test_native_csvw_nulls_do_not_become_nodes_or_publications(
    bundle, tmp_path, monkeypatch, native_orion, nulls
):
    bundle[1]["distribution"][0]["csvw:dialect"]["csvw:null"] = nulls
    nodes, edges, report, counts, _ = run_loader(
        bundle,
        tmp_path,
        monkeypatch,
        native_orion,
        "gene_id\tdisease_id\tpublications\n-\t0001\t-\n1\t0002\t-\n",
    )
    assert all(n["id"] != "NCBIGene:-" for n in nodes)
    assert len(edges) == 1 and edges[0]["subject"] == "NCBIGene:1"
    assert not edges[0].get("publications")
    assert counts["diagnostic_count"] > 0


@pytest.mark.parametrize("kind", ["zip", "tar"])
@pytest.mark.parametrize("relative_url", [True, False])
def test_native_archive_members_use_declared_parent(
    bundle, tmp_path, monkeypatch, native_orion, kind, relative_url
):
    content = b"gene_id\tdisease_id\tpublications\n1\t0001\tPMID:1\n"
    archive = tmp_path / ("source." + kind)
    if kind == "zip":
        with zipfile.ZipFile(archive, "w") as packed:
            packed.writestr("records.tsv", content)
    else:
        with tarfile.open(archive, "w") as packed:
            member = tarfile.TarInfo("records.tsv")
            member.size = len(content)
            packed.addfile(member, io.BytesIO(content))
    doc = bundle[1]
    child = doc["distribution"][0]
    child["containedIn"] = {"@id": "archive"}
    child["sha256"] = hashlib.sha256(content).hexdigest()
    if relative_url:
        child["contentUrl"] = "records.tsv"
    else:
        child.pop("contentUrl")
    doc["distribution"].append(
        {
            "@id": "archive",
            "@type": "cr:FileObject",
            "contentUrl": "https://example.test/source",
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        }
    )
    output = tmp_path / "generated"
    result = generator.generate(save(bundle), output)
    workspace = tmp_path / "parsed"
    workspace.mkdir()
    downloads = []

    def download(request):
        # servers such as JensenLab and Reactome refuse Python's default User-Agent
        downloads.append((request.full_url, request.get_header("User-agent")))
        return archive.open("rb")

    monkeypatch.setattr("urllib.request.urlopen", download)
    loader = load_generated(output / result["parser"])(source_data_dir=str(workspace))
    loader.get_data()
    loader.load(str(workspace / "nodes.jsonl"), str(workspace / "edges.jsonl"))
    assert downloads == [
        ("https://example.test/source", "KGB-CIA/0.1 (+https://github.com/UNC-RENCI-KGAI/KGB-CIA)")
    ]
    assert len((workspace / "edges.jsonl").read_text().splitlines()) == 1


def test_native_repeated_nodes_merge_properties_before_normalization(
    bundle, tmp_path, monkeypatch, native_orion
):
    gene = bundle[1]["recordSet"][0]["field"][0]
    gene["subField"].append(field("xref", "xref"))
    nodes, edges, report, counts, workspace = run_loader(
        bundle,
        tmp_path,
        monkeypatch,
        native_orion,
        "gene_id\tdisease_id\tpublications\txref\n1\t0001\t\tTEST:A\n1\t0002\t\tTEST:B\n",
    )
    assert len(nodes) == counts["source_nodes"] == 3
    assert counts["repeat_nodes"] == 1
    assert next(n for n in nodes if n["id"] == "NCBIGene:1")["xref"] == ["TEST:A", "TEST:B"]
    assert len(edges) == 2
    assert not list(workspace.glob("node-merge-*"))


def test_native_canonical_collisions_keep_properties_and_lineage(
    bundle, tmp_path, monkeypatch, native_orion
):
    from orion import kgx_file_normalizer, loader_interface
    from orion.normalization import NodeNormalizer, NormalizationScheme

    gene = bundle[1]["recordSet"][0]["field"][0]
    gene["subField"].append(field("xref", "xref"))
    _, source_edges, _, _, workspace = run_loader(
        bundle,
        tmp_path,
        monkeypatch,
        native_orion,
        "gene_id\tdisease_id\tpublications\txref\n1\t0001\t\tTEST:A\n2\t0001\t\tTEST:B\n",
    )
    original_writer = kgx_file_normalizer.KGXFileWriter
    assert loader_interface.KGXFileWriter is original_writer
    # Load the generated class to use the same scoped native writer integration as the runner.
    parser_path = next((tmp_path / "generated/parsers").glob("*/src/parser.py"))
    generated = load_generated(parser_path)
    runtime = sys.modules[generated.__module__].merging_node_writers

    def normalize(self, curies):
        return {
            curie: {
                "id": {"identifier": "NCBIGene:1" if curie.startswith("NCBIGene:") else curie},
                "type": ["biolink:Gene" if curie.startswith("NCBIGene:") else "biolink:Disease"],
                "equivalent_identifiers": [{"identifier": curie}],
            }
            for curie in curies
        }

    monkeypatch.setattr(NodeNormalizer, "hit_node_norm_service", normalize)
    normalizer = kgx_file_normalizer.KGXFileNormalizer(
        source_nodes_file_path=str(workspace / "nodes.jsonl"),
        nodes_output_file_path=str(workspace / "normalized_nodes.jsonl"),
        node_norm_map_file_path=str(workspace / "node_map.json"),
        node_norm_failures_file_path=str(workspace / "failures.log"),
        source_edges_file_path=str(workspace / "edges.jsonl"),
        edges_output_file_path=str(workspace / "normalized_edges.jsonl"),
        normalization_scheme=NormalizationScheme(
            node_normalization_version="test",
            babel_version="test",
            edge_normalization_version="v4.4.6",
            strict=True,
        ),
        predicates_pre_normalized=True,
        preserve_unconnected_nodes=True,
    )
    with runtime():
        counts = normalizer.normalize_kgx_files()
    nodes = [
        json.loads(line) for line in (workspace / "normalized_nodes.jsonl").read_text().splitlines()
    ]
    edges = [
        json.loads(line) for line in (workspace / "normalized_edges.jsonl").read_text().splitlines()
    ]
    assert len(nodes) == counts["final_normalized_nodes"] == 2
    assert next(n for n in nodes if n["id"] == "NCBIGene:1")["xref"] == ["TEST:A", "TEST:B"]
    assert {p for e in edges for p in e["kgbcia_source_records"]} == {
        p for e in source_edges for p in e["kgbcia_source_records"]
    }
    assert kgx_file_normalizer.KGXFileWriter is original_writer
    assert not list(workspace.glob("node-merge-*"))


def test_runner_rejects_missing_biolink_version(bundle, tmp_path, native_orion):
    (bundle[0].parent / "semantics_plan.json").unlink()
    output = tmp_path / "generated"
    generator.main([str(bundle[0]), str(output)])
    spec = importlib.util.spec_from_file_location(
        "missing_version_runner", SCRIPTS / "run_orion.py"
    )
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    with pytest.raises(SystemExit, match="2"):
        runner.main([str(output), "Kgbcia_synthetic_source", str(tmp_path / "storage")])


def test_runner_reports_why_a_download_failed_and_retries_it(
    bundle, tmp_path, monkeypatch, native_orion, capsys
):
    import urllib.error

    from orion import ingest_pipeline, normalization

    monkeypatch.setattr(normalization, "get_current_node_norm_version", lambda: "test-api-version")
    monkeypatch.setattr(normalization, "get_current_babel_version", lambda: "test-babel-version")
    monkeypatch.setattr(ingest_pipeline.time, "sleep", lambda seconds: None)
    output = tmp_path / "generated"
    generator.main([str(bundle[0]), str(output)])
    spec = importlib.util.spec_from_file_location("fetch_failure_runner", SCRIPTS / "run_orion.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    attempts = []

    def refusing(code):
        def refuse(url, *args, **kwargs):
            attempts.append(code)
            raise urllib.error.HTTPError(str(url), code, "Refused", {}, None)

        return refuse

    argv = [str(output), "Kgbcia_synthetic_source", str(tmp_path / "storage")]
    for code in (403, 404):
        for call in ("urlretrieve", "urlopen"):
            monkeypatch.setattr(f"urllib.request.{call}", refusing(code))
        assert runner.main(argv) == 1
        assert f"fetch: <HTTPError {code}" in capsys.readouterr().err
    # the second run downloaded again rather than stopping at "previously failed"
    assert 404 in attempts


def test_native_jsonl_skips_bad_line_and_keeps_physical_record_numbers(
    bundle,
    tmp_path,
    monkeypatch,
    native_orion,
):
    bundle[1]["distribution"][0]["encodingFormat"] = "application/x-ndjson"
    text = (
        json.dumps({"gene_id": "1", "disease_id": "0001", "publications": ""})
        + "\nnot json\n"
        + json.dumps({"gene_id": "2", "disease_id": "0002", "publications": ""})
        + "\n"
    )
    _, edges, report, _, _ = run_loader(bundle, tmp_path, monkeypatch, native_orion, text)
    assert len(edges) == 2
    assert edges[1]["kgbcia_source_records"][0].endswith("#3")
    assert any("Malformed JSONL" in s.reason for s in report.skipped)


def test_native_unmapped_primary_source_is_not_replaced_with_aggregator(
    bundle,
    tmp_path,
    monkeypatch,
    native_orion,
):
    rs = bundle[1]["recordSet"][0]
    primary = rs["field"][3]
    primary.pop("value")
    primary["source"] = {"fileObject": {"@id": "file"}, "extract": {"column": "primary"}}
    rs["field"].append(
        field("aggregator", "aggregator_knowledge_source", value="infores:synthetic")
    )
    _, edges, report, _, _ = run_loader(
        bundle,
        tmp_path,
        monkeypatch,
        native_orion,
        "gene_id\tdisease_id\tpublications\tprimary\n1\t0001\t\t\n",
    )
    assert edges == []
    assert any("Mapped primary" in s.reason for s in report.skipped)


def load_merge_script():
    spec = importlib.util.spec_from_file_location("merge_graphs", SCRIPTS / "merge_graphs.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_graph(graphs, source, nodes, edges, *, status="stable"):
    run_dir = graphs / source / source / "data" / "1"
    normalized = run_dir / "parsed_p" / "normalized_n"
    normalized.mkdir(parents=True)
    meta = {
        "fetch_status": "stable",
        "parsings": {
            "p": {
                "parsing_status": "stable",
                "normalizations": {"n": {"normalization_status": status}},
            }
        },
    }
    (run_dir / f"{source}.meta.json").write_text(json.dumps(meta))
    for name, rows in (("nodes", nodes), ("edges", edges)):
        text = "".join(json.dumps(row) + "\n" for row in rows)
        (normalized / f"normalized_{name}.jsonl").write_text(text)


def test_stable_normalization_skips_unfinished_graphs(tmp_path):
    write_graph(tmp_path, "done", [], [])
    write_graph(tmp_path, "failed", [], [], status="failed")
    merge = load_merge_script()
    assert merge.stable_normalization(tmp_path / "done").name == "normalized_n"
    assert merge.stable_normalization(tmp_path / "failed") is None


def test_native_merge_follows_orion_rules(tmp_path, native_orion):
    def edge(pks, records, **extra):
        triple = {"subject": "A:1", "predicate": "biolink:related_to", "object": "B:1"}
        return {
            **triple,
            "primary_knowledge_source": pks,
            "kgbcia_source_records": records,
            "publications": records,
            **extra,
        }

    nodes = [
        {"id": "A:1", "category": ["biolink:Gene"]},
        {"id": "B:1", "category": ["biolink:Gene"]},
    ]
    graphs = tmp_path / "graphs"
    # "knowledge" holds "edge": ORION's own file sorting would read these nodes as edges
    write_graph(graphs, "knowledge_source", nodes, [edge("infores:a", ["one"])])
    write_graph(graphs, "other", nodes[:1], [edge("infores:a", ["two"]), edge("infores:b", ["x"])])
    write_graph(graphs, "unfinished", [{"id": "C:1"}], [], status="failed")
    output = tmp_path / "merged"
    assert load_merge_script().main([str(graphs), str(output)]) == 0

    merged_nodes = [json.loads(line) for line in (output / "nodes.jsonl").read_text().splitlines()]
    edges = [json.loads(line) for line in (output / "edges.jsonl").read_text().splitlines()]
    assert sorted(n["id"] for n in merged_nodes) == ["A:1", "B:1"]
    assert len(edges) == 2  # a second primary source stays a separate edge
    same = next(e for e in edges if e["primary_knowledge_source"] == "infores:a")
    assert sorted(same["kgbcia_source_records"]) == ["one", "two"]
    metadata = json.loads((output / "merge-metadata.json").read_text())
    assert metadata["sources_not_merged"] == ["unfinished"] and metadata["edges_diff"] == 1
