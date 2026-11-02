"""The second pass: which items go to the reviewer, how they're grouped, and what is stored.

Labels, proposals, work files and abstracts are built in code; the model is a fake client.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from pathlib import Path

import pytest
from pubmed_evidence import config, llm, records, review
from pubmed_evidence.cache import Abstract, LabelCache, LabelRow, ProposalRow, PubMedCache
from pubmed_evidence.graphio import pair_id, statement_id
from pubmed_evidence.records import Entity, LabelRun, PubmedRecord, SentenceRecord, Statement
from toy import ABL1, CML, IMATINIB

MODEL = "gpt-5.6-luna"
PV = "label@11111111"
REVIEWER = "gpt-5.6-terra"

INHIBITS = {
    "qualified_predicate": "biolink:causes",
    "object_aspect_qualifier": "activity",
    "object_direction_qualifier": "decreased",
}
EXPRESSION = {"object_aspect_qualifier": "expression"}
CONTRIBUTES = {"qualified_predicate": "biolink:contributes_to"}
TREATS = statement_id(IMATINIB, "biolink:treats", CML)
AFFECTS = statement_id(IMATINIB, "biolink:affects", ABL1)
GENE = statement_id(ABL1, "biolink:gene_associated_with_condition", CML)
OUTSIDE = statement_id(IMATINIB, "biolink:binds", ABL1)  # labeled, but not in this work dir

P_TREATS = pair_id(IMATINIB, CML)
P_AFFECTS = pair_id(IMATINIB, ABL1)
P_GENE = pair_id(ABL1, CML)

# What should go to the reviewer: (kind, pmid).
EXPECTED = {
    ("contradicted", "101"),
    ("contradicted", "201"),
    ("qualifier", "202"),
    ("qualifier", "302"),
}


def _proposal(sid, pmid, qualifiers, **kw) -> ProposalRow:
    return ProposalRow(sid, pmid, PV, MODEL, qualifiers, quote="the quote", **kw)


def _statement(sid, subject, predicate, object, rule) -> Statement:
    return Statement(
        statement_id=sid,
        pair_id=pair_id(subject, object),
        subject=subject,
        predicate=predicate,
        object=object,
        rule=rule,
        edge_count=1,
    )


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Path, Path]:
    work, cache_dir = tmp_path / "work", tmp_path / "cache"
    statements = [
        _statement(TREATS, IMATINIB, "biolink:treats", CML, "chemical-treats"),
        _statement(AFFECTS, IMATINIB, "biolink:affects", ABL1, "chemical-gene"),
        _statement(GENE, ABL1, "biolink:gene_associated_with_condition", CML, "gene-disease"),
    ]
    records.write_records(work / records.STATEMENTS, statements)
    records.write_records(
        work / records.SENTENCES,
        [
            SentenceRecord(statement_id=TREATS, sentence="imatinib treats CML"),
            SentenceRecord(statement_id=AFFECTS, sentence="imatinib affects ABL1"),
            SentenceRecord(statement_id=GENE, sentence="ABL1 is associated with CML"),
        ],
    )
    records.write_records(
        work / records.ENTITIES,
        [
            Entity(id=IMATINIB, name="imatinib", category=["biolink:SmallMolecule"]),
            Entity(id=ABL1, name="ABL1", category=["biolink:Gene"], synonyms=["c-ABL"]),
            Entity(id=CML, name="CML", category=["biolink:Disease"]),
        ],
    )
    records.write_records(
        work / records.PUBMED,
        [
            PubmedRecord(
                pair_id=P_AFFECTS,
                query="q",
                retrieved="2026-11-02",
                total=3,
                synonyms={IMATINIB: ["imatinib", "Gleevec", "STI571"], ABL1: ["ABL1"]},
            )
        ],
    )
    records.write_label_run(work, LabelRun(model=MODEL, prompt_version=PV, prompt="label.md"))
    with LabelCache(cache_dir / config.LABEL_CACHE) as cache:
        cache.put_labels(
            [
                LabelRow(TREATS, "101", PV, MODEL, "contradicted", "did not", True),
                LabelRow(TREATS, "102", PV, MODEL, "contradicted", "made up", False),
                LabelRow(TREATS, "103", PV, MODEL, "confirmed", "imatinib works", True),
                LabelRow(TREATS, "104", PV, "other-model", "contradicted", "q", True),
                LabelRow(AFFECTS, "201", PV, MODEL, "contradicted", "no effect", True, "causal"),
                LabelRow(GENE, "301", PV, MODEL, "ambiguous"),
                LabelRow(OUTSIDE, "401", PV, MODEL, "contradicted", "q", True),
            ]
        )
        cache.put_proposals(
            [
                _proposal(AFFECTS, "202", INHIBITS, quote_valid=True, valid=True, claim="causal"),
                _proposal(AFFECTS, "203", INHIBITS, quote_valid=True, valid=False),
                _proposal(AFFECTS, "202", EXPRESSION, quote_valid=False, valid=True),
                _proposal(GENE, "302", CONTRIBUTES, quote_valid=True, valid=True),
            ]
        )
    with PubMedCache(cache_dir / config.PUBMED_CACHE) as pubmed:
        pubmed.put_abstracts(
            Abstract(p, f"Title {p}", f"Abstract text of paper {p}.", 2020)
            for p in ("101", "102", "103", "104", "201", "202", "203", "301", "302")
        )
    return work, cache_dir


ITEM = re.compile(r"^### (R\d+): (\w+), PMID (\d+)$", re.M)


class FakeClient:
    """Accepts every item unless `decide(kind, pmid)` says otherwise (None leaves it out);
    raises LLMError for a message `fail(user)` is true of."""

    def __init__(self, decide=None, fail=None):
        self.decide = decide or (lambda kind, pmid: "accept")
        self.fail = fail or (lambda user: False)
        self.calls: list[dict] = []
        self.lock = threading.Lock()

    def complete_json(self, system, user, *, max_output_tokens=16000, reasoning_effort=None):
        with self.lock:
            self.calls.append(
                {
                    "system": system,
                    "user": user,
                    "max_output_tokens": max_output_tokens,
                    "reasoning_effort": reasoning_effort,
                }
            )
        if self.fail(user):
            raise llm.LLMError("HTTP 500 after retries")
        reviews = []
        for local, kind, pmid in ITEM.findall(user):
            verdict = self.decide(kind, pmid)
            if verdict is not None:
                reviews.append({"item": local, "verdict": verdict, "reason": f"{kind} {pmid}"})
        usage = llm.Usage(input_tokens=1000, output_tokens=100, cost=0.01, seconds=0.5)
        return {"reviews": reviews}, usage

    def items(self) -> set[tuple[str, str]]:
        return {(kind, pmid) for c in self.calls for _, kind, pmid in ITEM.findall(c["user"])}


def _call_for(client: FakeClient, pmid: str) -> str:
    [user] = [c["user"] for c in client.calls if f", PMID {pmid}\n" in c["user"]]
    return user


def _reviews(cache_dir: Path, **kw):
    with LabelCache(cache_dir / config.LABEL_CACHE) as cache:
        return cache.reviews(PV, MODEL, **kw)


def test_sends_only_items_that_add_edges_grouped_by_pair(setup):
    work, cache_dir = setup
    client = FakeClient()
    s = review.review_items(work, cache_dir, client=client, model=REVIEWER, workers=3)

    assert client.items() == EXPECTED
    assert len(client.calls) == 3  # one per pair
    assert s["items"] == {"contradicted": 2, "qualifier": 2}
    assert s["pending"] == 4 and s["pending_pairs"] == 3 and s["calls"] == 3
    assert s["accepted"] == {"contradicted": 2, "qualifier": 2}
    assert s["ok"] and s["cost"] == pytest.approx(0.03)

    system, _ = llm.load_prompt(config.REVIEW_PROMPT)
    assert all(c["system"] == system for c in client.calls)
    assert all(c["reasoning_effort"] == "medium" for c in client.calls)

    treats = _call_for(client, "101")
    assert "### PMID 101" in treats
    for other in ("102", "103", "104", "201"):
        assert f"PMID {other}" not in treats  # only the abstracts the items cite

    affects = _call_for(client, "201")
    assert affects == _call_for(client, "202")  # same pair, same call
    assert "### PMID 201" in affects and "### PMID 202" in affects
    assert "PMID 203" not in affects
    assert affects.count("Title 202") == 1
    # Synonyms from step 3's query, the edge and its resulting statement.
    assert f"- {IMATINIB}: imatinib (SmallMolecule); other names: Gleevec; STI571" in affects
    assert "Edge: imatinib affects ABL1" in affects
    assert "Resulting statement: imatinib causes decreased activity of ABL1" in affects
    assert f"Proposed qualifiers: {json.dumps(INHIBITS, sort_keys=True)}" in affects
    assert 'First-pass quote: "the quote"' in affects
    assert "First-pass claim: causal" in affects

    gene = _call_for(client, "302")
    assert "other names: c-ABL" in gene  # no pubmed record: the entity's own synonyms


def test_verdicts_stored_with_first_pass_keys(setup):
    work, cache_dir = setup
    client = FakeClient(decide=lambda kind, pmid: "reject" if pmid == "202" else "accept")
    s = review.review_items(work, cache_dir, client=client, model=REVIEWER, workers=1)
    assert s["rejected"] == {"contradicted": 0, "qualifier": 1}

    rows = {(r.kind, r.pmid): r for r in _reviews(cache_dir)}
    assert set(rows) == EXPECTED
    _, version = llm.load_prompt(config.REVIEW_PROMPT, extra=review.FORMAT_VERSION)
    for r in rows.values():
        assert (r.model, r.prompt_version) == (MODEL, PV)
        assert (r.reviewer, r.review_prompt_version) == (REVIEWER, version)
    assert rows[("contradicted", "101")].qualifiers is None
    assert rows[("contradicted", "101")].statement_id == TREATS
    assert rows[("qualifier", "202")].qualifiers == INHIBITS
    assert rows[("qualifier", "202")].verdict == "reject"
    assert rows[("qualifier", "202")].reason == "qualifier 202"
    assert rows[("qualifier", "302")].verdict == "accept"

    with LabelCache(cache_dir / config.LABEL_CACHE) as cache:
        usage = cache.usage("review", REVIEWER, version)
    assert usage["calls"] == 3 and usage["errors"] == 0 and usage["pairs"] == 3


def test_rerun_skips_reviewed_items_and_new_prompt_rereviews(setup, tmp_path):
    work, cache_dir = setup
    review.review_items(work, cache_dir, client=FakeClient(), model=REVIEWER)

    again = FakeClient()
    s = review.review_items(work, cache_dir, client=again, model=REVIEWER)
    assert again.calls == [] and s["already_reviewed"] == 4 and s["pending"] == 0

    edited = tmp_path / "review.md"
    edited.write_text(config.REVIEW_PROMPT.read_text() + "\nBe brief.\n", encoding="utf-8")
    fresh = FakeClient()
    s = review.review_items(work, cache_dir, client=fresh, model=REVIEWER, prompt=edited)
    assert fresh.items() == EXPECTED and s["already_reviewed"] == 0
    assert len({r.review_prompt_version for r in _reviews(cache_dir)}) == 2


def test_items_left_out_of_the_reply_are_not_stored_and_retried(setup):
    work, cache_dir = setup
    client = FakeClient(decide=lambda kind, pmid: None if pmid == "201" else "accept")
    s = review.review_items(work, cache_dir, client=client, model=REVIEWER)
    assert s["omitted"] == 1
    assert {(r.kind, r.pmid) for r in _reviews(cache_dir)} == EXPECTED - {("contradicted", "201")}

    retry = FakeClient()
    review.review_items(work, cache_dir, client=retry, model=REVIEWER)
    assert retry.items() == {("contradicted", "201")}
    assert "### PMID 202" not in retry.calls[0]["user"]


def test_invalid_reply_entries_are_counted():
    item = review.Item("contradicted", "s", "1")
    reply = {
        "reviews": [
            {"item": "R1", "verdict": "Accept", "reason": "ok"},
            {"item": "R1", "verdict": "reject"},
            {"item": "R9", "verdict": "accept"},
            {"item": "R1", "verdict": "maybe"},
            "R1",
        ]
    }
    verdicts, invalid = review.parse_reply(reply, {"R1": item})
    assert verdicts == {"R1": ("accept", "ok")} and invalid == 4
    with pytest.raises(ValueError):
        review.parse_reply({"verdicts": []}, {"R1": item})


def test_limit_and_label_run_updated(setup):
    work, cache_dir = setup
    client = FakeClient()
    s = review.review_items(work, cache_dir, client=client, model=REVIEWER, limit=1)
    assert s["pending_pairs"] == 1 and len(client.calls) == 1
    assert client.items() == {("contradicted", "101")}  # the first pair in sorted order

    run = records.read_label_run(work)
    _, version = llm.load_prompt(config.REVIEW_PROMPT, extra=review.FORMAT_VERSION)
    assert (run.model, run.prompt_version, run.prompt) == (MODEL, PV, "label.md")
    assert (run.reviewer, run.review_prompt_version) == (REVIEWER, version)


def test_other_label_run_leaves_label_run_json_alone(setup):
    work, cache_dir = setup
    client = FakeClient()
    s = review.review_items(
        work, cache_dir, client=client, model=REVIEWER, label_model="other-model"
    )
    assert client.items() == {("contradicted", "104")}
    assert not s["label_run_updated"] and records.read_label_run(work).reviewer is None


def test_statement_words():
    assert (
        review.statement_words("imatinib", "biolink:affects", "ABL1", INHIBITS)
        == "imatinib causes decreased activity of ABL1"
    )
    assert (
        review.statement_words(
            "A", "biolink:affects", "B", {"species_context_qualifier": "NCBITaxon:9606"}
        )
        == "A affects B (species context: NCBITaxon:9606)"
    )


PI = "pi:gpt-6.1-sol"


def _verdict_lines(path: Path, lines: list) -> None:
    path.write_text(
        "\n".join(line if isinstance(line, str) else json.dumps(line) for line in lines) + "\n",
        encoding="utf-8",
    )


def test_export_then_import_round_trip(setup, tmp_path):
    work, cache_dir = setup
    out = tmp_path / "pi" / "review_items.jsonl"
    s = review.export_items(work, cache_dir, out, reviewer=PI)
    assert s["exported"] == 4 and s["pairs"] == 3

    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert {(d["kind"], d["pmid"]) for d in lines} == EXPECTED
    for d in lines:
        qualifiers = d["qualifiers"] if d["kind"] == "qualifier" else None
        assert d["item_id"] == review.item_id(d["kind"], d["statement_id"], d["pmid"], qualifiers)
        assert d["title"] == f"Title {d['pmid']}" and d["abstract"]
        assert d["review_prompt_version"] == s["review_prompt_version"]
    by_pmid = {d["pmid"]: d for d in lines}
    assert by_pmid["202"]["resulting_statement"] == "imatinib causes decreased activity of ABL1"
    assert by_pmid["202"]["structured"]["predicate"] == "biolink:affects"
    assert by_pmid["202"]["entities"][IMATINIB]["synonyms"] == ["Gleevec", "STI571"]
    assert by_pmid["101"]["sentence"] == "imatinib treats CML"

    instructions = (tmp_path / "pi" / "review_items.instructions.md").read_text()
    assert s["instructions"].name == "review_items.instructions.md"
    assert '"item_id"' in instructions and '{"reviews"' not in instructions
    assert "## Contradicted items" in instructions
    assert s["review_prompt_version"].startswith("review-pi@")

    # Ids are stable: a second export gives the same ones.
    review.export_items(work, cache_dir, out, reviewer=PI)
    assert [json.loads(line)["item_id"] for line in out.read_text().splitlines()] == [
        d["item_id"] for d in lines
    ]

    verdicts = tmp_path / "pi" / "verdicts.jsonl"
    verdict = {d["item_id"]: "reject" if d["pmid"] == "101" else "accept" for d in lines}
    _verdict_lines(
        verdicts, [{"item_id": i, "verdict": v, "reason": "because"} for i, v in verdict.items()]
    )
    imported = review.import_verdicts(work, cache_dir, verdicts, reviewer=PI)
    assert imported["imported"] == 4 and imported["unknown"] == imported["invalid"] == 0
    assert imported["rejected"] == {"contradicted": 1, "qualifier": 0}

    rows = _reviews(cache_dir, reviewer=PI)
    assert {(r.kind, r.pmid) for r in rows} == EXPECTED
    assert {r.review_prompt_version for r in rows} == {s["review_prompt_version"]}
    assert next(r for r in rows if r.pmid == "202").qualifiers == INHIBITS
    run = records.read_label_run(work)
    assert run.reviewer == PI
    assert run.review_prompt_version == s["review_prompt_version"]

    assert review.export_items(work, cache_dir, out, reviewer=PI)["exported"] == 0  # nothing left


def test_export_is_per_reviewer(setup, tmp_path):
    """Items another reviewer has reviewed are still pending for this one: step 6 honours only
    the reviewer label_run.json names, which the import switches to."""
    work, cache_dir = setup
    out = tmp_path / "items.jsonl"
    review.export_items(work, cache_dir, out, reviewer=PI)
    first = json.loads(out.read_text().splitlines()[0])
    verdicts = tmp_path / "verdicts.jsonl"
    _verdict_lines(verdicts, [{"item_id": first["item_id"], "verdict": "accept"}])
    review.import_verdicts(work, cache_dir, verdicts, reviewer=PI)
    assert review.export_items(work, cache_dir, out, reviewer=PI)["exported"] == 3
    assert review.export_items(work, cache_dir, out, reviewer="pi:other")["exported"] == 4


def test_import_rejects_unknown_ids_and_invalid_verdicts(setup, tmp_path):
    work, cache_dir = setup
    out = tmp_path / "items.jsonl"
    review.export_items(work, cache_dir, out, reviewer="pi:x")
    first = json.loads(out.read_text().splitlines()[0])
    verdicts = tmp_path / "verdicts.jsonl"
    _verdict_lines(
        verdicts,
        [
            {"item_id": first["item_id"], "verdict": "accept", "reason": "ok"},
            {"item_id": "c-000000000000", "verdict": "accept"},
            {"item_id": first["item_id"], "verdict": "maybe"},
            "not json",
        ],
    )
    s = review.import_verdicts(
        work, cache_dir, verdicts, reviewer="pi:x", review_prompt_version="review-pi@manual"
    )
    assert (s["lines"], s["imported"], s["unknown"], s["invalid"]) == (4, 1, 1, 2)
    [row] = _reviews(cache_dir, reviewer="pi:x")
    assert row.review_prompt_version == "review-pi@manual"


def _args(work: Path, cache_dir: Path, *argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    review.add_arguments(parser)
    args = parser.parse_args(list(argv))
    args.work, args.cache_dir = work, cache_dir
    return args


def test_run_returns_1_when_too_many_calls_fail(setup, monkeypatch):
    work, cache_dir = setup
    client = FakeClient(fail=lambda user: ", PMID 101\n" in user)
    # No key needed when the client is faked: llm.Client is what checks for it.
    monkeypatch.delenv(config.LLM_API_KEY, raising=False)
    monkeypatch.setattr(llm, "load_model", lambda model_id, *a: llm.ModelInfo(model_id, "url"))
    monkeypatch.setattr(llm, "Client", lambda model, *a, **kw: client)

    assert review.run(_args(work, cache_dir, "--reasoning-effort", "none")) == 1
    assert len(client.calls) == 3 and client.calls[0]["reasoning_effort"] is None
    assert records.read_label_run(work).reviewer is None  # a failed pass isn't named
    assert ("contradicted", "101") not in {(r.kind, r.pmid) for r in _reviews(cache_dir)}
    with LabelCache(cache_dir / config.LABEL_CACHE) as cache:
        errors = cache.db.execute("SELECT pair_id, error FROM calls WHERE error IS NOT NULL")
        assert [tuple(r) for r in errors] == [(P_TREATS, "HTTP 500 after retries")]

    # The retry only sends the failed pair, and succeeds.
    client.fail = lambda user: False
    assert review.run(_args(work, cache_dir)) == 0
    assert len(client.calls) == 4 and ", PMID 101\n" in client.calls[-1]["user"]
    assert client.calls[-1]["reasoning_effort"] == "medium"
    assert records.read_label_run(work).reviewer == config.REVIEW_MODEL


def test_run_fails_cleanly_without_inputs(tmp_path, setup, monkeypatch):
    work, cache_dir = setup
    assert review.run(_args(tmp_path / "empty", cache_dir)) == 1  # no label_run.json
    assert review.run(_args(work, cache_dir, "--import", str(tmp_path / "missing.jsonl"))) == 1
    out = tmp_path / "items.jsonl"
    assert review.run(_args(work, cache_dir, "--export", str(out))) == 1  # needs --reviewer
    assert review.run(_args(work, cache_dir, "--export", str(out), "--reviewer", PI)) == 0
    assert review.run(_args(work, cache_dir, "--import", str(out))) == 1  # needs --reviewer
    monkeypatch.delenv(config.LLM_API_KEY, raising=False)
    assert review.run(_args(work, cache_dir)) == 1  # no API key


def test_instructions_replace_only_the_reply_section():
    prompt = (
        "# Review\n\n### Input and reply format notes\n\n"
        "## Input and reply format\n\nold\n\n## After\n\nkept\n"
    )
    text = review.instructions(prompt)
    assert text.startswith("# Review\n\n### Input and reply format notes\n\n")
    assert review.EXPORT_FORMAT in text and "old" not in text
    assert text.endswith("exported again next time.\n\n## After\n\nkept\n")
    assert review.instructions("# Review\n").endswith(review.EXPORT_FORMAT)
