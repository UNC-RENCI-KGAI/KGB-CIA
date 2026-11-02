"""Step 6: applying cached labels to the toy graph."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
from pubmed_evidence import apply, config, graphio, records
from pubmed_evidence.cache import LabelCache, LabelRow, ProposalRow, ReviewRow
from toy import ABL1, EDGES, IMATINIB

from shared.schema import Edge

MODEL, PROMPT = "bulk-model", "label@00000000"
REVIEWER, REVIEW_PROMPT = "review-model", "review@00000000"

TREATS = graphio.edge_statement_id(EDGES[0])  # made by EDGES[0] and EDGES[1]
DPI = graphio.edge_statement_id(EDGES[2])
AFFECTS = graphio.edge_statement_id(EDGES[3])  # causes decreased activity
NEGATED = graphio.edge_statement_id(EDGES[-1])
AFFECTS_QUALIFIERS = {
    "object_aspect_qualifier": "activity",
    "object_direction_qualifier": "decreased",
    "qualified_predicate": "biolink:causes",
}


def label(sid: str, pmid: str, value: str = "confirmed", quote_valid: bool | None = True):
    return LabelRow(sid, pmid, PROMPT, MODEL, value, "a quote from the abstract", quote_valid)


def proposal(sid: str, pmid: str, qualifiers: dict, quote_valid=True, valid=True):
    return ProposalRow(sid, pmid, PROMPT, MODEL, qualifiers, "a quote", quote_valid, valid)


def review(kind: str, sid: str, pmid: str, verdict: str = "accept", qualifiers=None):
    return ReviewRow(
        kind, sid, pmid, PROMPT, MODEL, qualifiers, REVIEWER, REVIEW_PROMPT, verdict, "reason"
    )


def fill(tmp_path: Path, labels=(), proposals=(), reviews=(), reviewer: bool = True) -> None:
    with LabelCache(tmp_path / "cache" / config.LABEL_CACHE) as cache:
        cache.put_labels(labels)
        cache.put_proposals(proposals)
        cache.put_reviews(reviews)
    records.write_label_run(
        tmp_path / "work",
        records.LabelRun(
            model=MODEL,
            prompt_version=PROMPT,
            reviewer=REVIEWER if reviewer else None,
            review_prompt_version=REVIEW_PROMPT if reviewer else None,
        ),
    )


def run_apply(graph: Path, tmp_path: Path, out: str = "out", **kw) -> tuple[dict, list[dict]]:
    report = apply.apply_labels(graph, tmp_path / out, tmp_path / "cache", tmp_path / "work", **kw)
    return report, list(graphio.read_jsonl(tmp_path / out / graphio.EDGES))


def dump(edge: Edge) -> dict:
    return json.loads(edge.model_dump_json(exclude_none=True))


def new_edges(edges: list[dict]) -> list[dict]:
    return edges[len(EDGES) :]


def args_for(graph: Path, tmp_path: Path, *argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    apply.add_arguments(parser)
    args = parser.parse_args(list(argv))
    args.input, args.output = graph, tmp_path / "out"
    args.work, args.cache_dir = tmp_path / "work", tmp_path / "cache"
    return args


def test_confirmed_pmids_join_both_duplicate_edges_and_nothing_else_changes(toy_graph, tmp_path):
    fill(tmp_path, [label(TREATS, "9000001"), label(TREATS, "11287972"), label(TREATS, "123")])
    report, edges = run_apply(toy_graph, tmp_path)
    assert len(edges) == len(EDGES)
    assert edges[0]["publications"] == ["PMID:11287972", "PMID:123", "PMID:9000001"]
    assert edges[1]["publications"] == ["PMID:123", "PMID:9000001", "PMID:11287972"]
    expected = [dump(e) for e in EDGES]
    for i in (0, 1):
        edges[i].pop("publications")
        expected[i].pop("publications")
    assert edges == expected
    assert all(e["kgbcia_source_records"] for e in edges)
    assert report["edges_given_pmids"] == 2
    assert report["pmids_added"] == 5
    assert report["statements_confirmed"] == 1
    nodes = (tmp_path / "out" / graphio.NODES).read_text().splitlines()
    assert nodes == (toy_graph / graphio.NODES).read_text().splitlines()


def test_labels_with_invalid_quotes_are_ignored(toy_graph, tmp_path):
    fill(
        tmp_path,
        [
            label(DPI, "1", quote_valid=False),
            label(DPI, "2", quote_valid=None),
            label(AFFECTS, "3", "contradicted", quote_valid=False),
        ],
        [proposal(AFFECTS, "4", {"object_direction_qualifier": "increased"}, quote_valid=False)],
        [review("contradicted", AFFECTS, "3")],
    )
    report, edges = run_apply(toy_graph, tmp_path)
    assert edges == [dump(e) for e in EDGES]
    assert report["skipped"]["invalid_quote"] == {"confirmed": 2, "contradicted": 1, "proposal": 1}
    assert report["edges_given_pmids"] == 0


def test_accepted_contradictions_make_one_negated_edge(toy_graph, tmp_path):
    fill(
        tmp_path,
        [
            label(AFFECTS, "222", "contradicted"),
            label(AFFECTS, "111", "contradicted"),
            label(AFFECTS, "333", "contradicted"),  # not reviewed
            label(AFFECTS, "444", "contradicted"),  # rejected
            label(AFFECTS, "555", "ambiguous", quote_valid=None),
        ],
        reviews=[
            review("contradicted", AFFECTS, "111"),
            review("contradicted", AFFECTS, "222"),
            review("contradicted", AFFECTS, "444", "reject"),
        ],
    )
    report, edges = run_apply(toy_graph, tmp_path)
    [negated] = new_edges(edges)
    assert negated == {
        "id": "kgbcia-augmentation:"
        + graphio.statement_id(IMATINIB, "biolink:affects", ABL1, AFFECTS_QUALIFIERS, True),
        "subject": IMATINIB,
        "predicate": "biolink:affects",
        "object": ABL1,
        "primary_knowledge_source": config.INFORES,
        "aggregator_knowledge_source": [],
        "knowledge_level": "knowledge_assertion",
        "agent_type": "text_mining_agent",
        "publications": ["PMID:111", "PMID:222"],
        **AFFECTS_QUALIFIERS,
        "negated": True,
    }
    assert edges[3] == dump(EDGES[3])  # the original is untouched
    assert report["negated_edges_added"] == 1
    assert report["skipped"]["unreviewed"]["contradicted"] == 1
    assert report["skipped"]["rejected"]["contradicted"] == 1


def test_contradictions_wait_for_a_review(toy_graph, tmp_path, capsys):
    fill(
        tmp_path,
        [label(TREATS, "7"), label(AFFECTS, "8", "contradicted")],
        [proposal(AFFECTS, "9", {"object_direction_qualifier": "increased"})],
        reviewer=False,
    )
    report, edges = run_apply(toy_graph, tmp_path)
    assert len(edges) == len(EDGES)
    assert edges[0]["publications"][-1] == "PMID:7"
    assert report["skipped"]["unreviewed"] == {"contradicted": 1, "proposal": 1}
    assert "1 contradicted labels and 1 qualifier proposals await review" in capsys.readouterr().err


def test_proposals_make_merged_qualified_edges(toy_graph, tmp_path):
    increased = {"object_direction_qualifier": "increased"}
    increased_causes = {**increased, "qualified_predicate": "biolink:causes"}
    decreased = {"object_direction_qualifier": "decreased"}
    mechanism = {"causal_mechanism_qualifier": "inhibition"}
    fill(
        tmp_path,
        proposals=[
            proposal(AFFECTS, "2", increased),
            proposal(AFFECTS, "1", increased_causes),  # merges to the same set
            proposal(AFFECTS, "3", decreased),  # the original's own qualifiers
            proposal(AFFECTS, "4", mechanism),  # rejected
            proposal(AFFECTS, "5", {"object_aspect_qualifier": "spin"}, valid=False),
            proposal(DPI, "6", mechanism),
        ],
        reviews=[
            review("qualifier", AFFECTS, "2", qualifiers=increased),
            review("qualifier", AFFECTS, "1", qualifiers=increased_causes),
            review("qualifier", AFFECTS, "3", qualifiers=decreased),
            review("qualifier", AFFECTS, "4", "reject", qualifiers=mechanism),
            review("qualifier", DPI, "6", qualifiers=mechanism),
        ],
    )
    report, edges = run_apply(toy_graph, tmp_path)
    added = new_edges(edges)
    assert len(added) == 2
    assert [e["id"] for e in added] == sorted(e["id"] for e in added)
    by_predicate = {e["predicate"]: e for e in added}
    up = by_predicate["biolink:affects"]
    assert graphio.qualifiers_of(Edge.model_validate(up)) == {
        **AFFECTS_QUALIFIERS,
        "object_direction_qualifier": "increased",
    }
    assert up["publications"] == ["PMID:1", "PMID:2"]
    assert "negated" not in up
    dpi = by_predicate["biolink:directly_physically_interacts_with"]
    assert dpi["causal_mechanism_qualifier"] == "inhibition"
    assert dpi["publications"] == ["PMID:6"]
    assert report["qualified_edges_added"] == 2
    assert report["skipped"]["same_as_original"] == 1
    assert report["skipped"]["rejected"]["proposal"] == 1
    assert report["skipped"]["invalid_qualifiers"] == 1
    assert edges[: len(EDGES)] == [dump(e) for e in EDGES]


def test_proposal_already_in_graph_is_not_duplicated(toy_graph, tmp_path):
    # An unqualified affects edge whose proposal merges to EDGES[3]'s statement.
    plain = (
        EDGES[3]
        .model_copy(update={"primary_knowledge_source": "infores:ctd"})
        .model_dump(exclude_none=True)
    )
    for slot in AFFECTS_QUALIFIERS:
        plain.pop(slot)
    graph = tmp_path / "graph"
    graphio.write_jsonl(graph / graphio.NODES, graphio.read_nodes(toy_graph))
    graphio.write_jsonl(graph / graphio.EDGES, [*EDGES, Edge.model_validate(plain)])
    plain_sid = graphio.edge_statement_id(Edge.model_validate(plain))
    fill(
        tmp_path,
        proposals=[proposal(plain_sid, "1", AFFECTS_QUALIFIERS)],
        reviews=[review("qualifier", plain_sid, "1", qualifiers=AFFECTS_QUALIFIERS)],
    )
    report, edges = run_apply(graph, tmp_path)
    assert len(edges) == len(EDGES) + 1
    assert report["qualified_edges_added"] == 0
    assert report["skipped"]["already_in_graph"] == 1


def test_no_review_applies_unreviewed_findings(toy_graph, tmp_path, capsys):
    increased = {"object_direction_qualifier": "increased"}
    fill(
        tmp_path,
        [label(AFFECTS, "8", "contradicted"), label(AFFECTS, "9", "contradicted")],
        [proposal(AFFECTS, "10", increased)],
        [review("contradicted", AFFECTS, "9", "reject")],
    )
    args = args_for(toy_graph, tmp_path, "--no-review")
    assert apply.run(args) == 0
    out = capsys.readouterr().out
    assert "--no-review" in out
    edges = list(graphio.read_jsonl(tmp_path / "out" / graphio.EDGES))
    negated, qualified = new_edges(edges)
    assert negated["negated"] is True
    assert negated["publications"] == ["PMID:8"]  # the rejection still holds
    assert qualified["object_direction_qualifier"] == "increased"
    report = json.loads((tmp_path / "work" / apply.APPLY_REPORT).read_text())
    assert report["run"]["require_review"] is False
    assert report["skipped"]["rejected"]["contradicted"] == 1


def test_negated_input_edges_and_orphans_are_left_alone(toy_graph, tmp_path):
    fill(tmp_path, [label(NEGATED, "1"), label("ffffffffffffffff", "2")])
    report, edges = run_apply(toy_graph, tmp_path)
    assert edges == [dump(e) for e in EDGES]
    assert edges[-1]["negated"] is True
    assert report["negated_in_input"] == 1
    assert report["skipped"]["orphan_statements"] == 2


def test_new_edges_are_valid_and_output_is_deterministic(toy_graph, tmp_path):
    fill(
        tmp_path,
        [
            label(TREATS, "5"),
            label(DPI, "6"),
            label(AFFECTS, "7", "contradicted"),
            label(TREATS, "8", "contradicted"),
        ],
        [proposal(AFFECTS, "9", {"object_direction_qualifier": "increased"})],
    )
    kw = {"require_review": False, "infores": "infores:kgbcia-test"}
    report, edges = run_apply(toy_graph, tmp_path, **kw)
    added = new_edges(edges)
    assert len(added) == report["negated_edges_added"] + report["qualified_edges_added"] == 3
    for d in added:
        edge = Edge.model_validate(d)
        assert edge.id == apply.ID_PREFIX + graphio.edge_statement_id(edge)
        assert edge.primary_knowledge_source == "infores:kgbcia-test"
        assert edge.aggregator_knowledge_source == []
        assert edge.knowledge_level == config.NEW_EDGE_KNOWLEDGE_LEVEL
        assert edge.agent_type == config.NEW_EDGE_AGENT_TYPE
        assert "kgbcia_source_records" not in d
    assert [e["id"] for e in added[:2]] == sorted(e["id"] for e in added[:2])  # negated first
    first = {n: (tmp_path / "out" / n).read_bytes() for n in (graphio.NODES, graphio.EDGES)}
    run_apply(toy_graph, tmp_path, **kw)  # again, into the same dir
    run_apply(toy_graph, tmp_path, "out2", **kw)
    for name, data in first.items():
        assert (tmp_path / "out" / name).read_bytes() == data
        assert (tmp_path / "out2" / name).read_bytes() == data
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [graphio.EDGES, graphio.NODES]


def test_cli_flags_override_label_run(toy_graph, tmp_path):
    fill(tmp_path, [label(TREATS, "5")])
    records.write_label_run(
        tmp_path / "work", records.LabelRun(model="other", prompt_version="label@ffffffff")
    )
    report, edges = run_apply(toy_graph, tmp_path, model=MODEL, prompt_version=PROMPT)
    assert report["run"]["model"] == MODEL
    assert edges[1]["publications"] == ["PMID:5"]


def test_failed_run_leaves_no_output(toy_graph, tmp_path, monkeypatch):
    fill(tmp_path, [label(AFFECTS, "7", "contradicted")], reviewer=False)

    def broken(*args, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(apply, "new_edge", broken)
    with pytest.raises(RuntimeError):
        run_apply(toy_graph, tmp_path, require_review=False)
    assert list((tmp_path / "out").iterdir()) == []


def test_missing_label_run_fails(toy_graph, tmp_path, capsys):
    assert apply.run(args_for(toy_graph, tmp_path)) == 1
    assert "no label run" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_missing_label_cache_fails(toy_graph, tmp_path, capsys):
    records.write_label_run(tmp_path / "work", records.LabelRun(model=MODEL, prompt_version=PROMPT))
    assert apply.run(args_for(toy_graph, tmp_path)) == 1
    assert "no label cache" in capsys.readouterr().err


def test_output_dir_must_not_be_input_dir(toy_graph, tmp_path, capsys):
    fill(tmp_path, [label(TREATS, "5")])
    args = args_for(toy_graph, tmp_path)
    args.output = toy_graph / "." / ".." / toy_graph.name
    before = (toy_graph / graphio.EDGES).read_bytes()
    assert apply.run(args) == 1
    assert "is the input dir" in capsys.readouterr().err
    assert (toy_graph / graphio.EDGES).read_bytes() == before


def test_refeeding_the_output_adds_nothing(toy_graph, tmp_path):
    """This stage's own output as input: no new edge twice, no PMID twice."""
    fill(
        tmp_path,
        [label(TREATS, "5"), label(AFFECTS, "7", "contradicted")],
        [proposal(AFFECTS, "9", {"object_direction_qualifier": "increased"})],
    )
    first, _ = run_apply(toy_graph, tmp_path, require_review=False)
    assert first["negated_edges_added"] == first["qualified_edges_added"] == 1
    again, _ = run_apply(tmp_path / "out", tmp_path, "out2", require_review=False)
    assert again["negated_edges_added"] == again["qualified_edges_added"] == 0
    assert again["pmids_added"] == 0
    assert again["skipped"]["already_in_graph"] == 2
    for name in (graphio.NODES, graphio.EDGES):
        assert (tmp_path / "out2" / name).read_bytes() == (tmp_path / "out" / name).read_bytes()


def test_records_are_written_back_as_read(tmp_path):
    """Slots apply doesn't touch keep their exact values: no defaults added, no nulls dropped,
    key order kept; only `publications` changes."""
    graph = tmp_path / "graph"
    graph.mkdir()
    nodes = [
        '{"id":"CHEBI:45783","category":["biolink:SmallMolecule"],"name":"imatinib"}',
        '{"id":"MONDO:0011996","category":["biolink:Disease"],"extra":null}',
    ]
    edges = [
        '{"subject":"CHEBI:45783","predicate":"biolink:treats","object":"MONDO:0011996",'
        '"primary_knowledge_source":"infores:drugcentral","knowledge_level":"knowledge_assertion",'
        '"agent_type":"manual_agent","p_value":null,"original_subject":null,"score":NaN,'
        '"publications":["PMID:1"],"z_last":true}',
        '{"subject":"MONDO:0011996","predicate":"biolink:has_phenotype","object":"CHEBI:45783",'
        '"primary_knowledge_source":"infores:x","knowledge_level":"not_provided",'
        '"agent_type":"not_provided","p_value":null}',
    ]
    (graph / graphio.NODES).write_text("\n".join(nodes) + "\n")
    (graph / graphio.EDGES).write_text("\n".join(edges) + "\n")
    treats = graphio.statement_id("CHEBI:45783", "biolink:treats", "MONDO:0011996")
    fill(tmp_path, [label(treats, "2")])
    run_apply(graph, tmp_path)
    assert (tmp_path / "out" / graphio.NODES).read_text().splitlines() == nodes
    out = (tmp_path / "out" / graphio.EDGES).read_text().splitlines()
    assert out[0] == edges[0].replace('["PMID:1"]', '["PMID:1","PMID:2"]')
    assert out[1] == edges[1]


def test_only_statements_in_scope_are_applied(toy_graph, tmp_path, capsys):
    fill(tmp_path, [label(TREATS, "5"), label(DPI, "6"), label(AFFECTS, "7", "contradicted")])
    graphio.write_jsonl(tmp_path / "work" / records.STATEMENTS, [{"statement_id": DPI}])
    report, edges = run_apply(toy_graph, tmp_path, require_review=False)
    assert report["pmids_added"] == 1 and edges[2]["publications"][-1] == "PMID:6"
    assert report["negated_edges_added"] == 0
    assert report["skipped"]["out_of_scope"] == 2
    assert records.STATEMENTS not in capsys.readouterr().err  # it was there: no warning


def test_without_statements_every_label_applies_with_a_warning(toy_graph, tmp_path, capsys):
    fill(tmp_path, [label(TREATS, "5")])
    report, _ = run_apply(toy_graph, tmp_path)
    assert report["pmids_added"] == 2
    assert f"no {tmp_path / 'work' / records.STATEMENTS}" in capsys.readouterr().err


def test_a_run_without_labels_fails(toy_graph, tmp_path, capsys):
    fill(tmp_path, [label(TREATS, "5")])
    args = args_for(toy_graph, tmp_path, "--model", "other-model")
    assert apply.run(args) == 1
    assert "no labels for other-model" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_an_invalid_edge_fails_loudly(toy_graph, tmp_path, capsys):
    fill(tmp_path, [label(TREATS, "5")])
    with (toy_graph / graphio.EDGES).open("a") as f:
        f.write('{"subject":"A:1","predicate":"treats","object":"B:2"}\n')
    assert apply.run(args_for(toy_graph, tmp_path)) == 1
    err = capsys.readouterr().err
    assert f"edge {len(EDGES) + 1} of" in err and "not a valid Edge" in err
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == []
