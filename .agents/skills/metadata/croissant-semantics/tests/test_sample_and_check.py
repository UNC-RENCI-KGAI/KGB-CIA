"""Tests for sample.py parsing and check.py's offline checks. No network."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from check import Findings, check_plan, coverage  # noqa: E402
from sample import json_rows, shape, table_rows  # noqa: E402


def test_table_rows_skip_comments_and_use_header():
    data = b"# CTD fields\n!gaf-version: 2.2\nGeneID\tSymbol\n1\tA1BG\n2\tA2M\npartial"
    rows = table_rows(
        data, {"encodingFormat": "text/tab-separated-values"}, ["GeneID", "Symbol"], 10
    )
    assert rows == [{"GeneID": "1", "Symbol": "A1BG"}, {"GeneID": "2", "Symbol": "A2M"}]


def test_table_rows_without_header_map_by_position():
    data = b"!comment\nUniProtKB\tP0C767\nUniProtKB\tQ69027\n"
    rows = table_rows(data, {"contentUrl": "x.gaf"}, ["DB", "DB Object ID"], 10)
    assert rows[0] == {"DB": "UniProtKB", "DB Object ID": "P0C767"}


def test_json_rows_follow_json_paths():
    data = b'{"data": [{"id": "FB:1", "n": "a"}, {"id": "FB:2", "n": "b"}]}'
    fields = [
        {"name": "id", "source": {"extract": {"jsonPath": "$.data[*].id"}}},
        {"name": "n", "source": {"extract": {"jsonPath": "$.data[*].n"}}},
    ]
    assert json_rows(data, fields, 10) == [{"id": "FB:1", "n": "a"}, {"id": "FB:2", "n": "b"}]


def test_shape_describes_values():
    assert "CURIEs, prefixes MESH (2)" in shape(["MESH:D1", "MESH:D2"])
    assert "list joined by '|'" in shape(["1|2", "3|4", "5"])
    assert "name suggests bare numeric ids" in shape(["1", "2", "2"], "GeneID")
    assert "bare identifiers" in shape(["FBgn0031086", "FBgn0000001"])
    assert shape(["", None]) == "always empty in sample"


def plan_with(**layout):
    return {
        "prefixes": {"NCBIGene": "http://x.org/"},
        "layouts": [{"id": "l", "match": {"layout": "L1"}, **layout}],
    }


MODEL = {
    "classes": {
        "entity": {},
        "association": {"is_a": "entity"},
        "gene to disease association": {"is_a": "association"},
        "named thing": {"is_a": "entity"},
        "gene": {"is_a": "named thing", "id_prefixes": ["NCBIGene", "HGNC"]},
    },
    "slots": {"id": {}, "subject": {}, "symbol": {}},
}


def errors(plan):
    f = Findings()
    check_plan(plan, f, MODEL)
    return [m for level, _, m in f.items if level == "error"], [
        m for level, _, m in f.items if level == "warning"
    ]


def test_check_plan_catches_unknown_terms_and_prefixes():
    errs, _ = errors(
        plan_with(
            row={"type": "biolink:Gen", "source": "agent", "confidence": "high"},
            columns={
                "x": {
                    "type": "FOO:1",
                    "property": "biolink:nonsense",
                    "source": "agent",
                    "confidence": "low",
                }
            },
        )
    )
    assert any("not a Biolink class" in e for e in errs)
    assert any("prefix FOO" in e for e in errs)
    assert any("not a Biolink slot" in e for e in errs)


def test_check_plan_warns_about_unlisted_id_prefix_and_missing_confidence():
    errs, warns = errors(
        plan_with(
            entities=[
                {
                    "name": "g",
                    "type": "biolink:Gene",
                    "role": "biolink:subject",
                    "columns": {"id": {"property": "biolink:id", "id_prefix": "NCBIGene"}},
                }
            ],
            columns={"y": {"property": "biolink:symbol"}},
        )
    )
    assert not errs
    assert any("confidence" in w for w in warns)
    p = plan_with(
        entities=[
            {
                "name": "g",
                "type": "biolink:Gene",
                "source": "agent",
                "confidence": "high",
                "columns": {"id": {"property": "biolink:id", "id_prefix": "ENSEMBL"}},
            }
        ]
    )
    p["prefixes"]["ENSEMBL"] = "http://e.org/"
    _, warns = errors(p)
    assert any("doesn't list ENSEMBL" in w for w in warns)


def test_coverage_counts_fields_by_vocabulary():
    out = {
        "recordSet": [
            {
                "@id": "r",
                "dataType": ["biolink:GeneToDiseaseAssociation"],
                "field": [
                    {
                        "@id": "g",
                        "dataType": ["biolink:Gene"],
                        "subField": [
                            {
                                "@id": "a",
                                "dataType": ["sc:Text", "biolink:Gene"],
                                "equivalentProperty": "biolink:id",
                                "source": {},
                            },
                            {"@id": "b", "dataType": "sc:Text", "source": {}},
                        ],
                    },
                    {"@id": "c", "dataType": ["sc:Text", "ECO:0000001"], "source": {}},
                    {"@id": "d", "dataType": "sc:Text", "source": {}},
                ],
            },
            {"@id": "codes_x", "field": []},
        ]
    }
    cov = coverage(out)
    assert cov["record_sets"] == 1 and cov["record_sets_with_semantics"] == 1
    assert cov["fields_by_kind"] == {"biolink": 1, "grouped only": 1, "other": 1, "none": 1}


def test_rows_may_be_things_and_prefix_expansions_need_a_delimiter():
    errs, _ = errors(
        plan_with(row={"type": "biolink:Gene", "source": "agent", "confidence": "high"})
    )
    assert not errs
    p = plan_with()
    p["prefixes"]["CLINVAR"] = "http://identifiers.org/clinvar"
    errs, _ = errors(p)
    assert any("doesn't end in" in e for e in errs)


def test_id_prefix_warning_only_for_the_id_column():
    _, warns = errors(
        plan_with(
            entities=[
                {
                    "name": "g",
                    "type": "biolink:Gene",
                    "source": "agent",
                    "confidence": "high",
                    "columns": {
                        "id": {"property": "biolink:id", "id_prefix": "NCBIGene"},
                        "x": {"property": "biolink:xref", "id_prefix": "OTHER"},
                    },
                }
            ]
        )
    )
    assert not any("doesn't list" in w for w in warns)


def test_unlinked_local_terms_are_one_warning_unless_explained():
    p = plan_with()
    p["local_terms"] = [
        {"id": "rcv:a", "definition": "d"},
        {"id": "rcv:b", "definition": "d"},
        {"id": "rcv:c", "definition": "d", "no_match_reason": "no equivalent in NCIT or ECO"},
    ]
    _, warns = errors(p)
    linked = [w for w in warns if "link to no established term" in w]
    assert len(linked) == 1 and "rcv:a" in linked[0] and "rcv:c" not in linked[0]


def test_json_rows_keep_records_aligned_when_keys_are_missing():
    data = b'{"response": {"docs": [{"id": "A", "m": "x"}, {"id": "B"}, {"id": "C", "m": "z"}]}}'
    fields = [
        {"name": "id", "source": {"extract": {"jsonPath": "$.response.docs[*].id"}}},
        {"name": "m", "source": {"extract": {"jsonPath": "$.response.docs[*].m"}}},
    ]
    assert json_rows(data, fields, 10) == [
        {"id": "A", "m": "x"},
        {"id": "B", "m": None},
        {"id": "C", "m": "z"},
    ]


def test_shape_reports_json_lists_once():
    out = shape([["a", "b"], ["c"], ["d", "e"]])
    assert out.count("JSON lists") == 1


def test_obo_short_form_ids_need_declared_prefixes():
    errs, _ = errors(
        plan_with(
            columns={
                "t": {
                    "property": "biolink:id",
                    "id_form": "obo",
                    "source": "agent",
                    "confidence": "high",
                }
            }
        )
    )
    assert not errs
    errs, _ = errors(
        plan_with(
            columns={
                "t": {
                    "property": "biolink:id",
                    "id_form": "obi",
                    "source": "agent",
                    "confidence": "high",
                }
            }
        )
    )
    assert any("id_form must be one of" in e for e in errs)


def test_infores_ids_must_be_in_the_catalog():
    from check import check_infores

    plan = plan_with(
        columns={"src": {"codes": {"ChEMBL": "infores:chembl", "X": "infores:made-up"}}},
        constants=[
            {"name": "k", "property": "biolink:primary_knowledge_source", "value": "infores:old"}
        ],
    )
    f = Findings()
    check_infores(
        plan, f, {"infores:chembl": {"status": "released"}, "infores:old": {"status": "deprecated"}}
    )
    errs = [m for lv, _, m in f.items if lv == "error"]
    warns = [m for lv, _, m in f.items if lv == "warning"]
    assert len(errs) == 1 and "infores:made-up" in errs[0]
    assert len(warns) == 1 and "deprecated" in warns[0]


def test_gzip_only_tables_need_a_dialect():
    from check import check_formats

    def croissant(fmt):
        return {
            "distribution": [{"@id": "f", "name": "x.tsv.gz", "encodingFormat": fmt}],
            "recordSet": [
                {
                    "@id": "rs",
                    "name": "rs",
                    "field": [
                        {
                            "@id": "rs/a",
                            "name": "a",
                            "source": {"fileObject": {"@id": "f"}, "extract": {"column": "a"}},
                        }
                    ],
                }
            ],
        }

    plan = {"layouts": [{"id": "l", "match": {"record_sets": ["rs"]}}]}
    f = Findings()
    check_formats(croissant("application/gzip"), plan, f)
    assert any("only as gzip" in m for _, _, m in f.items)
    for fmt, p in (
        (["text/tab-separated-values", "application/gzip"], plan),
        ("application/gzip", {"layouts": [{**plan["layouts"][0], "dialect": "tsv"}]}),
    ):
        f = Findings()
        check_formats(croissant(fmt), p, f)
        assert not f.items


def test_commented_header_keeps_hash_when_the_column_has_it():
    rows = table_rows(
        b"#ID(s) interactor A\tTaxid\nflybase:FBgn1\ttaxid:7227\n",
        {"contentUrl": "x.tsv"},
        ["#ID(s) interactor A", "Taxid"],
        5,
    )
    assert rows == [{"#ID(s) interactor A": "flybase:FBgn1", "Taxid": "taxid:7227"}]


def test_obo_short_forms():
    from check import OBO_ID

    assert all(
        OBO_ID.match(v) for v in ["MONDO_0004989", "NCBITaxon_9606", "OBA_VT0000047", "PR_P12345"]
    )
    assert not any(OBO_ID.match(v) for v in ["BTK_HUMAN", "FBgn0031086", "MONDO:0004989"])


def test_name_date_orders_releases():
    from sample import name_date

    assert name_date("release-12-01-21.tar.gz") == "20211201"
    assert name_date("release-03-15-2024/built.tsv") == "20240315"
    assert name_date("BindingDB_All_202610_tsv.zip") == "20261000"
    assert name_date("gene_2026-07-01.json") == "20260701"
    assert name_date("PGS000123.txt.gz") == ""
    names = ["release-12-01-21", "release-01-05-24", "release-06-30-22"]
    assert max(names, key=name_date) == "release-01-05-24"


def test_bot_challenge_fails_loudly():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    import pytest
    import sample

    class Challenge(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server's name
            self.send_response(202)
            self.send_header("x-amzn-waf-action", "challenge")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Challenge)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    sample.USE_CACHE = False
    try:
        with pytest.raises(sample.BotCheck, match="bot challenge"):
            sample.fetch_bytes(
                f"http://127.0.0.1:{server.server_port}/genes.tsv.gz", 1000, whole=False
            )
    finally:
        sample.USE_CACHE = True
        server.shutdown()


def test_profile_streams_a_small_plain_file(capsys):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    import sample

    body = b"acc\ttype\nP1\tGI\nP2\tPDB\nP3\tGI\n"

    class Plain(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server's name
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Plain)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/x.tsv"
    croissant = {
        "distribution": [{"@id": "f", "contentUrl": url}],
        "recordSet": [
            {
                "@id": "rs",
                "field": [
                    {"name": n, "source": {"fileObject": {"@id": "f"}, "extract": {"column": n}}}
                    for n in ("acc", "type")
                ],
            }
        ],
    }
    try:
        sample.profile(croissant, croissant["recordSet"], ["type"], 5, None, 10_000)
    finally:
        server.shutdown()
    out = capsys.readouterr().out
    assert "Profiled 3 rows" in out and "2  'GI'" in out


def test_fileset_members_jsonl_and_local_files(tmp_path):
    import gzip as gz

    import pytest
    import sample

    lines = b'{"subject": "DOID:1", "predicate": "biolink:treats"}\n{"subject": "CHEBI:2"}\n{"subj'
    (tmp_path / "edges.jsonl.gz").write_bytes(gz.compress(lines))
    fields = [
        {
            "name": n,
            "source": {
                "fileSet": {"@id": "fs"},
                "extract": {"jsonPath": f"$.{n}", "path": "edges.jsonl.gz"},
            },
        }
        for n in ("subject", "predicate")
    ]
    croissant = {
        "distribution": [
            {
                "@id": "fs",
                "@type": "cr:FileSet",
                "contentUrl": "https://kb.example/graph/",
                "encodingFormat": ["application/x-ndjson", "application/gzip"],
            }
        ],
        "recordSet": [{"@id": "rs", "name": "edges", "field": fields}],
    }
    dist, why = sample.source_file(croissant["recordSet"][0], sample.distribution_index(croissant))
    assert dist["contentUrl"] == "https://kb.example/graph/edges.jsonl.gz" and not why
    assert sample.file_kind(dist, fields) == "jsonl"
    sample.URL_MAP.append(("https://kb.example/graph/", f"file://{tmp_path}/"))
    try:
        got, errors = sample.sample_rows(croissant, croissant["recordSet"], 10, 1_000_000, k=1)
    finally:
        sample.URL_MAP.clear()
    assert not errors
    assert got[0][2] == [
        {"subject": "DOID:1", "predicate": "biolink:treats"},
        {"subject": "CHEBI:2", "predicate": None},
    ]
    # a dialect whose header row doesn't name the columns is an error, not a table of empty columns
    with pytest.raises(ValueError, match="doesn't name the croissant's columns"):
        table_rows(
            b"#a\tb\n1\t2\n3\t4\n",
            {"contentUrl": "x.tsv"},
            ["ID(s) interactor A", "Taxid"],
            5,
            {
                "csvw": {"delimiter": "\t", "commentPrefix": "#", "header": True},
                "columns": "header",
            },
        )
    with pytest.raises(ValueError, match="legacy Excel"):
        sample.xlsx_rows(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 100, None, ["a"], 5)


def test_explained_warnings_become_notes():
    f = Findings(
        [
            {
                "where": "disease",
                "message": "biomarker_for",
                "reason": "cross-tab: only gene rows use it",
            },
            {"message": "never matches", "reason": "stale"},
        ]
    )
    f.warn(
        "disease predicate biolink:biomarker_for",
        "its domain is chemical entity or gene or gene product, "
        "but the subject is genomic entity (biomarker_for)",
    )
    f.warn("other", "something else")
    levels = [lv for lv, _, _ in f.items]
    assert levels == ["note", "warning"] and "only gene rows" in f.items[0][2]
    assert set(f.used) == {0}


def test_same_failure_naming_different_columns_counts_as_same():
    from check import _gist

    a = (
        'AssertionError: Column "$.snpeff_effect" does not exist in node "fileset-1". '
        "Existing columns: Index([a])"
    )
    b = (
        'AssertionError: Column "$.category" does not exist in node "fileset-1". '
        "Existing columns: Index([b, c])"
    )
    assert _gist(a) == _gist(b)
    assert _gist(a) != _gist("json.decoder.JSONDecodeError: Extra data: line 2 column 1")


def test_round7_sampling_fixes():
    import pytest
    import sample

    # URLs with '=' map with =>
    sample.URL_MAP.clear()
    sample.parse_url_maps(["https://q.example/?query=SELECT+x=>file:///tmp/q/"])
    assert sample.URL_MAP == [("https://q.example/?query=SELECT+x", "file:///tmp/q/")]
    sample.URL_MAP.clear()
    with pytest.raises(SystemExit, match="OLD=>NEW"):
        sample.parse_url_maps(["https://q.example/?query=SELECT=https://other/"])
    # JSON where a table is declared
    with pytest.raises(ValueError, match="the file is JSON"):
        table_rows(
            b'{ "head": { "vars": ["s"] } }\n',
            {"contentUrl": "https://q.example/?query=x"},
            ["s"],
            5,
        )
    # IRIs grouped by namespace, blank nodes named
    assert (
        sample._prefix_of("http://purl.obolibrary.org/obo/DOID_4")
        == "http://purl.obolibrary.org/obo/"
    )
    assert sample._prefix_of("_:b12") == "_: (blank node)"
    # separators inside brackets aren't lists
    assert "list" not in shape(
        [
            "Abl1 [Source:MGI Symbol;Acc:MGI:87859]",
            "Akt1 [Source:MGI Symbol;Acc:MGI:87986]",
            "Apoe [Source:MGI Symbol;Acc:MGI:88057]",
        ]
    )


def test_round7_check_fixes():
    import time

    import check
    import pytest

    a = (
        'AssertionError: Column ""details on how the code was determined ""this variant"" '
        'does not exist in node "x"'
    )
    b = 'AssertionError: Column "$.other" does not exist in node "y"'
    assert check._gist(a) == check._gist(b)
    assert "atomic" in check._hint("ValueError: could not convert string to float: '-'")
    t = time.time()
    with pytest.raises(check.subprocess.TimeoutExpired):
        check.run_bounded(["sleep", "30"], 3)
    assert time.time() - t < 10


def test_validate_timeout_or_memory_cap_is_an_error_not_a_warning(monkeypatch):
    """A croissant whose structural validity couldn't be checked must fail the run (exit 1), not
    pass silently: check.py's exit code only looks at errors, not warnings."""
    import subprocess

    import check

    monkeypatch.setattr(
        check,
        "run_bounded",
        lambda cmd, timeout: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd, timeout)),
    )
    f = Findings()
    check._run_validate("croissant.json", f, 60)
    assert f.count("error") == 1
    assert "unverified" in check.VALIDATION["result"]

    monkeypatch.setattr(
        check,
        "run_bounded",
        lambda cmd, timeout: (_ for _ in ()).throw(check.MemoryExceeded("used 5.0 GB")),
    )
    f = Findings()
    check._run_validate("croissant.json", f, 60)
    assert f.count("error") == 1
    assert "unverified" in check.VALIDATION["result"]


def test_sampling_stops_at_the_first_refusal():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    import sample

    hits = []

    class Refuse(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server's name
            hits.append(self.path)
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Refuse)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    croissant = {
        "distribution": [{"@id": f"f{i}", "contentUrl": f"{base}/{i}.csv"} for i in range(5)],
        "recordSet": [
            {
                "@id": f"rs{i}",
                "name": f"rs{i}",
                "field": [
                    {
                        "name": "a",
                        "source": {"fileObject": {"@id": f"f{i}"}, "extract": {"column": "a"}},
                    }
                ],
            }
            for i in range(5)
        ],
    }
    sample.USE_CACHE = False
    try:
        got, errors = sample.sample_rows(croissant, croissant["recordSet"], 5, 10_000, k=3)
    finally:
        sample.USE_CACHE = True
        server.shutdown()
    assert not got and len(hits) == 1 and "login or key" in errors[0]


def test_a_bot_challenge_holds_the_host_across_runs(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    import pytest
    import sample

    hits = []

    class Challenge(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server's name
            hits.append(self.path)
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Challenge)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    cache_dir = sample.CACHE_DIR
    sample.CACHE_DIR = tmp_path / "downloads"
    try:
        with pytest.raises(sample.BotCheck, match="bot challenge"):
            sample.fetch_bytes(f"{base}/a.tsv", 1000, whole=False)
        # a later run (the hold is on disk) and another file: refused without a request
        sample._last_request.clear()
        with pytest.raises(sample.BotCheck, match="no requests go there"):
            sample.fetch_bytes(f"{base}/b.tsv", 1000, whole=False)
        assert hits == ["/a.tsv"]
    finally:
        sample.CACHE_DIR = cache_dir
        server.shutdown()


def test_load_errors_compare_by_root_exception():
    from check import _gist

    new = (
        "ValueError: invalid literal for int() with base 10: '21966275.0' | "
        "mlcroissant._src.core.issues.GenerationError: An error occured during the sequential "
        "generation of the dataset, more specifically during the operation ReadFields(rs1)"
    )
    same = (
        "ValueError: invalid literal for int() with base 10: '21966275.0' | "
        "mlcroissant._src.core.issues.GenerationError: An error occurred during the streaming "
        "generation of the dataset, more specifically during the operation ReadFields(rs1)"
    )
    other = "KeyError: 'pmid' | mlcroissant._src.core.issues.GenerationError: An error occured"
    assert _gist(new) == _gist(same)
    assert _gist(new) != _gist(other)
    wrapped_a = "mlcroissant._src.core.issues.GenerationError: An error occured during the sequential generation"  # noqa: E501
    wrapped_b = "mlcroissant._src.core.issues.GenerationError: An error occurred during the streaming generation"  # noqa: E501
    assert _gist(wrapped_a) == _gist(wrapped_b)


def test_code_merge_warning_accepts_a_knowledge_level_field():
    from check import Findings, check_code_merges

    def plan(kl_codes):
        return {
            "layouts": [
                {
                    "id": "dis",
                    "columns": {
                        "AssociationType": {
                            "property": "biolink:predicate",
                            "codes": {
                                "is_implicated_in": "biolink:contributes_to",
                                "implicated_via_orthology": "biolink:contributes_to",
                            },
                            "derived": [
                                {
                                    "name": "knowledge_level",
                                    "property": "biolink:knowledge_level",
                                    "codes": kl_codes,
                                }
                            ],
                        }
                    },
                }
            ]
        }

    told = {
        "implicated_via_orthology": "biolink:prediction",
        "is_implicated_in": "biolink:knowledge_assertion",
    }
    f = Findings()
    check_code_merges(plan(told), f)
    assert f.count("warning") == 0
    same = dict.fromkeys(told, "biolink:knowledge_assertion")
    f = Findings()
    check_code_merges(plan(same), f)
    assert f.count("warning") == 1


def test_profile_splits_lists_and_stops_early(tmp_path, capsys):
    import sample

    path = tmp_path / "sets.tsv"
    path.write_text("set\tgenes\n" + "".join(f"s{i}\tA|B|C\n" for i in range(1000)))
    croissant = {
        "distribution": [{"@id": "f", "contentUrl": f"file://{path}"}],
        "recordSet": [
            {
                "@id": "rs",
                "field": [
                    {"name": n, "source": {"fileObject": {"@id": "f"}, "extract": {"column": n}}}
                    for n in ("set", "genes")
                ],
            }
        ],
    }
    sample.profile(croissant, croissant["recordSet"], ["genes"], 5, None, 10**6, split="|")
    out = capsys.readouterr().out
    assert "Profiled 1000 rows" in out and "1000  'B'" in out
    sample.profile(croissant, croissant["recordSet"], ["genes"], 5, None, 10**6, stop_mb=0.001)
    out = capsys.readouterr().out
    assert "stopped after 0.001 MB" in out and "Profiled 1000 rows" not in out


def test_profile_closes_the_local_file_handle(tmp_path, monkeypatch, capsys):
    """_profile_one's local file:// branch must close its own file handle, not just the HTTP
    response it tracks for the remote branch, or profiling many local files leaks a descriptor
    per call."""
    import sample

    path = tmp_path / "data.tsv"
    path.write_text("a\tb\n1\t2\n")
    opened = []
    real_open = Path.open

    def tracking_open(self, *args, **kwargs):
        fh = real_open(self, *args, **kwargs)
        opened.append(fh)
        return fh

    monkeypatch.setattr(Path, "open", tracking_open)
    croissant = {
        "distribution": [{"@id": "f", "contentUrl": f"file://{path}"}],
        "recordSet": [
            {
                "@id": "rs",
                "field": [
                    {"name": n, "source": {"fileObject": {"@id": "f"}, "extract": {"column": n}}}
                    for n in ("a", "b")
                ],
            }
        ],
    }
    sample.profile(croissant, croissant["recordSet"], ["b"], 5, None, 10**6)
    capsys.readouterr()
    assert opened and all(fh.closed for fh in opened)


def test_reduced_croissant_keeps_one_record_set_per_layout_and_its_references():
    from check import reduced_croissant

    def rs(i, cols, file, refs=None):
        fields = [
            {"@id": f"{i}/{c}", "name": c, "source": {"fileObject": {"@id": file}}} for c in cols
        ]
        if refs:
            fields[0]["references"] = {"field": {"@id": refs}}
        return {"@id": i, "field": fields}

    out = {
        "distribution": [{"@id": f"f{i}", "contentUrl": f"http://x/{i}.tsv"} for i in range(4)]
        + [{"@id": "codes_file", "contentUrl": "http://x/c.csv"}],
        "recordSet": [
            rs("a1", ["gene", "disease"], "f0", refs="codes/key"),
            rs("a2", ["gene", "disease"], "f1", refs="codes/key"),
            rs("a3", ["gene", "disease"], "f2", refs="codes/key"),
            rs("b1", ["chem"], "f3"),
            rs("codes", ["key", "value"], "codes_file"),
            rs("codes2", ["key", "value"], "codes_file"),
        ],
    }
    out["recordSet"][3] = rs("b1", ["chem"], "f3", refs="codes2/key")
    small = reduced_croissant(out)
    # codes2 has the same columns as codes, but b1 references it
    assert [r["@id"] for r in small["recordSet"]] == ["a1", "b1", "codes", "codes2"]
    assert [d["@id"] for d in small["distribution"]] == ["f0", "f3", "codes_file"]


def test_dump_prints_archive_members_and_sheets(tmp_path, capsys):
    import zipfile as zf

    import openpyxl
    import pytest
    import sample

    with zf.ZipFile(tmp_path / "docs.zip", "w") as z:
        z.writestr("release/README.txt", "Column 'Genus ' is the genus.\n")
    book = openpyxl.Workbook()
    book.active.title = "VMR"
    book.active.append(["Genus ", "Species"])
    book.active.append(["Alphavirus", "Sindbis virus"])
    book.save(tmp_path / "vmr.xlsx")
    croissant = {
        "distribution": [
            {"@id": "docs", "contentUrl": "http://kb.test/docs.zip"},
            {"@id": "vmr", "contentUrl": "http://kb.test/vmr.xlsx"},
        ]
    }
    sample.URL_MAP.append(("http://kb.test/", f"file://{tmp_path}/"))
    try:
        sample.dump(croissant, "docs", "README.txt", None, 10**6)
        assert "is the genus" in capsys.readouterr().out
        with pytest.raises(SystemExit, match="release/README.txt"):
            sample.dump(croissant, "docs", None, None, 10**6)
        sample.dump(croissant, "vmr", None, None, 10**6)
        assert "Sheets: VMR" in capsys.readouterr().out
        sample.dump(croissant, "vmr", None, "VMR", 10**6)
        assert "Alphavirus\tSindbis virus" in capsys.readouterr().out
    finally:
        sample.URL_MAP.clear()


def test_run_bounded_stops_a_process_over_the_memory_cap(monkeypatch):
    import sys

    import check
    import pytest

    monkeypatch.setattr(check, "MEMORY_CAP_MB", 200)
    grow = "b = []\nwhile True:\n    b.append(bytearray(50_000_000))\n"
    with pytest.raises(check.MemoryExceeded, match="over --memory-gb"):
        check.run_bounded([sys.executable, "-c", grow], timeout=60)
    small = check.run_bounded([sys.executable, "-c", "print('ok')"], timeout=60)
    assert small.stdout.strip() == "ok"


def test_explained_matching_merged_places_specific_entries_and_errors():
    from check import Findings

    f = Findings(
        [
            {"where": "x column_6", "message": "unmapped", "reason": "general"},
            {"where": "x column_6 -> knowledge_level", "message": "unmapped", "reason": "derived"},
            {"where": "a layout, b layout", "message": "prefix", "reason": "merged copy"},
            {"where": "s1 matrix", "message": "pattern", "reason": "wrong sheet", "level": "error"},
        ]
    )
    f.warn("x column_6 -> knowledge_level", "values with no code mapping: unmapped")
    f.warn("b layout id", "prefix not listed")
    f.error("s1 matrix id", "values don't fit the pattern")
    f.error("other", "values don't fit the pattern")
    notes = [m for lv, _, m in f.items if lv == "note"]
    assert any("derived" in m for m in notes)
    assert any("merged copy" in m for m in notes)
    assert any("wrong sheet" in m for m in notes)
    assert f.count("error") == 1 and f.count("warning") == 0


def test_enum_codes_must_be_enum_values():
    from check import Findings, check_enum_codes

    model = {
        "classes": {},
        "slots": {"knowledge level": {"range": "KnowledgeLevelEnum"}},
        "enums": {"KnowledgeLevelEnum": {"permissible_values": {"prediction": {}}}},
    }
    plan = {
        "layouts": [
            {
                "id": "l",
                "columns": {
                    "ev": {
                        "property": "biolink:knowledge_level",
                        "codes": {"IEA": "biolink:prediction", "X": "biolink:guess"},
                    }
                },
            }
        ]
    }
    f = Findings()
    check_enum_codes(plan, f, model)
    assert f.count("error") == 1 and "biolink:guess" in f.items[0][2]


def test_tar_members_read_through_double_gzip_and_bz2(tmp_path):
    import bz2
    import gzip as gz
    import io
    import tarfile

    import sample

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        data = b"a\tb\n1\t2\n"
        info = tarfile.TarInfo("dir/table.tsv")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    (tmp_path / "twice.tar.gz").write_bytes(gz.compress(gz.compress(buf.getvalue())))
    (tmp_path / "once.tar.bz2").write_bytes(bz2.compress(buf.getvalue()))
    for name in ("twice.tar.gz", "once.tar.bz2"):
        got = sample.tar_member(f"file://{tmp_path / name}", "table.tsv", 1000)
        assert got == b"a\tb\n1\t2\n"


def test_a_larger_cached_copy_serves_a_smaller_request(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    import sample

    hits = []
    body = b"x" * 5000

    class Plain(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server's name
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Plain)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cache_dir, indexed = sample.CACHE_DIR, sample._INDEXED
    sample.CACHE_DIR, sample._INDEXED = tmp_path / "downloads", None
    url = f"http://127.0.0.1:{server.server_port}/f.tsv"
    try:
        assert sample.fetch_bytes(url, 10_000, whole=False) == body
        assert sample.fetch_bytes(url, 1000, whole=False) == body[:1000]
        assert sample.fetch_bytes(url, 8000, whole=True) == body  # the copy is the whole file
        assert hits == ["/f.tsv"]
    finally:
        sample.CACHE_DIR, sample._INDEXED = cache_dir, indexed
        server.shutdown()


def test_parquet_rows_and_first_member_of_a_zipped_fileset(tmp_path):
    import io
    import zipfile as zf

    import pyarrow as pa
    import pyarrow.parquet as pq
    import sample

    buf = io.BytesIO()
    pq.write_table(pa.table({"gene": ["MGI:1", "MGI:2"], "tags": [["a"], ["b", "c"]]}), buf)
    with zf.ZipFile(tmp_path / "export.zip", "w") as z:
        z.writestr("export/part-0001-abc.snappy.parquet", buf.getvalue())
        z.writestr("export/_SUCCESS", "")
    croissant = {
        "distribution": [
            {"@id": "zip", "@type": "cr:FileObject", "contentUrl": "http://kb.test/export.zip"},
            {
                "@id": "parts",
                "@type": "cr:FileSet",
                "containedIn": {"@id": "zip"},
                "includes": "export/part-*.parquet",
                "encodingFormat": "application/x-parquet",
            },
        ],
        "recordSet": [
            {
                "@id": "rs",
                "field": [
                    {"name": n, "source": {"fileSet": {"@id": "parts"}, "extract": {"column": n}}}
                    for n in ("gene", "tags")
                ],
            }
        ],
    }
    sample.URL_MAP.append(("http://kb.test/", f"file://{tmp_path}/"))
    try:
        got, errors = sample.sample_rows(croissant, croissant["recordSet"], 5, 10**6, k=1)
    finally:
        sample.URL_MAP.clear()
    assert not errors
    assert got[0][2] == [{"gene": "MGI:1", "tags": ["a"]}, {"gene": "MGI:2", "tags": ["b", "c"]}]


def test_profile_per_file_counts_each_record_set_separately(tmp_path, capsys):
    import argparse

    import sample

    for i in (1, 2):
        (tmp_path / f"f{i}.tsv").write_text("id\nA\nB\n")
    croissant = {
        "distribution": [
            {"@id": f"f{i}", "contentUrl": f"file://{tmp_path}/f{i}.tsv"} for i in (1, 2)
        ],
        "recordSet": [
            {
                "@id": f"rs{i}",
                "name": f"rs{i}",
                "field": [
                    {
                        "name": "id",
                        "source": {"fileObject": {"@id": f"f{i}"}, "extract": {"column": "id"}},
                    }
                ],
            }
            for i in (1, 2)
        ],
    }
    args = argparse.Namespace(
        profile="id", top=5, max_mb=1, by=None, split=None, stop_mb=None, prefixes=None, shapes=None
    )
    for rs in croissant["recordSet"]:
        sample._run_profile(croissant, [rs], args, None)
    out = capsys.readouterr().out
    assert out.count("        1  'A'") == 2  # once per file, not 2 for the files together


def test_load_failure_causes():
    from check import _load_cause

    hash_header = (
        "ValueError: Column \"taxon_id\" does not exist. Possible fields: '#taxon_id', "
        "'STRING_name_compact'"
    )
    assert "starts with '#'" in _load_cause(hash_header)
    spaces = "ValueError: Column \"group1\" does not exist. Possible fields: 'group1 group2 score'"
    assert "one column" in _load_cause(spaces)
    no_header = 'ValueError: Column "Disease" does not exist. Possible fields: Index([1, 3, 2])'
    assert "no header row" in _load_cause(no_header)
    positional = 'ValueError: Column "column_1" does not exist in node'
    assert "column_1" in _load_cause(positional)
