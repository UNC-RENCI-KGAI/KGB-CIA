"""Tests for dialects.py and how apply_plan and sample use dialects. No network."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import dialects  # noqa: E402
from apply_plan import PlanError, apply_plan  # noqa: E402
from sample import table_rows  # noqa: E402

GAF = [
    "!gaf-version: 2.2",
    "!generated-by: GOC",
    "UniProtKB\tA0A024RBG1\tNUDT4B\tenables\tGO:0003723\tGO_REF:0000043\tIEA",
    "UniProtKB\tA0A075B6H7\tIGKV3-7\tinvolved_in\tGO:0002250\tGO_REF:0000043\tIEA",
]
GAF_COLS = [
    "DB",
    "DB Object ID",
    "DB Object Symbol",
    "Qualifier",
    "GO ID",
    "DB:Reference",
    "Evidence Code",
]


def test_registry_entries_are_complete():
    for name, d in dialects.registry().items():
        assert d["columns"] in ("header", "field_order"), name
        assert d["media_type"] and d["spec"].startswith("https://"), name
        assert "delimiter" in d["csvw"], name


def test_resolve_named_and_inline():
    assert dialects.resolve("gaf-2.2")["csvw"]["commentPrefix"] == "!"
    inline = dialects.resolve({"based_on": "tsv", "csvw": {"commentPrefix": "%"}})
    assert inline["csvw"] == {
        "delimiter": "\t",
        "header": True,
        "quoteChar": None,
        "commentPrefix": "%",
    }
    with pytest.raises(KeyError):
        dialects.resolve("gaf-9")
    with pytest.raises(KeyError):
        dialects.resolve({"csvw": {"delimiter": ","}})  # no based_on and no columns


def test_parse_field_order_skips_comments():
    rows = dialects.parse(GAF, dialects.resolve("gaf-2.2"), GAF_COLS)
    assert (
        len(rows) == 2
        and rows[0]["DB Object Symbol"] == "NUDT4B"
        and rows[1]["Evidence Code"] == "IEA"
    )
    assert dialects.fit(rows, GAF_COLS, dialects.resolve("gaf-2.2"))[0] == 1.0


def test_parse_vcf_header_and_mitab_nulls():
    vcf = ["##fileformat=VCFv4.3", "##INFO=<ID=X>", "#CHROM\tPOS\tID", "1\t100\trs1"]
    assert dialects.parse(vcf, dialects.resolve("vcf-4"), ["#CHROM", "POS", "ID"]) == [
        {"#CHROM": "1", "POS": "100", "ID": "rs1"}
    ]
    # a croissant whose names drop the '#' (FlyBase) gets them without it
    assert dialects.parse(vcf, dialects.resolve("vcf-4"), ["CHROM", "POS", "ID"])[0]["CHROM"] == "1"
    mitab = ["#ID(s) interactor A\tNegative", "uniprotkb:P1\t-"]
    assert dialects.parse(mitab, dialects.resolve("mitab-2.7"), [])[0]["Negative"] is None


def test_wrong_dialect_fits_badly():
    rows = dialects.parse(GAF, dialects.resolve("csv"), GAF_COLS)
    assert dialects.fit(rows, GAF_COLS, dialects.resolve("csv"))[0] < 0.9


def test_croissant_round_trip_and_encoding_formats():
    d = dialects.resolve("vcf-4")
    props = dialects.to_croissant(d)
    assert (
        props["csvw:dialect"]["csvw:commentPrefix"] == "##"
        and "csvw:quoteChar" not in props["csvw:dialect"]
    )
    back = dialects.from_croissant(props)
    assert (
        back["csvw"]["delimiter"] == "\t"
        and back["columns"] == "header"
        and back["name"] == "vcf-4"
    )
    gz = {"contentUrl": "https://x.org/a.vcf.gz", "encodingFormat": "application/gzip"}
    assert dialects.encoding_formats(gz, d) == [
        "text/tab-separated-values",
        "application/gzip",
        d["spec"],
    ]
    # an inline dialect with no media type keeps the file's table type
    plain = {"contentUrl": "https://x.org/a.txt", "encodingFormat": "text/plain"}
    assert dialects.encoding_formats(plain, {"csvw": {}, "columns": "header"}) == ["text/plain"]


def test_table_rows_use_the_files_own_dialect():
    dist = {"contentUrl": "x.gaf", **dialects.to_croissant(dialects.resolve("gaf-2.2"))}
    rows = table_rows("\n".join(GAF).encode() + b"\n", dist, GAF_COLS, 10)
    assert rows[0]["GO ID"] == "GO:0003723"


def croissant_with_two_files():
    def rs(i, file_id):
        return {
            "@id": f"rs{i}",
            "name": f"rs{i}",
            "field": [
                {
                    "@id": f"rs{i}/{c}",
                    "name": c,
                    "dataType": "sc:Text",
                    "source": {"fileObject": {"@id": file_id}, "extract": {"column": c}},
                }
                for c in GAF_COLS
            ],
        }

    return {
        "@context": {},
        "distribution": [
            {
                "@type": "cr:FileObject",
                "@id": "f1",
                "contentUrl": "https://x.org/a.gaf.gz",
                "encodingFormat": "application/gzip",
            },
            {
                "@type": "cr:FileObject",
                "@id": "f2",
                "contentUrl": "https://x.org/b.gpad",
                "encodingFormat": "text/plain",
            },
        ],
        "recordSet": [rs(1, "f1"), rs(2, "f2")],
    }


def test_apply_plan_writes_dialects_onto_files():
    plan = {
        "layouts": [
            {"id": "gaf", "match": {"record_sets": ["rs1"]}, "dialect": "gaf-2.2"},
            {"id": "gpad", "match": {"record_sets": ["rs2"]}, "dialect": "gpad-2.0"},
        ]
    }
    out, counts = apply_plan(croissant_with_two_files(), plan)
    f1, f2 = out["distribution"]
    assert counts["dialect_files"] == 2
    assert f1["encodingFormat"][:2] == ["text/tab-separated-values", "application/gzip"]
    assert f1["rcv:dialect"] == "gaf-2.2" and f1["rcv:columnOrder"] == "field_order"
    assert f2["rcv:dialect"] == "gpad-2.0" and "application/gzip" not in f2["encodingFormat"]
    assert out["@context"]["csvw"] == "http://www.w3.org/ns/csvw#"


def test_apply_plan_rejects_conflicting_dialects_and_unknown_names():
    c = croissant_with_two_files()
    c["recordSet"][1]["field"][0]["source"]["fileObject"]["@id"] = "f1"
    plan = {
        "layouts": [
            {"id": "a", "match": {"record_sets": ["rs1"]}, "dialect": "gaf-2.2"},
            {"id": "b", "match": {"record_sets": ["rs2"]}, "dialect": "gpad-2.0"},
        ]
    }
    with pytest.raises(PlanError, match="different dialects"):
        apply_plan(c, plan)
    with pytest.raises(PlanError, match="no dialect"):
        apply_plan(
            croissant_with_two_files(),
            {"layouts": [{"id": "a", "match": {"record_sets": ["rs1"]}, "dialect": "gaf-9"}]},
        )


def test_by_suffix_chooses_per_file():
    spec = {"by_suffix": {".csv": "ctd-csv", ".tsv": "ctd-tsv"}}
    assert (
        dialects.for_file(spec, {"contentUrl": "https://x/CTD_genes.tsv.gz"})["name"] == "ctd-tsv"
    )
    assert dialects.for_file(spec, {"contentUrl": "https://x/CTD_genes.CSV"})["name"] == "ctd-csv"
    assert dialects.for_file(spec, {"contentUrl": "https://x/CTD_genes.xml.gz"}) is None
    plan = {
        "layouts": [
            {
                "id": "go",
                "match": {"record_sets": ["rs1", "rs2"]},
                "dialect": {"by_suffix": {".gaf": "gaf-2.2", ".gpad": "gpad-2.0"}},
            }
        ]
    }
    out, _ = apply_plan(croissant_with_two_files(), plan)
    assert [d["rcv:dialect"] for d in out["distribution"]] == ["gaf-2.2", "gpad-2.0"]
    with pytest.raises(PlanError, match="no entry"):
        apply_plan(
            croissant_with_two_files(),
            {
                "layouts": [
                    {
                        "id": "go",
                        "match": {"record_sets": ["rs1", "rs2"]},
                        "dialect": {"by_suffix": {".gaf": "gaf-2.2"}},
                    }
                ]
            },
        )


def test_headerless_files_keep_their_column_order_after_grouping():
    plan = {
        "layouts": [
            {
                "id": "gaf",
                "match": {"record_sets": ["rs1"]},
                "dialect": "gaf-2.2",
                "entities": [
                    {
                        "name": "term",
                        "type": "biolink:BiologicalProcess",
                        "source": "agent",
                        "confidence": "high",
                        "columns": {"GO ID": {"property": "biolink:id"}},
                    }
                ],
            }
        ]
    }
    out, _ = apply_plan(croissant_with_two_files(), plan)
    f1 = out["distribution"][0]
    assert f1["rcv:columnNames"] == GAF_COLS
    assert [x["name"] for x in out["recordSet"][0]["field"]][:5] == GAF_COLS[:4] + ["term"]
    back = dialects.from_croissant(f1)
    assert dialects.parse(GAF, back, ["term"])[0]["GO ID"] == "GO:0003723"


def test_sparse_rows_fit():
    d = dialects.resolve("tsv")
    rows = dialects.parse(["a\tb\tc", "1\t\t", "2\t\t"], d, ["a", "b", "c"])
    assert dialects.fit(rows, ["a", "b", "c"], d)[0] == 1.0
    rows = dialects.parse(["a\tb\tc", "1", "2\tx\ty"], d, ["a", "b", "c"])
    assert dialects.fit(rows, ["a", "b", "c"], d)[0] == 0.5


def test_trailing_delimiter_is_not_a_wide_row():
    d = dialects.resolve("tsv")
    rows = dialects.parse(["a\tb\t", "1\t2\t", "3\t4\t5"], d, ["a", "b"])
    assert "__extra__" not in rows[0] and rows[1]["__extra__"] == ["5"]


def test_gmt_rest_column_takes_remaining_cells():
    d = dialects.resolve("gmt")
    cols = ["geneSetName", "description", "geneIdentifiers"]
    rows = dialects.parse(["SET_A\thttp://x\t1\t2\t3", "SET_B\t> desc\t9"], d, cols)
    assert rows[0]["geneIdentifiers"] == ["1", "2", "3"] and rows[1]["geneIdentifiers"] == ["9"]
    assert dialects.fit(rows, cols, d)[0] == 1.0
    props = dialects.to_croissant(d, cols)
    assert props["rcv:restColumn"] == "geneIdentifiers"
    back = dialects.from_croissant(props)
    assert dialects.parse(["S\td\ta\tb"], back, cols)[0]["geneIdentifiers"] == ["a", "b"]


def test_flybase_header_row_with_hash():
    lines = [
        "## FlyBase Gene Report",
        "## Generated: 2026",
        "#gene_symbol\tprimary_FBgn#",
        "Act5C\tFBgn0000042",
        "## Finished report",
    ]
    rows = dialects.parse(lines, dialects.resolve("flybase-tsv"), ["gene_symbol", "primary_FBgn#"])
    assert rows == [{"gene_symbol": "Act5C", "primary_FBgn#": "FBgn0000042"}]


def test_bom_and_quoted_newlines():
    lines = [
        "﻿ID,label,definition",
        'VO:1,alum,"an adjuvant',
        'made of aluminium"',
        'VO:2,MF59,"oil, water"',
    ]
    rows = dialects.parse(lines, dialects.resolve("csv"), ["ID", "label", "definition"])
    assert rows == [
        {"ID": "VO:1", "label": "alum", "definition": "an adjuvant\nmade of aluminium"},
        {"ID": "VO:2", "label": "MF59", "definition": "oil, water"},
    ]
    guessed = table_rows(
        "\n".join(lines).encode() + b"\n", {"contentUrl": "x.csv"}, ["ID", "label", "definition"], 5
    )
    assert guessed == rows


def test_fit_rejects_header_read_as_data_and_robot_template_rows():
    cols = ["subject_id", "predicate_id", "object_id"]
    lines = ["subject_id\tpredicate_id\tobject_id", "VO:1\tskos:exactMatch\tRXCUI:2"]
    rows = dialects.parse(lines, dialects.resolve("tsv-noheader"), cols)
    assert dialects.fit(rows, cols, dialects.resolve("tsv-noheader"))[0] == 0.0
    robot = ["ID,LABEL,parent", "ID,LABEL,SC %", "VO:1,alum,VO:9"]
    cols = ["ID", "LABEL", "parent"]
    assert (
        dialects.fit(
            dialects.parse(robot, dialects.resolve("csv"), cols), cols, dialects.resolve("csv")
        )[0]
        == 0.0
    )
    rows = dialects.parse(robot, dialects.resolve("robot-template-csv"), cols)
    assert rows == [{"ID": "VO:1", "LABEL": "alum", "parent": "VO:9"}]
    assert dialects.fit(rows, cols, dialects.resolve("robot-template-csv"))[0] == 1.0


def test_header_in_the_last_comment_line():
    lines = [
        "# Alliance of Genome Resources",
        "# Date: 2026-10-01",
        "#ID(s) interactor A\tTaxid interactor A",
        "flybase:FBgn1\ttaxid:7227",
        "# trailing note",
        "flybase:FBgn2\ttaxid:7227",
    ]
    cols = ["#ID(s) interactor A", "Taxid interactor A"]
    rows = dialects.parse(lines, dialects.resolve("tsv-comment-header"), cols)
    assert rows == [
        {"#ID(s) interactor A": "flybase:FBgn1", "Taxid interactor A": "taxid:7227"},
        {"#ID(s) interactor A": "flybase:FBgn2", "Taxid interactor A": "taxid:7227"},
    ]
    back = dialects.from_croissant(dialects.to_croissant(dialects.resolve("tsv-comment-header")))
    assert dialects.parse(lines, back, cols) == rows


def test_quoted_tsv_header():
    lines = ['"complex"\t"enabled function"', '"GO:0000502"\t"GO:0004298"']
    cols = ["complex", "enabled function"]
    rows = dialects.parse(lines, dialects.resolve("tsv-quoted"), cols)
    assert rows == [{"complex": "GO:0000502", "enabled function": "GO:0004298"}]
    assert dialects.fit(rows, cols, dialects.resolve("tsv-quoted"))[0] == 1.0
    assert (
        dialects.fit(
            dialects.parse(lines, dialects.resolve("tsv"), cols), cols, dialects.resolve("tsv")
        )[0]
        < 0.9
    )


def test_a_bracketed_header_isnt_json():
    data = b"[class]\tlabel\nVO:0000001\tvaccine\n"
    rows = table_rows(data, {"contentUrl": "x.tsv"}, ["[class]", "label"], 10)
    assert rows == [{"[class]": "VO:0000001", "label": "vaccine"}]
    with pytest.raises(ValueError, match="the file is JSON"):
        table_rows(b'[{"a": 1}]\n', {"contentUrl": "x.tsv"}, ["a"], 10)


def test_blank_lines_inside_quoted_fields_are_kept():
    data = b'id,note\n1,"first\n\nsecond"\n\n2,plain\n'
    rows = table_rows(data, {"contentUrl": "x.csv"}, ["id", "note"], 10)
    assert rows == [{"id": "1", "note": "first\n\nsecond"}, {"id": "2", "note": "plain"}]


def test_backslash_escapes_when_doublequote_is_false():
    d = dialects.resolve({"based_on": "csv", "csvw": {"doubleQuote": False}})
    rows = dialects.parse(["id,q", '1,"say \\"hi\\", then go"'], d, ["id", "q"])
    assert rows == [{"id": "1", "q": 'say "hi", then go'}]


def test_comment_header_skips_rule_lines():
    lines = ["#gene\tsymbol", "#-----", "#", "FBgn1\tabc"]
    rows = dialects.parse(lines, dialects.resolve("tsv-comment-header"), ["gene", "symbol"])
    assert rows == [{"gene": "FBgn1", "symbol": "abc"}]


def test_ucum_division_matches_qudt_codes():
    import vocab

    assert vocab.ucum_division("kg/m2") == "kg.m-2"
    assert vocab.ucum_division("mg/dL") == "mg.dL-1"
    assert vocab.ucum_division("umol/L") == "umol.L-1"
    assert vocab.ucum_division("1/s") == "s-1"
    assert vocab.ucum_division("per second") == "per second"


def test_data_with_short_codes_isnt_a_robot_template():
    cols = ["grade", "class", "label"]
    lines = ["grade,class,label", "A,I,first", "C,II,second"]
    rows = dialects.parse(lines, dialects.resolve("csv"), cols)
    assert dialects.fit(rows, cols, dialects.resolve("csv"))[0] == 1.0


def test_header_names_keep_spaces_the_croissant_keeps():
    cols = ["Genus ", "Species"]
    rows = table_rows(
        b"Genus ,Species\nAlphavirus,Sindbis virus\n", {"contentUrl": "x.csv"}, cols, 5
    )
    assert rows == [{"Genus ": "Alphavirus", "Species": "Sindbis virus"}]
    rows = dialects.parse(
        ["Genus ,Species", "Alphavirus,Sindbis virus"], dialects.resolve("csv"), cols
    )
    assert rows == [{"Genus ": "Alphavirus", "Species": "Sindbis virus"}]
