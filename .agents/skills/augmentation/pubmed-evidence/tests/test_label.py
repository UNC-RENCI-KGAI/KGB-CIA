"""Step 5 (label.py) with a fake model client and stubbed Biolink lookups: no network."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest
from pubmed_evidence import biolink, config, label, llm, records
from pubmed_evidence.biolink import QualifierSpec
from pubmed_evidence.cache import Abstract, LabelCache, LabelRow, PubMedCache
from pubmed_evidence.graphio import pair_id, read_jsonl, statement_id, write_jsonl
from pubmed_evidence.records import (
    Entity,
    FetchedPmid,
    Pair,
    PubmedRecord,
    SentenceRecord,
    Statement,
)

IMATINIB = "CHEBI:45783"
ABL1 = "NCBIGene:25"
CML = "MONDO:0011996"
MODEL = "gpt-5.6-luna"  # listed in .pi/models.json, so it has costs


def statement(subject, predicate, object, qualifiers=None) -> Statement:
    return Statement(
        statement_id=statement_id(subject, predicate, object, qualifiers),
        pair_id=pair_id(subject, object),
        subject=subject,
        predicate=predicate,
        object=object,
        qualifiers=qualifiers or {},
        rule="test",
        edge_count=1,
    )


BIND = statement(IMATINIB, "biolink:directly_physically_interacts_with", ABL1)
AFFECTS = statement(
    IMATINIB,
    "biolink:affects",
    ABL1,
    {
        "object_aspect_qualifier": "activity",
        "object_direction_qualifier": "decreased",
        "qualified_predicate": "biolink:causes",
    },
)
TREATS = statement(IMATINIB, "biolink:treats", CML)
GENE_PAIR = pair_id(IMATINIB, ABL1)
CML_PAIR = pair_id(IMATINIB, CML)

SENTENCES = {
    BIND.statement_id: "imatinib directly physically interacts with ABL1.",
    AFFECTS.statement_id: "imatinib causes decreased activity of ABL1.",
    TREATS.statement_id: "imatinib treats chronic myeloid leukemia.",
}

ABSTRACTS = {
    "1001": Abstract(
        "1001",
        "Imatinib is a potent ABL1 inhibitor",
        "Imatinib bound the ABL1 kinase domain and inhibited its catalytic activity in vitro.",
        2001,
    ),
    "1002": Abstract(
        "1002",
        "Binding studies of STI571",
        "In our assays STI571 did not bind the active conformation of c-Abl at any dose.",
        2002,
    ),
    "1003": Abstract(
        "1003",
        "Cardiovascular events on imatinib",
        "ABL1 transcript levels were measured at baseline in all 300 patients on imatinib.",
        2019,
    ),
    "2001": Abstract(
        "2001",
        "Imatinib in CML",
        "Imatinib induced complete cytogenetic responses in most patients with chronic myeloid "
        "leukemia.",
        2004,
    ),
}

AFFECTS_QUALIFIERS = {
    "qualified_predicate": QualifierSpec(
        "qualified_predicate", ("biolink:causes",), "predicate_type", "A predicate. More."
    ),
    "object_aspect_qualifier": QualifierSpec(
        "object_aspect_qualifier",
        ("activity", "expression", "abundance", "secretion"),
        "GeneOrGeneProductOrChemicalEntityAspectEnum",
        "The aspect of the object that is affected. Further details follow here.",
    ),
    "object_direction_qualifier": QualifierSpec(
        "object_direction_qualifier", ("increased", "decreased"), "DirectionQualifierEnum"
    ),
    # Open-ranged: never offered.
    "anatomical_context_qualifier": QualifierSpec(
        "anatomical_context_qualifier", None, "AnatomicalEntity"
    ),
}


REAL_ALLOWED = biolink.allowed_qualifiers
REAL_VALIDATE = biolink.validate_qualifiers
GENE = ["biolink:Gene"]


@pytest.fixture(autouse=True)
def stub_biolink(monkeypatch):
    """biolink.py is another module's job: stub the two lookups step 5 makes."""
    validated: list[dict] = []

    def allowed(subject_categories, predicate, object_categories):
        return dict(AFFECTS_QUALIFIERS) if predicate == "biolink:affects" else {}

    def validate(qualifiers, subject_categories, predicate, object_categories):
        validated.append(dict(qualifiers))
        return []

    monkeypatch.setattr(biolink, "allowed_qualifiers", allowed)
    monkeypatch.setattr(biolink, "validate_qualifiers", validate)
    return validated


def pubmed_record(pid: str, pmids: list[str]) -> PubmedRecord:
    return PubmedRecord(
        pair_id=pid,
        query="q",
        retrieved="2026-11-02",
        total=len(pmids),
        synonyms={IMATINIB: ["imatinib", "Gleevec", "STI571"], ABL1: ["ABL1", "c-Abl"]},
        mesh={IMATINIB: ["Imatinib Mesylate"]},
        pmids=[FetchedPmid(pmid=p, origin="search", rank=i) for i, p in enumerate(pmids, 1)],
    )


@dataclass
class Work:
    work: Path
    cache_dir: Path

    def write_pubmed(self, gene_pmids: list[str], cml_pmids: list[str] | None = None) -> None:
        recs = [pubmed_record(GENE_PAIR, gene_pmids)]
        if cml_pmids is not None:
            recs.append(pubmed_record(CML_PAIR, cml_pmids))
        records.write_records(self.work / records.PUBMED, recs)

    def add_abstracts(self, abstracts) -> None:
        with PubMedCache(self.cache_dir / config.PUBMED_CACHE) as cache:
            cache.put_abstracts(abstracts)

    def labels(self, version: str | None = None, model: str = MODEL) -> dict:
        version = version or label.label_prompt()[1]
        with LabelCache(self.cache_dir / config.LABEL_CACHE) as cache:
            return {(r.statement_id, r.pmid): r for r in cache.labels(version, model)}

    def proposals(self, model: str = MODEL) -> list:
        with LabelCache(self.cache_dir / config.LABEL_CACHE) as cache:
            return cache.proposals(label.label_prompt()[1], model)


@pytest.fixture
def w(tmp_path: Path) -> Work:
    work = Work(tmp_path / "work", tmp_path / "cache")
    records.write_records(
        work.work / records.ENTITIES,
        [
            Entity(
                id=IMATINIB,
                name="imatinib",
                category=["biolink:SmallMolecule", "biolink:ChemicalEntity"],
                synonyms=["Gleevec", "STI571"],
            ),
            Entity(id=ABL1, name="ABL1", category=["biolink:Gene"], synonyms=["ABL"]),
            Entity(id=CML, name="chronic myeloid leukemia", category=["biolink:Disease"]),
        ],
    )
    records.write_records(work.work / records.STATEMENTS, [BIND, AFFECTS, TREATS])
    records.write_records(
        work.work / records.PAIRS,
        [
            Pair(
                pair_id=GENE_PAIR,
                nodes=tuple(sorted((IMATINIB, ABL1))),
                statement_ids=[BIND.statement_id, AFFECTS.statement_id],
            ),
            Pair(
                pair_id=CML_PAIR,
                nodes=tuple(sorted((IMATINIB, CML))),
                statement_ids=[TREATS.statement_id],
            ),
        ],
    )
    records.write_records(
        work.work / records.SENTENCES,
        [SentenceRecord(statement_id=k, sentence=v) for k, v in SENTENCES.items()],
    )
    work.write_pubmed(["1001", "1002", "1003"], ["2001"])
    work.add_abstracts(ABSTRACTS.values())
    return work


@dataclass
class Sent:
    """What the fake client was sent, with the message's local ids read back."""

    system: str
    user: str

    @property
    def pmids(self) -> list[str]:
        return re.findall(r"^## PMID:(\d+)", self.user, re.M)

    @property
    def local(self) -> dict[str, str]:
        """Statement id -> its local id (S1, ...) in this message."""
        by_sentence = {v: k for k, v in SENTENCES.items()}
        return {
            by_sentence[sentence]: sid
            for sid, sentence in re.findall(r"^(S\d+): (.+)$", self.user, re.M)
        }

    def s(self, st: Statement) -> str:
        return self.local[st.statement_id]


class FakeClient:
    def __init__(self, answer=None):
        self.answer = answer or (lambda sent: {"labels": [], "proposals": []})
        self.sent: list[Sent] = []
        self._lock = threading.Lock()

    def complete_json(self, system, user, *, max_output_tokens=16000, reasoning_effort=None):
        sent = Sent(system, user)
        with self._lock:
            self.sent.append(sent)
        result = self.answer(sent)
        if isinstance(result, Exception):
            raise result
        return result, llm.Usage(1000, 100, 200, 0.001, 0.1)


def run_label(w: Work, client: FakeClient, **kw) -> dict:
    return label.label_pairs(w.work, w.cache_dir, model=MODEL, client=client, workers=1, **kw)


def args_for(w: Work, *argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    label.add_arguments(parser)
    args = parser.parse_args(["--model", MODEL, *argv])
    args.input, args.output, args.work, args.cache_dir = None, None, w.work, w.cache_dir
    return args


# --- Labels ---------------------------------------------------------------------------------


def test_labels_quotes_and_no_evidence(w):
    def answer(sent: Sent):
        if BIND.statement_id not in sent.local:
            return {"labels": [], "proposals": []}
        return {
            "labels": [
                {
                    "statement": sent.s(BIND),
                    "pmid": "1001",
                    "quote": "Imatinib bound the ABL1 kinase domain",
                    "label": "confirmed",
                    "claim": "causal",
                },
                {
                    "statement": sent.s(BIND),
                    "pmid": "PMID:1002",
                    "quote": "STI571 never bound c-Abl in any assay we ran",
                    "label": "contradicted",
                    "claim": "causal",
                },
            ],
            "proposals": [],
        }

    summary = run_label(w, FakeClient(answer))
    labels = w.labels()
    assert len(labels) == 7  # 2 statements x 3 abstracts + 1 x 1
    confirmed = labels[(BIND.statement_id, "1001")]
    assert (confirmed.label, confirmed.quote_valid, confirmed.claim) == (
        "confirmed",
        True,
        "causal",
    )
    contradicted = labels[(BIND.statement_id, "1002")]
    assert (contradicted.label, contradicted.quote_valid) == ("contradicted", False)
    rest = [
        r for k, r in labels.items() if k not in {(BIND.statement_id, p) for p in ("1001", "1002")}
    ]
    assert {r.label for r in rest} == {"no_evidence"}
    assert all(r.quote_valid is None for r in rest)
    assert summary["labels"] == {
        "confirmed": 1,
        "contradicted": 1,
        "ambiguous": 0,
        "no_evidence": 5,
    }
    assert summary["invalid_quotes"] == 1
    assert (summary["calls"], summary["calls_ok"], summary["calls_failed"]) == (2, 2, 0)
    assert summary["usage"]["calls"] == 2 and summary["usage"]["pairs"] == 2


def test_message_content(w):
    client = FakeClient()
    run_label(w, client)
    gene = next(s for s in client.sent if "1001" in s.pmids)
    assert gene.system == label.label_prompt()[0]
    assert gene.pmids == ["1001", "1002", "1003"]
    assert "- imatinib (CHEBI:45783)" in gene.user
    assert "synonyms: Gleevec; STI571" in gene.user
    assert "MeSH headings: Imatinib Mesylate" in gene.user
    assert "qualifiers: object_aspect_qualifier: activity; object_direction_qualifier: " in (
        gene.user
    )
    assert "Title: Imatinib is a potent ABL1 inhibitor" in gene.user
    # Only enum-valued slots are offered; long value lists by name, short ones inline.
    assert "anatomical_context_qualifier" not in gene.user
    assert "- object_aspect_qualifier: one of GeneOrGeneProductOrChemicalEntityAspectEnum" in (
        gene.user
    )
    assert "- object_direction_qualifier: increased, decreased" in gene.user
    assert "- qualified_predicate: biolink:causes" in gene.user
    assert "# Qualifier values" in gene.user
    assert (
        "- GeneOrGeneProductOrChemicalEntityAspectEnum: activity, expression, abundance, "
        "secretion" in gene.user
    )
    assert "allowed qualifiers: none" in gene.user  # the binding statement


def test_statements_sharing_qualifiers_share_a_group():
    second = statement(IMATINIB, "biolink:affects", ABL1)
    job = label.PairJob(
        GENE_PAIR,
        [
            Entity(id=IMATINIB, name="imatinib", category=["biolink:SmallMolecule"]),
            Entity(id=ABL1, name="ABL1", category=["biolink:Gene"]),
        ],
        {},
        {},
        [AFFECTS, BIND, second],
        {AFFECTS.statement_id: "a.", BIND.statement_id: "b.", second.statement_id: "c."},
        ["1001"],
    )
    request = label.Request(GENE_PAIR, job.statements, ["1001"], job.combos())
    offered = {s.statement_id: label.Qualifiers().offered(job, s) for s in job.statements}
    text = label.build_message(job, request, ABSTRACTS, offered)
    assert text.count("allowed qualifiers: Q1") == 2
    assert "Q1, for S1 and S3:" in text
    assert "Q2" not in text
    assert text.count("- object_direction_qualifier: increased, decreased") == 1
    assert text.rstrip().endswith("reply with the JSON object only.")


def test_value_lists_written_once():
    aspects = ("activity", "expression", "abundance", "secretion")
    other = ("activity", "expression", "abundance", "secretion", "uptake")
    enum = "AspectEnum"

    def spec(slot, values):
        return QualifierSpec(slot, values, enum)

    statements = [AFFECTS, BIND, TREATS]
    offered = {
        AFFECTS.statement_id: {"object_aspect_qualifier": spec("object_aspect_qualifier", aspects)},
        BIND.statement_id: {
            "object_aspect_qualifier": spec("object_aspect_qualifier", aspects),
            "subject_aspect_qualifier": spec("subject_aspect_qualifier", aspects),
        },
        TREATS.statement_id: {"object_aspect_qualifier": spec("object_aspect_qualifier", other)},
    }
    job = label.PairJob(
        GENE_PAIR, [Entity(id=IMATINIB), Entity(id=ABL1)], {}, {}, statements, SENTENCES, ["1001"]
    )
    request = label.Request(GENE_PAIR, statements, ["1001"], job.combos())
    text = label.build_message(job, request, ABSTRACTS, offered)
    assert [line for line in text.splitlines() if line.startswith("Q")] == [
        "Q1, for S1:",
        "Q2, for S2:",
        "Q3, for S3:",
    ]
    assert text.count("- AspectEnum: activity, expression, abundance, secretion\n") == 1
    assert "- AspectEnum (2): activity, expression, abundance, secretion, uptake" in text
    assert text.count("- subject_aspect_qualifier: one of AspectEnum\n") == 1
    assert "- object_aspect_qualifier: one of AspectEnum (2)" in text


def test_open_range_qualifiers_are_invalid():
    job = label.PairJob(
        GENE_PAIR,
        [Entity(id=IMATINIB, category=["biolink:SmallMolecule"]), Entity(id=ABL1, category=GENE)],
        {},
        {},
        [AFFECTS],
        SENTENCES,
        ["1001"],
    )
    q = label.Qualifiers()
    assert "anatomical_context_qualifier" not in q.offered(job, AFFECTS)
    errors = q.errors(job, AFFECTS, {"anatomical_context_qualifier": "UBERON:0002107"})
    assert errors == ["anatomical_context_qualifier: open-range qualifier not proposed by the LLM"]
    assert q.errors(job, AFFECTS, {"object_direction_qualifier": "increased"}) == []
    assert "not an allowed value" in q.errors(job, AFFECTS, {"object_direction_qualifier": "up"})[0]


# --- Parsing --------------------------------------------------------------------------------


def request_for(statements, pmids, combos=None) -> label.Request:
    every = {(s.statement_id, p) for s in statements for p in pmids}
    return label.Request(
        GENE_PAIR, list(statements), list(pmids), every if combos is None else combos
    )


def parse(reply, request, check=lambda s, q: []):
    return label.parse_reply(
        reply, request, ABSTRACTS, prompt_version="label@x", model=MODEL, check=check
    )


def test_parse_reads_ids_and_labels_leniently():
    request = request_for([BIND, AFFECTS], ["1001", "1002"])
    quote = "Imatinib bound the ABL1 kinase domain"
    parsed = parse(
        {
            "labels": [
                {"statement": "s1", "pmid": 1001, "label": "Confirmed.", "quote": quote},
                {"statement": "S 2", "pmid": "PMID 1002", "label": "no-evidence"},
                {"statement": "2.", "pmid": "PMID:1002", "label": " ambiguous ", "claim": "causal"},
            ]
        },
        request,
    )
    got = {(r.statement_id, r.pmid): r for r in parsed.labels}
    assert len(got) == 4
    assert got[(BIND.statement_id, "1001")].label == "confirmed"
    assert got[(BIND.statement_id, "1001")].quote_valid is True
    assert got[(BIND.statement_id, "1002")].label == "no_evidence"
    # The second label of a combination wins over no_evidence; claims only on decisive labels.
    assert got[(AFFECTS.statement_id, "1002")].label == "ambiguous"
    assert got[(AFFECTS.statement_id, "1002")].claim is None
    assert parsed.problems == {"duplicate label": 1}


@pytest.mark.parametrize(
    "item",
    [
        {"statement": "S9", "pmid": "1001", "label": "confirmed"},
        {"statement": "S1", "pmid": "9999", "label": "confirmed"},
        {"statement": "S1", "pmid": "1001", "label": "supported"},
        "not an object",
    ],
)
def test_parse_rejects_a_reply_it_cant_read(item):
    request = request_for([BIND], ["1001"])
    good = {"statement": "S1", "pmid": "1001", "label": "ambiguous"}
    with pytest.raises(ValueError):
        parse({"labels": [good, item]}, request)


@pytest.mark.parametrize(
    "proposals",
    [
        "not a list",
        ["not an object"],
        [{"statement": "S7", "qualifiers": {"object_aspect_qualifier": "activity"}}],
        [{"statement": "S1", "qualifiers": "activity", "evidence": [{"pmid": "1001"}]}],
        [{"statement": "S1", "qualifiers": {"object_aspect_qualifier": "activity"}}],
        [
            {
                "statement": "S1",
                "qualifiers": {"object_aspect_qualifier": "expression"},
                "evidence": [{"pmid": "4242", "quote": "x"}],
            }
        ],
    ],
)
def test_parse_rejects_malformed_proposals(proposals):
    request = request_for([AFFECTS], ["1001"])
    with pytest.raises(ValueError):
        parse({"labels": [], "proposals": proposals}, request)


def test_unreadable_reply_is_a_failed_call_and_retried(w):
    bad = {"labels": [{"statement": "S1", "pmid": "PMID one", "label": "confirmed"}]}
    summary = run_label(w, FakeClient(lambda sent: bad))
    assert summary["calls_failed"] == summary["calls"] > 0
    assert w.labels() == {}
    assert not (w.work / records.LABEL_RUN).exists()
    retry = FakeClient()
    run_label(w, retry)
    assert len(retry.sent) == summary["calls"]


def test_unexpected_exception_is_a_failed_call(w):
    summary = run_label(w, FakeClient(lambda sent: KeyError("boom")))
    assert summary["calls_failed"] == summary["calls"] > 0
    with LabelCache(w.cache_dir / config.LABEL_CACHE) as cache:
        errors = [r["error"] for r in cache.db.execute("SELECT error FROM calls")]
    assert errors and all(e.startswith("KeyError") for e in errors)


def test_failed_run_leaves_label_run_alone(w):
    run_label(w, FakeClient())
    before = records.read_label_run(w.work)
    summary = label.label_pairs(
        w.work,
        w.cache_dir,
        model="gpt-oss-120b",
        client=FakeClient(lambda sent: llm.LLMError("down")),
        workers=1,
    )
    assert summary["label_run_written"] is False
    assert records.read_label_run(w.work) == before


def test_qualified_predicate_needs_a_subject_or_object_qualifier():
    request = request_for([BIND], ["1001"])
    quote = "Imatinib bound the ABL1 kinase domain"
    parsed = parse(
        {
            "labels": [],
            "proposals": [
                {
                    "statement": "S1",
                    "qualifiers": {
                        "causal_mechanism_qualifier": "inhibition",
                        "qualified_predicate": "biolink:causes",
                    },
                    "claim": "causal",
                    "evidence": [{"pmid": "1001", "quote": quote}],
                }
            ],
        },
        request,
    )
    (row,) = parsed.proposals
    assert row.qualifiers == {"causal_mechanism_qualifier": "inhibition"}
    assert parsed.problems["qualified_predicate dropped: no subject or object qualifier"] == 1


def test_parse_duplicates_resolved_deterministically():
    request = request_for([BIND], ["1001"])
    good = "Imatinib bound the ABL1 kinase domain"
    bad = "imatinib was invented in a garage somewhere"

    def one(*items):
        return parse({"labels": list(items)}, request).labels[0]

    confirmed_bad = {"statement": "S1", "pmid": "1001", "label": "confirmed", "quote": bad}
    confirmed = {"statement": "S1", "pmid": "1001", "label": "confirmed", "quote": good}
    contradicted = {"statement": "S1", "pmid": "1001", "label": "contradicted", "quote": good}
    ambiguous = {"statement": "S1", "pmid": "1001", "label": "ambiguous"}
    assert one(confirmed_bad, confirmed).quote_valid is True
    assert one(confirmed, confirmed_bad).quote_valid is True
    assert one(confirmed_bad, ambiguous).label == "ambiguous"
    # Confirmed and contradicted, both quoted: mixed evidence.
    mixed = one(confirmed, contradicted)
    assert (mixed.label, mixed.quote) == ("ambiguous", None)


def test_parse_skips_combinations_not_asked_for():
    combos = {(AFFECTS.statement_id, "1001")}
    request = request_for([BIND, AFFECTS], ["1001"], combos)
    parsed = parse({"labels": [{"statement": "S1", "pmid": "1001", "label": "ambiguous"}]}, request)
    assert [(r.statement_id, r.label) for r in parsed.labels] == [
        (AFFECTS.statement_id, "no_evidence")
    ]
    assert parsed.problems == {"combination not asked for": 1}


def test_parse_reply_without_labels_is_an_error():
    with pytest.raises(ValueError):
        parse({"answer": "I could not find anything"}, request_for([BIND], ["1001"]))
    with pytest.raises(ValueError):
        parse([], request_for([BIND], ["1001"]))


def test_with_claim():
    direction = {"object_direction_qualifier": "increased"}
    added, errors = label.with_claim(direction, {}, "causal")
    assert added == {**direction, "qualified_predicate": "biolink:causes"} and errors == []
    own = {"qualified_predicate": "biolink:causes"}
    assert label.with_claim(direction, own, "causal") == (direction, [])
    assert label.with_claim(direction, {}, "associative") == (direction, [])
    assert label.with_claim(direction, {}, None) == (direction, [])
    _, errors = label.with_claim(direction, own, "associative")
    assert errors and "associative" in errors[0]
    mechanism = {"object_specialization_qualifier": "x"}
    assert label.with_claim(mechanism, {}, "causal") == (mechanism, [])


def test_normalize_qualifiers():
    assert label.normalize_qualifiers(
        {
            "biolink:object_direction_qualifier": " increased ",
            "qualified_predicate": "causes",
            "object_aspect_qualifier": "",
        }
    ) == {"object_direction_qualifier": "increased", "qualified_predicate": "biolink:causes"}
    assert label.normalize_qualifiers("nope") is None


# --- Proposals ------------------------------------------------------------------------------


def test_proposals_validated_and_stored_per_pmid(w, stub_biolink):
    found = "Imatinib bound the ABL1 kinase domain and inhibited its catalytic activity"

    def answer(sent: Sent):
        if AFFECTS.statement_id not in sent.local:
            return {"labels": []}
        a, b = sent.s(AFFECTS), sent.s(BIND)
        return {
            "labels": [],
            "proposals": [
                {
                    "statement": a,
                    "qualifiers": {"biolink:object_direction_qualifier": "increased"},
                    "claim": "causal",
                    "evidence": [
                        {"pmid": "1001", "quote": found},
                        {"pmid": "1003", "quote": "a sentence that is not in the abstract"},
                    ],
                },
                # Only what the statement already says: dropped.
                {
                    "statement": a,
                    "qualifiers": {
                        "object_direction_qualifier": "decreased",
                        "qualified_predicate": "causes",
                    },
                    "claim": "causal",
                    "evidence": [{"pmid": "1001", "quote": found}],
                },
                # No qualifiers are offered for the binding statement.
                {
                    "statement": b,
                    "qualifiers": {"object_direction_qualifier": "decreased"},
                    "claim": "associative",
                    "evidence": [{"pmid": "1001", "quote": found}],
                },
                # An associative claim on a statement that says `causes`.
                {
                    "statement": a,
                    "qualifiers": {"object_aspect_qualifier": "expression"},
                    "claim": "associative",
                    "evidence": [{"pmid": "1002", "quote": found}],
                },
            ],
        }

    summary = run_label(w, FakeClient(answer))
    rows = {(p.statement_id, p.pmid, json.dumps(p.qualifiers)): p for p in w.proposals()}
    up = {"object_direction_qualifier": "increased"}
    good = rows[(AFFECTS.statement_id, "1001", json.dumps(up))]
    assert (good.quote_valid, good.valid, good.errors, good.claim) == (True, True, [], "causal")
    unquoted = rows[(AFFECTS.statement_id, "1003", json.dumps(up))]
    assert (unquoted.quote_valid, unquoted.valid) == (False, True)
    binding = rows[
        (BIND.statement_id, "1001", json.dumps({"object_direction_qualifier": "decreased"}))
    ]
    assert binding.valid is False
    assert any("not among the qualifiers offered" in e for e in binding.errors)
    expression = rows[
        (AFFECTS.statement_id, "1002", json.dumps({"object_aspect_qualifier": "expression"}))
    ]
    assert expression.valid is False and "associative" in expression.errors[0]
    assert len(rows) == 4
    assert (summary["proposals_valid"], summary["proposals_invalid"]) == (2, 2)
    assert summary["problems"]["proposal adding nothing"] == 1
    # Biolink validates the qualifiers step 6 will write: the statement's, merged with these.
    assert {**AFFECTS.qualifiers, **up} in stub_biolink


def test_causal_direction_gets_qualified_predicate(w):
    unqualified = statement(IMATINIB, "biolink:affects", ABL1)
    request = request_for([unqualified], ["1001"])
    job = label.PairJob(
        GENE_PAIR,
        [Entity(id=IMATINIB, category=["biolink:SmallMolecule"]), Entity(id=ABL1, category=GENE)],
        {},
        {},
        [unqualified],
        {unqualified.statement_id: "x."},
        ["1001"],
    )
    q = label.Qualifiers()
    parsed = parse(
        {
            "labels": [],
            "proposals": [
                {
                    "statement": "S1",
                    "qualifiers": {"object_aspect_qualifier": "activity"},
                    "claim": "causal",
                    "evidence": [{"pmid": "1001", "quote": "inhibited its catalytic activity"}],
                }
            ],
        },
        request,
        check=lambda s, qualifiers: q.errors(job, s, qualifiers),
    )
    (row,) = parsed.proposals
    assert row.qualifiers == {
        "object_aspect_qualifier": "activity",
        "qualified_predicate": "biolink:causes",
    }
    assert row.valid is True


# --- Reruns, prompts, chunks ----------------------------------------------------------------


def test_rerun_sends_only_missing_combinations(w):
    run_label(w, FakeClient())
    again = FakeClient()
    summary = run_label(w, again)
    assert again.sent == [] and summary["calls"] == 0 and summary["pairs_to_call"] == 0

    w.write_pubmed(["1001", "1002", "1003", "1004"], ["2001"])
    w.add_abstracts([Abstract("1004", "New", "Imatinib and ABL1, newly studied together.")])
    third = FakeClient()
    run_label(w, third)
    (sent,) = third.sent
    assert sent.pmids == ["1004"]
    assert set(sent.local) == {BIND.statement_id, AFFECTS.statement_id}
    assert len(w.labels()) == 9


def test_changed_prompt_starts_fresh(w, tmp_path):
    prompt = tmp_path / "label_tuned.md"
    shutil.copy(config.LABEL_PROMPT, prompt)
    first = run_label(w, FakeClient(), prompt=prompt)
    assert first["prompt_version"].startswith("label_tuned@")
    prompt.write_text(prompt.read_text() + "\nOne more rule.\n", encoding="utf-8")
    client = FakeClient()
    second = run_label(w, client, prompt=prompt)
    assert second["prompt_version"] != first["prompt_version"]
    assert len(client.sent) == 2
    assert client.sent[0].system.endswith("One more rule.\n")
    assert len(w.labels(first["prompt_version"])) == 7
    assert len(w.labels(second["prompt_version"])) == 7


def test_new_model_starts_fresh(w):
    run_label(w, FakeClient())
    client = FakeClient()
    label.label_pairs(w.work, w.cache_dir, model="gpt-oss-120b", client=client, workers=1)
    assert len(client.sent) == 2


def test_chunking(w):
    client = FakeClient()
    summary = run_label(w, client, abstracts_per_call=2)
    assert sorted(s.pmids for s in client.sent) == [["1001", "1002"], ["1003"], ["2001"]]
    assert summary["calls"] == 3 and len(w.labels()) == 7


def test_plan_requests_with_partial_labels():
    job = label.PairJob(GENE_PAIR, [], {}, {}, [BIND, AFFECTS], {}, ["1001", "1002", "1003"])
    labeled = {
        (BIND.statement_id, "1001"),
        (AFFECTS.statement_id, "1001"),
        (BIND.statement_id, "1002"),
    }
    requests = label.plan_requests(job, labeled, per_call=1)
    assert [(r.pmids, [s.statement_id for s in r.statements]) for r in requests] == [
        (["1002"], [AFFECTS.statement_id]),
        (["1003"], [BIND.statement_id, AFFECTS.statement_id]),
    ]
    assert requests[1].combos == {(BIND.statement_id, "1003"), (AFFECTS.statement_id, "1003")}


def test_pairs_without_pubmed_or_abstracts_are_skipped(w):
    w.write_pubmed(["1001"])  # no record for the CML pair
    summary = run_label(w, FakeClient())
    assert summary["skipped"] == {"pairs without a PubMed record": 1}
    w.write_pubmed(["7777"], ["2001"])  # an abstract the cache doesn't have
    summary = run_label(w, FakeClient())
    assert summary["skipped"] == {
        "abstracts missing from the PubMed cache": 1,
        "pairs without abstracts": 1,
    }


# --- Failures -------------------------------------------------------------------------------


def test_failed_calls_logged_and_fail_the_run(w, monkeypatch, capsys):
    def answer(sent: Sent):
        if "2001" in sent.pmids:
            return llm.LLMError("incomplete response (max_output_tokens)", llm.Usage(500, 16000))
        return {"labels": []}

    client = FakeClient(answer)
    monkeypatch.setattr(llm, "Client", lambda model, **kw: client)
    assert label.run(args_for(w, "--workers", "2")) == 1
    err = capsys.readouterr().err
    assert "max_output_tokens" in err and "1 of 2 calls failed" in err
    with LabelCache(w.cache_dir / config.LABEL_CACHE) as cache:
        rows = cache.db.execute("SELECT pair_id, error, output_tokens FROM calls").fetchall()
    failed = [r for r in rows if r["error"]]
    assert [(r["pair_id"], r["output_tokens"]) for r in failed] == [(CML_PAIR, 16000)]
    # The other pair's labels are kept, and a rerun retries only the failed pair.
    assert len(w.labels()) == 6
    retry = FakeClient()
    run_label(w, retry)
    assert [s.pmids for s in retry.sent] == [["2001"]]


def test_few_failures_only_warn(w, monkeypatch, capsys):
    many = [str(3000 + i) for i in range(21)]
    w.write_pubmed(many, ["2001"])
    w.add_abstracts([Abstract(p, f"Title {p}", f"Abstract text number {p}.") for p in many])

    def answer(sent: Sent):
        return llm.LLMError("boom") if sent.pmids == ["3005"] else {"labels": []}

    monkeypatch.setattr(llm, "Client", lambda model, **kw: FakeClient(answer))
    assert label.run(args_for(w, "--abstracts-per-call", "1")) == 0
    captured = capsys.readouterr()
    assert "calls: 22 (21 ok, 1 failed)" in captured.out
    assert "rerun to retry; labels already cached are kept" in captured.err


def test_malformed_reply_counts_as_failed(w):
    client = FakeClient(lambda sent: {"verdict": "nothing here"})
    summary = run_label(w, client)
    assert summary["calls_failed"] == 2
    assert w.labels() == {}


def test_missing_input_names_the_step(w, capsys):
    (w.work / records.SENTENCES).unlink()
    assert label.run(args_for(w)) == 1
    assert "kgb-augment sentences" in capsys.readouterr().err
    (w.work / records.PAIRS).unlink()
    assert label.run(args_for(w)) == 1
    assert "kgb-augment select" in capsys.readouterr().err


def test_missing_key(w, monkeypatch, capsys):
    monkeypatch.delenv(config.LLM_API_KEY, raising=False)
    assert label.run(args_for(w)) == 1
    assert config.LLM_API_KEY in capsys.readouterr().err


# --- Modes ----------------------------------------------------------------------------------


def test_sample_files(w):
    client = FakeClient(
        lambda sent: {
            "labels": [
                {
                    "statement": "S1",
                    "pmid": sent.pmids[0],
                    "label": "confirmed",
                    "quote": "made-up words that are not there",
                    "claim": "causal",
                }
            ]
        }
    )
    summary = run_label(w, client, sample=1, seed=0)
    assert summary["pairs"] == 1 and len(client.sent) == 1
    md = (w.work / label.SAMPLE_MD).read_text()
    rows = list(read_jsonl(w.work / label.SAMPLE_JSONL))
    pmids = client.sent[0].pmids
    assert len(rows) == len(client.sent[0].local) * len(pmids)
    assert set(rows[0]) == {
        "statement_id",
        "pair_id",
        "sentence",
        "pmid",
        "title",
        "abstract",
        "label",
        "quote",
        "quote_valid",
        "claim",
        "gold_label",
    }
    assert all(r["gold_label"] is None for r in rows)
    assert {r["label"] for r in rows} <= {"confirmed", "no_evidence"}
    assert f"### PMID:{pmids[0]}" in md and "**confirmed**" in md and "QUOTE NOT FOUND" in md
    # The same seed picks the same pair.
    again = FakeClient()
    run_label(w, again, sample=1, seed=0)
    assert again.sent == []  # already labeled
    assert list(read_jsonl(w.work / label.SAMPLE_JSONL)) == rows


def test_gold_restricts_combinations(w, tmp_path):
    gold = tmp_path / "gold.jsonl"
    write_jsonl(
        gold,
        [
            {"statement_id": BIND.statement_id, "pmid": "1002", "gold_label": "contradicted"},
            {"statement_id": "unknown", "pmid": "1001", "gold_label": "confirmed"},
        ],
    )
    client = FakeClient()
    summary = run_label(w, client, gold=gold)
    (sent,) = client.sent
    assert sent.pmids == ["1002"] and list(sent.local) == [BIND.statement_id]
    assert list(w.labels()) == [(BIND.statement_id, "1002")]
    assert summary["skipped"] == {"gold items with an unknown statement": 1}


def test_dry_run_sends_nothing(w, monkeypatch):
    def no_client(*a, **kw):
        raise AssertionError("a dry run must not build a client")

    monkeypatch.setattr(llm, "Client", no_client)
    monkeypatch.delenv(config.LLM_API_KEY, raising=False)
    summary = label.label_pairs(w.work, w.cache_dir, model=MODEL, dry_run=True)
    assert summary["requests"] == 2 and summary["calls"] == 0
    assert summary["estimated_input_tokens"] > 1000
    assert summary["estimated_cost"] > 0
    example = (w.work / label.REQUEST_EXAMPLE).read_text()
    assert "=== instructions (system) ===" in example and "## PMID:" in example
    assert not (w.cache_dir / config.LABEL_CACHE).exists()
    assert not (w.work / records.LABEL_RUN).exists()


def test_label_run_written(w):
    run_label(w, FakeClient())
    run = records.read_label_run(w.work)
    version = label.label_prompt()[1]
    assert (run.model, run.prompt_version, run.prompt) == (MODEL, version, str(config.LABEL_PROMPT))
    # A review of this run is kept; another model's run clears it.
    records.write_label_run(
        w.work, run.model_copy(update={"reviewer": "gpt-5.6-terra", "review_prompt_version": "r@1"})
    )
    run_label(w, FakeClient())
    assert records.read_label_run(w.work).reviewer == "gpt-5.6-terra"
    label.label_pairs(w.work, w.cache_dir, model="gpt-oss-120b", client=FakeClient(), workers=1)
    run = records.read_label_run(w.work)
    assert (run.model, run.reviewer, run.review_prompt_version) == ("gpt-oss-120b", None, None)


def test_sample_and_gold_runs_leave_label_run_alone(w):
    run_label(w, FakeClient())
    before = records.read_label_run(w.work)
    label.label_pairs(
        w.work, w.cache_dir, model="gpt-oss-120b", client=FakeClient(), workers=1, sample=1
    )
    assert records.read_label_run(w.work) == before


def test_run_prints_a_summary(w, monkeypatch, capsys):
    monkeypatch.setattr(llm, "Client", lambda model, **kw: FakeClient())
    assert label.run(args_for(w, "--reasoning-effort", "none", "--limit", "1")) == 0
    out = capsys.readouterr().out
    assert "label: calls: 1 (1 ok, 0 failed)" in out
    assert "no_evidence 6" in out
    assert all(line.startswith("label: ") for line in out.splitlines())


def test_cached_labels_survive_with_label_rows(w):
    """Labels written by an earlier run are never resent, whatever their value."""
    version = label.label_prompt()[1]
    with LabelCache(w.cache_dir / config.LABEL_CACHE) as cache:
        cache.put_labels(
            [LabelRow(TREATS.statement_id, "2001", version, MODEL, "confirmed", "q", True)]
        )
    client = FakeClient()
    run_label(w, client)
    assert len(client.sent) == 1 and "2001" not in client.sent[0].pmids
    assert w.labels()[(TREATS.statement_id, "2001")].label == "confirmed"


@pytest.mark.parametrize(
    "subject, predicate, object, expected",
    [
        # Chemical and disease: nothing.
        (["biolink:SmallMolecule"], "biolink:treats", ["biolink:Disease"], set()),
        (["biolink:Drug"], "biolink:contraindicated_in", ["biolink:Disease"], set()),
        # Gene and disease: subject qualifiers only, with qualified_predicate.
        (
            GENE,
            "biolink:gene_associated_with_condition",
            ["biolink:Disease"],
            {
                "qualified_predicate",
                "subject_aspect_qualifier",
                "subject_direction_qualifier",
                "subject_form_or_variant_qualifier",
            },
        ),
    ],
)
def test_offered_qualifiers_fit_the_entities(monkeypatch, subject, predicate, object, expected):
    monkeypatch.setattr(biolink, "allowed_qualifiers", REAL_ALLOWED)
    monkeypatch.setattr(biolink, "validate_qualifiers", REAL_VALIDATE)
    s = statement("A:1", predicate, "B:1")
    job = label.PairJob(
        "A:1|B:1",
        [Entity(id="A:1", category=subject), Entity(id="B:1", category=object)],
        {},
        {},
        [s],
        {s.statement_id: "x"},
        ["1001"],
    )
    q = label.Qualifiers()
    offered = set(q.offered(job, s))
    assert offered == expected
    assert not any(k.startswith("object_") for k in offered)
    if not expected:
        errors = q.errors(job, s, {"object_direction_qualifier": "decreased"})
        assert any("not among the qualifiers offered" in e for e in errors)


def test_chemical_gene_offers_both_sides(monkeypatch):
    monkeypatch.setattr(biolink, "allowed_qualifiers", REAL_ALLOWED)
    s = statement(IMATINIB, "biolink:affects", ABL1)
    job = label.PairJob(
        GENE_PAIR,
        [Entity(id=IMATINIB, category=["biolink:SmallMolecule"]), Entity(id=ABL1, category=GENE)],
        {},
        {},
        [s],
        {s.statement_id: "x"},
        ["1001"],
    )
    offered = label.Qualifiers().offered(job, s)
    assert {
        "object_aspect_qualifier",
        "subject_derivative_qualifier",
        "qualified_predicate",
    } <= set(offered)
    assert "anatomical_context_qualifier" not in offered


@pytest.mark.parametrize(
    "proposed, restates",
    [
        ({"subject_form_or_variant_qualifier": "genetic_variant_form"}, True),
        ({"subject_form_or_variant_qualifier": "mutant_form"}, True),
        ({"subject_form_or_variant_qualifier": "loss_of_function_variant_form"}, False),
        (
            {
                "subject_form_or_variant_qualifier": "genetic_variant_form",
                "subject_aspect_qualifier": "expression",
            },
            False,
        ),
    ],
)
def test_generic_variant_forms_restate_gene_to_disease_edges(proposed, restates):
    gene, disease = ["biolink:Gene"], ["biolink:Disease"]
    assert label._restates_variants(proposed, gene, disease) is restates
    # On a chemical acting on a mutant protein, the form is the point.
    assert not label._restates_variants(proposed, ["biolink:SmallMolecule"], gene)
