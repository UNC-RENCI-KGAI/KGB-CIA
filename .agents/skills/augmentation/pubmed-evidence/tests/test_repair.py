"""The quote repair pass (repair.py) on its own, with a fake repair model: code first, the model
for what code can't place, the priority order of the repairs that count, and label_run.json
files from before code repairs. The pass as the label step runs it is in test_label.py."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import FakeClient
from pubmed_evidence import config, llm, quote_align, records, repair
from pubmed_evidence.cache import (
    AbstractClaimsRow,
    ClaimCheckRow,
    ClaimRepairRow,
    LabelCache,
    PubMedCache,
    claim_id,
)

from shared.pubmed import Abstract

IMATINIB = "CHEBI:45783"
ABL1 = "NCBIGene:25"
PAIR = f"{IMATINIB}|{ABL1}"
PV, MODEL = "label@test", "gpt-6-luna"
NAMES = {IMATINIB: "imatinib", ABL1: "ABL1"}
SYNONYMS = {IMATINIB: ["Gleevec", "STI571"], ABL1: ["c-Abl"]}

FIRST = "STI571 inhibited the kinase in every leukemia cell line we tested."
SECOND = "Its target in these cells was c-Abl, as shown by pull-down."
ABSTRACTS = [
    Abstract("1", "Kinase inhibition in leukemia cells", f"{FIRST} {SECOND}", 2001),
    Abstract("2", "Binding studies", "Imatinib bound the ABL1 kinase domain in vitro.", 2002),
]
# Words dropped: code places it. Made up: under half its words are in the abstract.
NEAR = "STI571 inhibited the kinase in every leukemia cell line tested"
MADE_UP = "Imatinib was found to bind ABL1 tightly in every assay we ran"
REPLY = "Imatinib bound the ABL1 kinase domain"


def stored(sentence: str) -> dict:
    """A claim as abstract_claims stores it: imatinib decreases ABL1's activity."""
    return {
        "polarity": "positive",
        "statement": {
            "subject": IMATINIB,
            "predicate": "biolink:affects",
            "object": ABL1,
            "qualified_predicate": "biolink:causes",
            "qualifiers": {
                "object_aspect_qualifier": "activity",
                "object_direction_qualifier": "decreased",
            },
        },
        "also_supports": [],
        "claim_role": "primary_finding",
        "hedged": False,
        "negative_finding_type": None,
        "context": {"species": None, "anatomy": None, "notes": None},
        "evidence_sentence": sentence,
    }


@pytest.fixture
def labels(tmp_path: Path):
    """A label cache with one misquoted claim per abstract: PMID 1's near-verbatim, PMID 2's
    made up; and the abstracts in the test's PubMed cache."""
    with PubMedCache() as pubmed:
        pubmed.put_abstracts(ABSTRACTS)
    with LabelCache(tmp_path / "labels.sqlite") as cache:
        for pmid, sentence in (("1", NEAR), ("2", MADE_UP)):
            claim = stored(sentence)
            cache.put_abstract_claims(
                [AbstractClaimsRow(PAIR, pmid, PV, MODEL, "in_vitro", [claim])]
            )
            cache.put_claim_checks(
                [ClaimCheckRow(PAIR, pmid, PV, MODEL, claim_id(claim), True, False)]
            )
        yield cache


def key(pmid: str) -> tuple[str, str, str]:
    sentence = {"1": NEAR, "2": MADE_UP}[pmid]
    return (PAIR, pmid, claim_id(stored(sentence)))


def fixer(answer=None) -> FakeClient:
    usage = llm.Usage([llm.Attempt(config.REPAIR_MODEL, 300, 0, 0, 20)])
    return FakeClient(answer or (lambda sent: {"quote": REPLY}), usage)


def run(labels: LabelCache, client=None, synonyms=SYNONYMS) -> dict:
    with PubMedCache() as pubmed:
        return repair.repair_quotes(
            labels,
            pubmed,
            prompt_version=PV,
            model=MODEL,
            pair_ids=[PAIR],
            names=NAMES,
            synonyms=synonyms,
            client=client,
            workers=1,
        )


def rows(labels: LabelCache, repairer: tuple[str, str]) -> dict:
    return {(r.pair_id, r.pmid, r.claim_id): r for r in labels.claim_repairs(PV, MODEL, *repairer)}


# --- Code first, the model for the rest -----------------------------------------------------


def test_code_repairs_first_and_only_what_it_cant_place_goes_to_the_model(labels):
    client = fixer()
    s = run(labels, client)
    # One call, for the made-up quote; the near-verbatim one never goes to the model.
    [sent] = client.sent
    assert "PMID:2" in sent.user and json.dumps(MADE_UP) in sent.user
    code = rows(labels, repair.code_current())
    assert code[key("1")].evidence_sentence == f"{FIRST} {SECOND}"  # joined: it names c-Abl
    assert (code[key("1")].sentence_valid, code[key("1")].answered_by) == (True, "code")
    assert (code[key("2")].evidence_sentence, code[key("2")].sentence_valid) == (None, None)
    assert code[key("2")].answered_by == "code"
    assert set(rows(labels, repair.model_current())) == {key("2")}
    assert s["repairers"] == repair.current()
    assert {k: s[k] for k in ("pending", "code_tried", "code_found", "code_none", "for_model")} == {
        "pending": 2,
        "code_tried": 2,
        "code_found": 1,
        "code_none": 1,
        "for_model": 1,
    }
    assert (s["calls"], s["found"]) == (1, 1)
    assert repair.sentence_repairs(labels, PV, MODEL, repair.current()) == {
        key("1"): f"{FIRST} {SECOND}",
        key("2"): REPLY,
    }


def test_the_synonyms_given_are_what_code_looks_for(labels):
    run(labels, fixer(), synonyms={})
    # Without them neither entity is named in the abstract, and nothing can be joined.
    assert rows(labels, repair.code_current())[key("1")].evidence_sentence == FIRST


def test_rerunning_makes_no_new_calls_or_repairs(labels):
    run(labels, fixer())
    before = rows(labels, repair.code_current()) | rows(labels, repair.model_current())
    again = fixer()
    s = run(labels, again)
    assert again.sent == [] and s["pending"] == 0 and s["code_tried"] == 0
    assert rows(labels, repair.code_current()) | rows(labels, repair.model_current()) == before


def test_a_failed_call_is_retried_and_code_is_not_redone(labels):
    run(labels, fixer(lambda sent: llm.LLMError("boom")))
    assert rows(labels, repair.model_current()) == {}
    again = fixer()
    s = run(labels, again)
    assert len(again.sent) == 1 and (s["code_tried"], s["for_model"], s["found"]) == (0, 1, 1)


def test_without_the_models_credentials_the_code_repairs_still_count(labels, capsys):
    s = run(labels)
    assert s["skipped"] and s["repairers"] == [repair.code_current()]
    assert (s["code_found"], s["for_model"], s["calls"]) == (1, 1, 0)
    assert set(repair.sentence_repairs(labels, PV, MODEL, s["repairers"])) == {key("1")}
    repair.print_summary(s)
    out, err = capsys.readouterr()
    assert f"code ({quote_align.VERSION}) placed 1 of 2; 1 left for" in out
    assert "quote repair skipped" in err and "1 evidence sentences code couldn't place" in err


def test_code_wins_over_a_model_repair_an_earlier_run_made(labels):
    """A claim the model repaired before code repairs existed: code repairs it too, without a
    call, and the code's sentence counts; the model's stays cached as the fallback."""
    pair, pmid, claim = key("1")
    title = ABSTRACTS[0].title
    model_row = ClaimRepairRow(pair, pmid, PV, MODEL, claim, *repair.model_current(), title, True)
    labels.put_claim_repairs([model_row])
    client = fixer()
    run(labels, client)
    assert all("PMID:1" not in sent.user for sent in client.sent)
    found = repair.sentence_repairs(labels, PV, MODEL, repair.current())
    assert found[key("1")] == f"{FIRST} {SECOND}"
    model_first = list(reversed(repair.current()))
    assert (
        repair.sentence_repairs(labels, PV, MODEL, model_first)[key("1")]
        == model_row.evidence_sentence
    )


# --- Which repair counts --------------------------------------------------------------------


def put(labels: LabelCache, claim: str, by: tuple[str, str], sentence: str | None, valid) -> None:
    labels.put_claim_repairs([ClaimRepairRow(PAIR, "9", PV, MODEL, claim, *by, sentence, valid)])


def test_the_first_found_in_priority_order_counts(labels):
    code, model = repair.code_current(), ("terra", "repair@1")
    put(labels, "a" * 16, code, "code's sentence", True)
    put(labels, "a" * 16, model, "model's sentence", True)
    put(labels, "b" * 16, code, None, None)
    put(labels, "b" * 16, model, "model's sentence", True)
    put(labels, "c" * 16, code, "code's misquote", False)
    put(labels, "c" * 16, model, None, None)
    put(labels, "d" * 16, model, "model's sentence", True)  # code never tried it
    found = repair.sentence_repairs(labels, PV, MODEL, [code, model], [PAIR])
    assert {k[2][0]: v for k, v in found.items() if k[1] == "9"} == {
        "a": "code's sentence",
        "b": "model's sentence",
        "d": "model's sentence",
    }
    flipped = repair.sentence_repairs(labels, PV, MODEL, [model, code])
    assert flipped[(PAIR, "9", "a" * 16)] == "model's sentence"
    assert repair.sentence_repairs(labels, PV, MODEL, [code]) == {
        (PAIR, "9", "a" * 16): "code's sentence"
    }
    assert repair.sentence_repairs(labels, PV, MODEL, None) == {}
    assert repair.sentence_repairs(labels, PV, MODEL, []) == {}
    # With none found, the last made shows (the label sample's 'repair by ...').
    chosen = repair.chosen_repairs(labels, PV, MODEL, [code, model])
    assert chosen[(PAIR, "9", "c" * 16)].repairer == "terra"


# --- label_run.json -------------------------------------------------------------------------


def test_a_label_run_json_from_before_code_repairs_still_loads(tmp_path):
    old = {
        "model": MODEL,
        "prompt_version": PV,
        "prompt": "prompts/label.md",
        "reasoning_effort": "low",
        "reviewer": None,
        "review_prompt_version": None,
        "review_reasoning_effort": None,
        "repairer": "gpt-5.6-terra",
        "repair_prompt_version": "repair@0ld",
        "uberon_version": None,
    }
    (tmp_path / records.LABEL_RUN).write_text(json.dumps(old))
    run = records.read_label_run(tmp_path)
    assert run.repairers is None
    assert run.repair_order() == [("gpt-5.6-terra", "repair@0ld")]
    warning = repair.stale(run)
    assert "gpt-5.6-terra (repair@0ld)" in warning and f"code ({quote_align.VERSION})" in warning
    # Named in priority order, with the last in the old fields for readers from before.
    run.set_repairs(repair.current())
    records.write_label_run(tmp_path, run)
    again = records.read_label_run(tmp_path)
    assert again.repair_order() == repair.current() and repair.stale(again) is None
    assert (again.repairer, again.repair_prompt_version) == repair.model_current()
    written = json.loads((tmp_path / records.LABEL_RUN).read_text())
    assert written["repairers"] == [list(r) for r in repair.current()]
    # A run naming no repairs has nothing stale.
    run.set_repairs([])
    assert (run.repairers, run.repairer, run.repair_order(), repair.stale(run)) == (
        None,
        None,
        [],
        None,
    )


def _models(env: dict[str, str]) -> list:
    """[config.REVIEW_MODEL, config.REPAIR_MODEL, repair.current()] in a fresh interpreter with
    these environment variables and no other KGB_AUGMENT_ ones, since config reads them at
    import."""
    here = Path(__file__).resolve()
    code = (
        "import json; from pubmed_evidence import config, repair; "
        "print(json.dumps([config.REVIEW_MODEL, config.REPAIR_MODEL, repair.current()]))"
    )
    clean = {k: v for k, v in os.environ.items() if not k.startswith("KGB_AUGMENT_")}
    clean["PYTHONPATH"] = os.pathsep.join([str(here.parents[1] / "scripts"), str(here.parents[5])])
    done = subprocess.run(
        [sys.executable, "-c", code], env=clean | env, capture_output=True, text=True, check=True
    )
    return json.loads(done.stdout)


def test_the_repair_model_stays_terra_when_the_review_model_changes():
    """The repair model was the review model until gpt-6-luna began reviewing; it is its own
    setting now, so the repairs a label run counts don't follow the reviewer."""
    reviewer, repairer, current = _models({})
    assert (reviewer, repairer) == ("gpt-6-luna", "gpt-5.6-terra")
    assert [r for r, _ in current] == ["code", "gpt-5.6-terra"]
    assert _models({"KGB_AUGMENT_REVIEW_MODEL": "gpt-6.1-sol"}) == [
        "gpt-6.1-sol",
        repairer,
        current,
    ]
    assert _models({"KGB_AUGMENT_REPAIR_MODEL": "gpt-6.1-sol"})[:2] == [reviewer, "gpt-6.1-sol"]
