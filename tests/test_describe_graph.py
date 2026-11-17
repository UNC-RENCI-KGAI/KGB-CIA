import importlib.util
import json
from collections import Counter
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
    root = tmp_path / "transformation" / "graphs"
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
    assert dg.main([str(graphs.parent), "--user", "user", "--output", str(out)]) == 0
    page = (out / "overview.html").read_text()
    assert "How your data connects" in page and "<script>" in page
    assert "100% connected so far" in (out / "description.md").read_text()


def test_user_by_plain_name(graphs):
    d = dg.describe(graphs, user=["User"])
    assert d["overlap"]["user"]["sources"] == ["Kgbcia_user"]


def test_unknown_user_source_lists_choices(graphs):
    with pytest.raises(SystemExit, match="matches no built source. Choose from: big, small, user"):
        dg.describe(graphs, user=["nope"])


def test_ambiguous_user_source(graphs):
    with pytest.raises(SystemExit, match="matches several sources"):
        dg.describe(graphs, user=["s"])


def _merge_and_clean(graphs, cleaned_from, cleaned_counts=(7, 14)):
    merged = graphs.parent / "merged"
    merged.mkdir()
    (merged / "merge-metadata.json").write_text(
        json.dumps({"final_node_count": 7, "final_edge_count": 14})
    )
    cleaning = graphs.parent.parent / "cleaning"
    cleaning.mkdir()
    (cleaning / "cleaning_report.json").write_text(
        json.dumps(
            {
                "settings": {"input": str(cleaned_from or merged)},
                "nodes": {"in": cleaned_counts[0], "out": 6},
                "edges": {"in": cleaned_counts[1], "out": 12},
            }
        )
    )
    return merged, cleaning


def test_layout_found_from_transformation_dir(graphs):
    merged, cleaning = _merge_and_clean(graphs, None)
    for start in (graphs.parent, graphs):
        assert dg.find_layout(start) == (graphs, merged, cleaning)


def test_copied_graph_matched_by_counts(graphs, tmp_path):
    # cleaned on the cluster, downloaded here: the report's input path is the cluster's
    merged, cleaning = _merge_and_clean(graphs, "/projects/cluster/merged")
    assert dg.find_layout(graphs.parent) == (graphs, merged, cleaning)


def test_stale_cleaning_report_ignored(graphs, tmp_path):
    merged, _ = _merge_and_clean(graphs, tmp_path / "some_other_merge", cleaned_counts=(8, 20))
    found = dg.find_layout(graphs.parent)
    assert found == (graphs, merged, None)
    assert dg.describe(*found)["totals"]["basis"] == "merged graph"


def test_labels():
    assert dg.label("biolink:SequenceVariant") == "Sequence variant"
    assert dg.label("biolink:genetically_associated_with") == "genetically associated with"
    assert dg.short(12_841_209) == "12.8M"


def test_orion_error_repeat_dropped():
    meta = {
        "fetch_status": "failed",
        "fetch_error": "<HTTPError 404: 'Not Found'>-HTTP Error 404: Not Found",
    }
    assert dg._failure(meta) == "download failed: HTTP Error 404: Not Found"
    meta = {
        "fetch_status": "failed",
        "fetch_error": "ValueError('Checksum mismatch')-Checksum mismatch",
    }
    assert dg._failure(meta) == "download failed: Checksum mismatch"
    assert dg._once("plain error-with-dash") == "plain error-with-dash"


PROTEIN = [
    "biolink:BiologicalEntity",
    "biolink:GeneOrGeneProduct",
    "biolink:NamedThing",
    "biolink:Polypeptide",
    "biolink:Protein",
]
DRUG = [
    "biolink:ChemicalEntity",
    "biolink:Drug",
    "biolink:MolecularEntity",
    "biolink:NamedThing",
    "biolink:SmallMolecule",
]
CHEMICAL = [
    "biolink:ChemicalEntity",
    "biolink:MolecularEntity",
    "biolink:NamedThing",
    "biolink:SmallMolecule",
]


def _nodes(directory, categories):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "nodes.jsonl").write_text(
        "".join(
            json.dumps({"id": f"X:{i}", "category": c}) + "\n" for i, c in enumerate(categories)
        )
    )


def test_categories_counted_from_cleaned_graph(graphs):
    # the merge sorts each node's categories, so a protein lists BiologicalEntity first
    merged, cleaning = _merge_and_clean(graphs, None)
    _nodes(cleaning, [PROTEIN, PROTEIN, DRUG, CHEMICAL])
    d = dg.describe(*dg.find_layout(graphs.parent))
    assert d["categories_basis"] == "cleaned graph"
    assert d["categories"] == {
        "biolink:Protein": 2,
        "biolink:Drug + biolink:SmallMolecule": 1,
        "biolink:SmallMolecule": 1,
    }
    assert dg.label("biolink:Drug + biolink:SmallMolecule") == "Drug + Small molecule"
    assert "## Node categories (cleaned graph)" in dg.markdown(d)


def test_categories_fall_back_to_sources_without_a_graph(graphs):
    d = dg.describe(graphs)
    assert d["categories_basis"] == "sources, before merging"
    assert d["categories"] == d["node_types"]


def test_inferred_most_specific_without_biolink():
    # categories are told apart only where some node carries one without the other
    chemical_entity = ("biolink:ChemicalEntity", "biolink:NamedThing")
    molecular_entity = ("biolink:ChemicalEntity", "biolink:MolecularEntity", "biolink:NamedThing")
    profiles = Counter({tuple(PROTEIN): 2, tuple(CHEMICAL): 1, tuple(DRUG): 1})
    profiles.update([chemical_entity, molecular_entity])
    most_specific = dg._inferred_most_specific(profiles)
    assert most_specific(tuple(CHEMICAL)) == ("biolink:SmallMolecule",)
    assert most_specific(tuple(DRUG)) == ("biolink:Drug",)
    assert "biolink:NamedThing" not in most_specific(tuple(PROTEIN))


def test_sources_not_in_graph_get_a_plain_reason():
    assert dg.why_not("download failed: Checksum mismatch for file_x").startswith(
        "Its files changed"
    )
    assert (
        dg.why_not("download failed: HTTP Error 403: Forbidden")
        == "Its website refused the download"
    )
    assert dg.why_not("parsing failed: Extra data") == "Its files downloaded but couldn't be read"
    assert dg.why_not("something new") == "Its build didn't finish"


CATALOG = [
    {"name": "Big", "slug": "big", "status": "good", "croissant": "kb/big/croissant.json"},
    {"name": "Blocked", "slug": "blocked", "status": "good"},
    {"name": "Gene Ontology", "slug": "gene-ontology", "status": "check"},
    {"name": "CIViC", "slug": "civic", "status": "good"},
]


def test_additional_croissants_lists_only_sources_not_tried(graphs, tmp_path):
    """Built sources and ones that failed to build are already on the page; the rest are new."""
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps(CATALOG))
    d = dg.describe(graphs, croissants=str(catalog))
    assert [r["slug"] for r in d["additional_croissants"]] == ["civic", "gene-ontology"]
    assert d["additional_croissants"][0] == {
        "name": "CIViC",
        "slug": "civic",
        "page": dg.BOULANGERIE_SITE + "kb/civic/",
        "status": "good",
    }
    md, page = dg.markdown(d), dg.overview_html(d)
    for text in (md, page):
        assert "Additional sources with available croissants" in text
        assert text.index("Other available sources not in the graph") < text.index(
            "Additional sources with available croissants"
        )
    assert f'<a href="{dg.BOULANGERIE_SITE}kb/gene-ontology/">' in page


def test_no_croissants_section_without_the_flag(graphs):
    d = dg.describe(graphs)
    assert "additional_croissants" not in d
    assert "available croissants" not in dg.markdown(d) + dg.overview_html(d)


def test_croissant_pages_follow_the_catalog_or_the_flag():
    assert dg.pages_base("https://example.org/site/catalog.json", None) == (
        "https://example.org/site/"
    )
    assert dg.pages_base("saved/catalog.json", None) == dg.BOULANGERIE_SITE
    assert dg.pages_base("saved/catalog.json", "https://example.org/b") == "https://example.org/b/"
    extra = dg.additional_croissants(CATALOG[3:], [], "https://example.org/b/")
    assert extra[0]["page"] == "https://example.org/b/kb/civic/"


def test_main_with_a_saved_catalog(graphs, tmp_path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps(CATALOG))
    out = tmp_path / "out"
    assert dg.main([str(graphs), "--croissants", str(catalog), "--output", str(out)]) == 0
    written = json.loads((out / "description.json").read_text())
    assert written["croissant_catalog"]["pages"] == dg.BOULANGERIE_SITE
    assert len(written["additional_croissants"]) == 2


@pytest.mark.parametrize("content", ["not json", '{"name": "x"}', '[{"name": "no slug"}]'])
def test_unreadable_catalog_says_so(graphs, tmp_path, content):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(content)
    with pytest.raises(SystemExit, match="--croissants"):
        dg.describe(graphs, croissants=str(catalog))
    with pytest.raises(SystemExit, match="could not read"):
        dg.describe(graphs, croissants=str(tmp_path / "missing.json"))


def test_croissant_options_are_documented():
    """The skill-option rule (#48): an agent only learns an option from the skill's Markdown."""
    skill = (SCRIPT.parents[1] / "SKILL.md").read_text()
    assert "--croissants" in skill and "--croissant-pages" in skill
