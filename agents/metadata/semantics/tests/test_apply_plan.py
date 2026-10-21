"""Tests for apply_plan.py and the layout helpers. Run: uv run --with pytest pytest tests/"""

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from apply_plan import PlanError, apply_plan  # noqa: E402
from common import column_names, layout_id, layouts  # noqa: E402


def field(fid, name, dtype="sc:Text"):
    return {
        "@type": "cr:Field",
        "@id": fid,
        "name": name,
        "dataType": dtype,
        "source": {"fileObject": {"@id": "f1"}, "extract": {"column": name}},
    }


def croissant():
    return {
        "@context": {"sc": "https://schema.org/", "cr": "http://mlcommons.org/croissant/"},
        "@type": "sc:Dataset",
        "name": "test",
        "distribution": [
            {
                "@type": "cr:FileObject",
                "@id": "f1",
                "contentUrl": "a.tsv",
                "encodingFormat": "text/tsv",
            }
        ],
        "recordSet": [
            {
                "@type": "cr:RecordSet",
                "@id": "rs_human",
                "name": "genes_diseases_human.tsv",
                "field": [
                    field("a1", "GeneID", "sc:Integer"),
                    field("a2", "Symbol"),
                    field("a3", "DiseaseID"),
                    field("a4", "Evidence"),
                    field("a5", "PMIDs"),
                ],
            },
            {
                "@type": "cr:RecordSet",
                "@id": "rs_mouse",
                "name": "genes_diseases_mouse.tsv",
                "field": [
                    field("b1", "GeneID", "sc:Integer"),
                    field("b2", "Symbol"),
                    field("b3", "DiseaseID"),
                    field("b4", "Evidence"),
                    field("b5", "PMIDs"),
                ],
            },
            {
                "@type": "cr:RecordSet",
                "@id": "rs_other",
                "name": "other.tsv",
                "field": [field("c1", "X")],
            },
        ],
    }


LAYOUT = layout_id(["GeneID", "Symbol", "DiseaseID", "Evidence", "PMIDs"])


def plan(**overrides):
    p = {
        "kb": "t",
        "prefixes": {
            "NCBIGene": "http://identifiers.org/ncbigene/",
            "MONDO": "http://purl.obolibrary.org/obo/MONDO_",
            "PMID": "http://www.ncbi.nlm.nih.gov/pubmed/",
            "NCBITaxon": "http://purl.obolibrary.org/obo/NCBITaxon_",
        },
        "layouts": [
            {
                "id": "gd",
                "match": {"layout": LAYOUT},
                "row": {
                    "type": "biolink:GeneToDiseaseAssociation",
                    "predicate": "biolink:associated_with",
                },
                "entities": [
                    {
                        "name": "gene",
                        "type": "biolink:Gene",
                        "role": "biolink:subject",
                        "columns": {
                            "GeneID": {
                                "property": "biolink:id",
                                "id_prefix": "NCBIGene",
                                "id_form": "bare",
                            },
                            "Symbol": {"property": "biolink:symbol"},
                        },
                    },
                    {
                        "name": "disease",
                        "type": "biolink:Disease",
                        "role": "biolink:object",
                        "columns": {"DiseaseID": {"property": "biolink:id", "id_form": "curie"}},
                    },
                ],
                "columns": {
                    "Evidence": {"codes": {"IMP": "ECO:0000315"}},
                    "PMIDs": {
                        "property": "biolink:publications",
                        "id_prefix": "PMID",
                        "list_separator": "|",
                    },
                },
            }
        ],
    }
    p.update(overrides)
    return p


def rs_by_id(c, rid):
    return next(r for r in c["recordSet"] if r["@id"] == rid)


def test_layouts_group_record_sets_by_column_set():
    groups = layouts(croissant())
    assert list(groups)[0] == LAYOUT
    assert [r["@id"] for r in groups[LAYOUT]] == ["rs_human", "rs_mouse"]
    assert layout_id(["b", "a"]) == layout_id(["a", "b"])


def test_input_is_not_modified():
    c = croissant()
    before = copy.deepcopy(c)
    apply_plan(c, plan())
    assert c == before


def test_entities_group_columns_and_keep_their_ids_and_sources():
    out, counts = apply_plan(croissant(), plan())
    assert counts["record_sets"] == 2
    rs = rs_by_id(out, "rs_human")
    assert rs["dataType"] == ["biolink:GeneToDiseaseAssociation"]
    names = column_names(rs)
    assert names[:2] == ["gene", "disease"]
    gene = rs["field"][0]
    assert gene["dataType"] == ["biolink:Gene"] and gene["equivalentProperty"] == "biolink:subject"
    gene_id, symbol = gene["subField"]
    assert gene_id["@id"] == "a1" and gene_id["source"]["extract"]["column"] == "GeneID"
    assert gene_id["dataType"] == [
        "sc:Integer",
        "biolink:Gene",
    ]  # atomic type kept, entity type added
    assert gene_id["annotation"][0]["equivalentProperty"] == "sh:prefix"
    assert gene_id["annotation"][0]["value"] == "NCBIGene"
    assert symbol["equivalentProperty"] == "biolink:symbol"
    assert "annotation" not in rs["field"][1]["subField"][0]  # curie ids need no prefix annotation


def test_row_predicate_becomes_a_constant_field():
    out, _ = apply_plan(croissant(), plan())
    pred = next(f for f in rs_by_id(out, "rs_human")["field"] if f["name"] == "predicate")
    assert (
        pred["value"] == "biolink:associated_with"
        and pred["equivalentProperty"] == "biolink:predicate"
    )
    assert "source" not in pred


def test_codes_make_one_shared_enumeration_and_references():
    out, counts = apply_plan(croissant(), plan())
    assert counts["codes_tables"] == 1
    enum = next(r for r in out["recordSet"] if r["@id"].startswith("codes_"))
    eid = enum["@id"]
    assert enum["dataType"] == "sc:Enumeration"
    assert enum["data"] == [{f"{eid}/name": "IMP", f"{eid}/url": "ECO:0000315"}]
    ev = next(f for f in rs_by_id(out, "rs_mouse")["field"] if f["name"] == "Evidence")
    assert ev["references"] == {"field": {"@id": f"{eid}/name"}}  # the form mlcroissant accepts


def test_list_separator_and_prefix_annotations():
    out, _ = apply_plan(croissant(), plan())
    pmids = next(f for f in rs_by_id(out, "rs_human")["field"] if f["name"] == "PMIDs")
    props = {a["equivalentProperty"]: a["value"] for a in pmids["annotation"]}
    assert props == {"sh:prefix": "PMID", "rcv:listSeparator": "|"}


def test_context_gets_croissant_terms_and_prefixes():
    out, _ = apply_plan(croissant(), plan())
    ctx = out["@context"]
    assert ctx["annotation"] == "cr:annotation" and ctx["value"] == "sc:value"
    assert ctx["biolink"] == "https://w3id.org/biolink/vocab/" and ctx["NCBIGene"].startswith(
        "http"
    )


def test_conflicting_prefix_is_an_error():
    c = croissant()
    c["@context"]["biolink"] = "http://example.org/other/"
    with pytest.raises(PlanError, match="already in @context"):
        apply_plan(c, plan())


def test_more_specific_match_wins():
    p = plan()
    mouse = copy.deepcopy(p["layouts"][0])
    mouse["id"] = "gd_mouse"
    mouse["match"] = {"layout": LAYOUT, "record_set_name": "mouse"}
    mouse["entities"][0]["constants"] = [
        {"name": "taxon", "property": "biolink:in_taxon", "value": "NCBITaxon:10090"}
    ]
    p["layouts"].append(mouse)
    out, _ = apply_plan(croissant(), p)
    mouse_gene = rs_by_id(out, "rs_mouse")["field"][0]
    human_gene = rs_by_id(out, "rs_human")["field"][0]
    assert any(s.get("value") == "NCBITaxon:10090" for s in mouse_gene["subField"])
    assert not any("value" in s for s in human_gene["subField"])


def test_equally_specific_matches_are_an_error():
    p = plan()
    p["layouts"].append({**copy.deepcopy(p["layouts"][0]), "id": "gd2"})
    with pytest.raises(PlanError, match="equally"):
        apply_plan(croissant(), p)


def test_has_columns_matches_every_record_set_with_those_columns():
    p = plan(
        layouts=[
            {
                "id": "ids",
                "match": {"has_columns": ["GeneID"]},
                "columns": {"GeneID": {"type": "biolink:Gene", "id_prefix": "NCBIGene"}},
            }
        ]
    )
    _out, counts = apply_plan(croissant(), p)
    assert counts["record_sets"] == 2


def test_missing_column_and_unmatched_layout_are_errors():
    p = plan()
    p["layouts"][0]["columns"]["Nope"] = {"property": "biolink:name"}
    with pytest.raises(PlanError, match="no column"):
        apply_plan(croissant(), p)
    with pytest.raises(PlanError, match="matches no record set"):
        apply_plan(croissant(), plan(layouts=[{"id": "x", "match": {"layout": "Ldeadbeef"}}]))


def test_column_in_two_places_is_an_error():
    p = plan()
    p["layouts"][0]["columns"]["Symbol"] = {"property": "biolink:name"}
    with pytest.raises(PlanError, match="more than once"):
        apply_plan(croissant(), p)


def test_local_terms_rows_have_every_column():
    p = plan(
        local_terms=[
            {"id": "rcv:t/Evidence", "label": "Evidence", "broad_match": "biolink:has_evidence"}
        ]
    )
    out, _ = apply_plan(croissant(), p)
    terms = rs_by_id(out, "rc_local_terms")
    row = terms["data"][0]
    assert set(row) == {f["@id"] for f in terms["field"]}
    assert (
        row["rc_local_terms/broad_match"] == "biolink:has_evidence"
        and row["rc_local_terms/close_match"] == ""
    )


def test_column_library_specs_are_reused_with_overrides():
    p = plan(
        column_library={
            "pmids": {
                "property": "biolink:publications",
                "id_prefix": "PMID",
                "list_separator": "|",
            }
        }
    )
    p["layouts"][0]["columns"]["PMIDs"] = {"use": "pmids", "list_separator": ";"}
    out, _ = apply_plan(croissant(), p)
    pm = next(f for f in rs_by_id(out, "rs_human")["field"] if f["name"] == "PMIDs")
    assert {a["equivalentProperty"]: a["value"] for a in pm["annotation"]} == {
        "sh:prefix": "PMID",
        "rcv:listSeparator": ";",
    }
    p["layouts"][0]["columns"]["PMIDs"] = {"use": "nope"}
    with pytest.raises(PlanError, match="column_library"):
        apply_plan(croissant(), p)


def test_null_values_become_annotations():
    p = plan()
    p["layouts"][0]["columns"]["PMIDs"]["null_values"] = ["N/A", "-"]
    out, _ = apply_plan(croissant(), p)
    pm = next(f for f in rs_by_id(out, "rs_human")["field"] if f["name"] == "PMIDs")
    assert [a["value"] for a in pm["annotation"] if a["equivalentProperty"] == "rcv:nullValue"] == [
        "N/A",
        "-",
    ]


def test_atomic_override_replaces_the_basic_type():
    p = plan()
    p["layouts"][0]["entities"][0]["columns"]["GeneID"]["atomic"] = "sc:Text"
    out, _ = apply_plan(croissant(), p)
    gid = rs_by_id(out, "rs_human")["field"][0]["subField"][0]
    assert gid["dataType"] == ["sc:Text", "biolink:Gene"]


def test_prefix_column_reads_the_prefix_per_row():
    p = plan()
    p["layouts"][0]["entities"][0]["columns"]["GeneID"] = {
        "property": "biolink:id",
        "prefix_column": "Evidence",
        "id_form": "mixed",
    }
    out, _ = apply_plan(croissant(), p)
    gid = rs_by_id(out, "rs_human")["field"][0]["subField"][0]
    pre = next(a for a in gid["annotation"] if a["name"] == "id_prefix")
    assert pre["source"]["extract"]["column"] == "Evidence" and "value" not in pre


def test_same_code_mapping_shares_one_table_across_layouts():
    p = plan()
    other = copy.deepcopy(p["layouts"][0])
    other.update(id="gd2", match={"layout": LAYOUT, "record_set_name": "mouse"})
    p["layouts"].append(other)
    _out, counts = apply_plan(croissant(), p)
    assert counts["codes_tables"] == 1


def test_unknown_and_renamed_keys_warn():
    import apply_plan as ap

    p = plan(skipped_layouts=[])
    p["layouts"][0]["columns"]["PMIDs"]["seperator"] = "|"
    apply_plan(croissant(), p)
    assert any("renamed" in w for w in ap.WARNINGS) and any("'seperator'" in w for w in ap.WARNINGS)


def test_json_path_entities_are_typed_individually():
    import apply_plan as ap

    c = croissant()
    for f in rs_by_id(c, "rs_human")["field"]:
        f["source"]["extract"] = {"jsonPath": f"$.data[*].{f['name']}"}
    out, _ = apply_plan(
        c, plan(layouts=[{**plan()["layouts"][0], "match": {"record_sets": ["rs_human"]}}])
    )
    rs = rs_by_id(out, "rs_human")
    assert not any(f.get("subField") for f in rs["field"])
    gid = next(f for f in rs["field"] if f["name"] == "GeneID")
    assert "biolink:Gene" in gid["dataType"] and any("JSON paths" in w for w in ap.WARNINGS)


def test_optional_columns_are_skipped_where_missing():
    p = plan(
        layouts=[
            {
                "id": "any",
                "match": {"has_columns": ["X"]},
                "columns": {
                    "X": {"property": "biolink:name"},
                    "Y": {"property": "biolink:symbol", "optional": True},
                },
            }
        ]
    )
    out, counts = apply_plan(croissant(), p)
    assert counts["record_sets"] == 1
    assert rs_by_id(out, "rs_other")["field"][0]["equivalentProperty"] == "biolink:name"


def test_null_override_removes_a_library_key():
    p = plan(column_library={"pmids": {"property": "biolink:publications", "list_separator": "|"}})
    p["layouts"][0]["columns"]["PMIDs"] = {"use": "pmids", "list_separator": None}
    out, _ = apply_plan(croissant(), p)
    pm = next(f for f in rs_by_id(out, "rs_human")["field"] if f["name"] == "PMIDs")
    assert "annotation" not in pm


def test_constant_from_record_set_name_and_has_columns_specificity():
    c = {
        "@context": {},
        "distribution": [],
        "recordSet": [
            {
                "@id": f"rs{i}",
                "name": f"Records from PGS00000{i}.txt",
                "field": [
                    {
                        "@id": f"rs{i}/{n}",
                        "name": n,
                        "dataType": "sc:Text",
                        "source": {"extract": {"column": n}},
                    }
                    for n in cols
                ],
            }
            for i, cols in ((1, ["a", "b"]), (2, ["a"]))
        ],
    }
    const = {
        "name": "score",
        "property": "rcv:score",
        "name_pattern": r"(PGS\d{6})",
        "value": "pgs:{1}",
    }
    plan = {
        "layouts": [
            {"id": "wide", "match": {"has_columns": ["a", "b"]}, "constants": [const]},
            {"id": "narrow", "match": {"has_columns": ["a"]}},
        ]
    }
    out, _ = apply_plan(c, plan)
    assert [f["value"] for f in out["recordSet"][0]["field"] if f["name"] == "score"] == [
        "pgs:PGS000001"
    ]
    assert not any(f["name"] == "score" for f in out["recordSet"][1]["field"])
    plan["layouts"][0]["constants"] = [{**const, "name_pattern": r"(XYZ\d+)"}]
    with pytest.raises(PlanError, match="doesn't match"):
        apply_plan(c, plan)


def test_derived_field_reads_the_same_column_with_another_property():
    c = {
        "@context": {},
        "distribution": [],
        "recordSet": [
            {
                "@id": "rs",
                "name": "rs",
                "field": [
                    {
                        "@id": "rs/q",
                        "name": "Qualifier",
                        "dataType": "sc:Text",
                        "source": {"fileObject": {"@id": "f"}, "extract": {"column": "Qualifier"}},
                    }
                ],
            }
        ],
    }
    plan = {
        "layouts": [
            {
                "id": "d",
                "match": {"record_sets": ["rs"]},
                "columns": {
                    "Qualifier": {
                        "property": "biolink:predicate",
                        "codes": {"is_implicated_in": "biolink:contributes_to"},
                        "derived": [
                            {
                                "name": "negated",
                                "property": "biolink:negated",
                                "true_values": ["is_not_implicated_in"],
                                "source": "documentation",
                                "confidence": "high",
                            }
                        ],
                    }
                },
            }
        ]
    }
    out, _ = apply_plan(c, plan)
    neg = next(f for f in out["recordSet"][0]["field"] if f["name"] == "negated")
    assert neg["equivalentProperty"] == "biolink:negated"
    assert neg["source"] == {"fileObject": {"@id": "f"}, "extract": {"column": "Qualifier"}}
    assert any(a.get("equivalentProperty") == "rcv:trueValue" for a in neg.get("annotation", []))
    plan["layouts"][0]["columns"]["Qualifier"]["derived"][0]["name"] = "Qualifier"
    with pytest.raises(PlanError, match="clashes"):
        apply_plan(c, plan)


def test_fields_without_ids_get_clean_unique_ids():
    c = {
        "@context": {},
        "distribution": [],
        "recordSet": [
            {
                "@id": "rs",
                "name": "rs",
                "field": [
                    {
                        "name": " #skip",
                        "dataType": "sc:Text",
                        "source": {"fileObject": {"@id": "f"}, "extract": {"column": " #skip"}},
                    },
                    {
                        "name": "#skip",
                        "dataType": "sc:Text",
                        "source": {"fileObject": {"@id": "f"}, "extract": {"column": "#skip"}},
                    },
                    {
                        "name": "Gene ID",
                        "dataType": "sc:Text",
                        "source": {"fileObject": {"@id": "f"}, "extract": {"column": "Gene ID"}},
                    },
                ],
            }
        ],
    }
    plan = {
        "layouts": [
            {
                "id": "l",
                "match": {"record_sets": ["rs"]},
                "columns": {"Gene ID": {"property": "biolink:id", "id_prefix": "NCBIGene"}},
            }
        ]
    }
    out, _ = apply_plan(c, plan)
    ids = [f["@id"] for f in out["recordSet"][0]["field"]]
    assert ids == ["rs/skip", "rs/skip_2", "rs/Gene_ID"]


def test_entity_named_like_a_column_is_refused_and_data_context_is_json():
    c = {
        "@context": {"data": "cr:data"},
        "distribution": [],
        "recordSet": [
            {
                "@id": "rs",
                "name": "rs",
                "field": [
                    {
                        "@id": "rs/species",
                        "name": "Species",
                        "dataType": "sc:Text",
                        "source": {"fileObject": {"@id": "f"}, "extract": {"column": "Species"}},
                    },
                    {
                        "@id": "rs/id",
                        "name": "ICTV_ID",
                        "dataType": "sc:Text",
                        "source": {"fileObject": {"@id": "f"}, "extract": {"column": "ICTV_ID"}},
                    },
                ],
            }
        ],
    }
    ent = {
        "name": "species",
        "type": "biolink:OrganismTaxon",
        "columns": {"ICTV_ID": {"property": "biolink:id"}},
    }
    with pytest.raises(PlanError, match="used more than once"):
        apply_plan(
            c, {"layouts": [{"id": "l", "match": {"record_sets": ["rs"]}, "entities": [ent]}]}
        )
    out, _ = apply_plan(
        c,
        {
            "layouts": [
                {
                    "id": "l",
                    "match": {"record_sets": ["rs"]},
                    "entities": [{**ent, "name": "species_taxon"}],
                }
            ]
        },
    )
    assert out["@context"]["data"] == {"@id": "cr:data", "@type": "@json"}


def test_json_entity_fallback_gives_the_id_the_entity_role():
    c = {
        "@context": {},
        "distribution": [],
        "recordSet": [
            {
                "@id": "rs",
                "name": "rs",
                "field": [
                    {
                        "@id": f"rs/{n}",
                        "name": n,
                        "dataType": "sc:Text",
                        "source": {
                            "fileObject": {"@id": "f"},
                            "extract": {"jsonPath": f"$.results[*].{n}"},
                        },
                    }
                    for n in ("primaryAccession", "taxonId", "scientificName")
                ],
            }
        ],
    }
    plan = {
        "layouts": [
            {
                "id": "l",
                "match": {"record_sets": ["rs"]},
                "row": {"type": "biolink:Protein"},
                "columns": {"primaryAccession": {"property": "biolink:id"}},
                "entities": [
                    {
                        "name": "taxon",
                        "type": "biolink:OrganismTaxon",
                        "role": "biolink:in_taxon",
                        "columns": {
                            "taxonId": {"property": "biolink:id", "id_prefix": "NCBITaxon"},
                            "scientificName": {"property": "biolink:name"},
                        },
                    }
                ],
            }
        ]
    }
    out, _ = apply_plan(c, plan)
    props = {f["name"]: f.get("equivalentProperty") for f in out["recordSet"][0]["field"]}
    assert props == {
        "primaryAccession": "biolink:id",
        "taxonId": "biolink:in_taxon",
        "scientificName": None,
    }


def test_constant_value_map():
    c = {
        "@context": {},
        "distribution": [],
        "recordSet": [
            {
                "@id": f"rs{i}",
                "name": f"{sp}_protein_masterlist.csv",
                "field": [
                    {
                        "@id": f"rs{i}/a",
                        "name": "a",
                        "dataType": "sc:Text",
                        "source": {"extract": {"column": "a"}},
                    }
                ],
            }
            for i, sp in ((1, "human"), (2, "mouse"))
        ],
    }
    const = {
        "name": "taxon",
        "property": "biolink:in_taxon",
        "name_pattern": r"^(\w+?)_",
        "value_map": {"human": "NCBITaxon:9606", "mouse": "NCBITaxon:10090"},
    }
    out, _ = apply_plan(
        c,
        {"layouts": [{"id": "l", "match": {"record_sets": ["rs1", "rs2"]}, "constants": [const]}]},
    )
    assert [f["value"] for rs in out["recordSet"] for f in rs["field"] if f["name"] == "taxon"] == [
        "NCBITaxon:9606",
        "NCBITaxon:10090",
    ]


def test_prefix_codes_map_namespace_keys_to_prefixes():
    c = {
        "@context": {},
        "distribution": [],
        "recordSet": [
            {
                "@id": "rs",
                "name": "rs",
                "field": [
                    {
                        "@id": f"rs/{n}",
                        "name": n,
                        "dataType": "sc:Text",
                        "source": {"fileObject": {"@id": "f"}, "extract": {"column": n}},
                    }
                    for n in ("xref_key", "xref_id")
                ],
            }
        ],
    }
    plan = {
        "prefixes": {"OMIM": "https://omim.org/entry/"},
        "layouts": [
            {
                "id": "l",
                "match": {"record_sets": ["rs"]},
                "columns": {
                    "xref_id": {
                        "property": "biolink:xref",
                        "id_form": "bare",
                        "prefix_column": "xref_key",
                        "prefix_codes": {"protein_xref_omim": "OMIM"},
                    }
                },
            }
        ],
    }
    out, _counts = apply_plan(c, plan)
    note = next(
        a
        for f in out["recordSet"][0]["field"]
        for a in f.get("annotation", [])
        if a["name"] == "id_prefix"
    )
    table = next(
        r for r in out["recordSet"] if r["@id"] == note["references"]["field"]["@id"].split("/")[0]
    )
    assert table["data"] == [
        {
            f"{table['@id']}/name": "protein_xref_omim",
            f"{table['@id']}/url": "https://omim.org/entry/",
        }
    ]
    plan["layouts"][0]["columns"]["xref_id"]["prefix_codes"] = {"protein_xref_omim": "MIM"}
    with pytest.raises(PlanError, match="not in the plan's prefixes"):
        apply_plan(c, plan)


def test_maintainer_answers_override_add_and_skip():
    from apply_plan import merge_answers

    c = {
        "@context": {},
        "distribution": [],
        "recordSet": [
            {
                "@id": "rs",
                "name": "GTEx edges",
                "field": [
                    {
                        "@id": f"rs/{n}",
                        "name": n,
                        "dataType": "sc:Text",
                        "source": {"extract": {"column": n}},
                    }
                    for n in ("slope", "score", "taxon")
                ],
            }
        ],
    }
    plan = {
        "layouts": [
            {
                "id": "edges",
                "match": {"record_sets": ["rs"]},
                "columns": {
                    "taxon": {
                        "property": "biolink:in_taxon",
                        "source": "agent",
                        "confidence": "medium",
                    },
                    "score": {"property": "biolink:has_confidence_score"},
                },
            }
        ]
    }
    answers = [
        {
            "question": "CTD taxon?",
            "answer": "organism the interaction was observed in",
            "by": "PI",
            "date": "2026-10-20",
            "applies_to": {"column": "taxon"},
            "spec": {"property": "biolink:species_context_qualifier"},
        },
        {
            "question": "slope?",
            "answer": "slope of allele count vs expression",
            "by": "PI",
            "date": "2026-10-20",
            "applies_to": {"column": "slope"},
            "spec": {"property": "biolink:effect_size"},
        },
        {
            "question": "score?",
            "answer": "imprecise across sources",
            "by": "PI",
            "applies_to": {"column": "score"},
            "skip": "meaning differs by source and isn't documented",
        },
        {"question": "nowhere?", "answer": "x", "applies_to": {"column": "not_a_column"}},
    ]
    merged, report = merge_answers(plan, c, answers)
    cols = merged["layouts"][0]["columns"]
    assert (
        cols["taxon"]["property"] == "biolink:species_context_qualifier"
        and cols["taxon"]["source"] == "maintainer"
    )
    assert cols["slope"] == {
        "optional": True,
        "property": "biolink:effect_size",
        "source": "maintainer",
        "confidence": "high",
        "basis": "Maintainer: slope of allele count vs expression (PI, 2026-10-20)",
    }
    assert "score" not in cols and "isn't documented" in merged["skipped"][0]["reason"]
    assert "matched no layout" in report[3]
    assert (
        plan["layouts"][0]["columns"]["taxon"]["property"] == "biolink:in_taxon"
    )  # the input plan is untouched
    out, _ = apply_plan(c, merged)
    assert (
        next(f for f in out["recordSet"][0]["field"] if f["name"] == "slope")["equivalentProperty"]
        == "biolink:effect_size"
    )


def test_match_layouts_list():
    from common import column_names, layout_id

    c = {
        "@context": {},
        "distribution": [],
        "recordSet": [
            {
                "@id": f"rs{i}",
                "name": f"rs{i}",
                "field": [
                    {
                        "@id": f"rs{i}/{n}",
                        "name": n,
                        "dataType": "sc:Text",
                        "source": {"extract": {"column": n}},
                    }
                    for n in cols
                ],
            }
            for i, cols in ((1, ["a", "b"]), (2, ["a", "b", "c"]), (3, ["x"]))
        ],
    }
    ids = [layout_id(column_names(rs)) for rs in c["recordSet"]]
    plan = {
        "layouts": [
            {
                "id": "ab",
                "match": {"layouts": ids[:2]},
                "columns": {"a": {"property": "biolink:id"}},
            }
        ]
    }
    _out, counts = apply_plan(c, plan)
    assert counts["record_sets"] == 2


def test_entity_constant_value_map_column_description_and_no_id():
    c = {
        "@context": {},
        "distribution": [],
        "recordSet": [
            {
                "@id": "rs",
                "name": "Human_Mouse_orthologs.chip",
                "field": [
                    {
                        "@id": f"rs/{n}",
                        "name": n,
                        "dataType": "sc:Text",
                        "description": "From the file.",
                        "source": {"fileObject": {"@id": "f"}, "extract": {"column": n}},
                    }
                    for n in ("Probe Set ID", "Gene Symbol")
                ],
            }
        ],
    }
    plan = {
        "layouts": [
            {
                "id": "orth",
                "match": {"record_sets": ["rs"]},
                "entities": [
                    {
                        "name": "target",
                        "type": "biolink:Gene",
                        "role": "biolink:object",
                        "no_id": "known only by symbol",
                        "columns": {
                            "Gene Symbol": {
                                "property": "biolink:symbol",
                                "description": "Mouse symbols.",
                            }
                        },
                        "constants": [
                            {
                                "name": "taxon",
                                "property": "biolink:in_taxon",
                                "name_pattern": r"_(\w+?)_orthologs",
                                "value_map": {"Mouse": "NCBITaxon:10090"},
                            }
                        ],
                    }
                ],
            }
        ]
    }
    out, _ = apply_plan(c, plan)
    group = next(f for f in out["recordSet"][0]["field"] if f["name"] == "target")
    assert [sf["value"] for sf in group["subField"] if sf["name"] == "taxon"] == ["NCBITaxon:10090"]
    symbol = next(sf for sf in group["subField"] if sf["name"] == "Gene Symbol")
    assert symbol["description"] == "From the file. Mouse symbols."
