"""The review step (review.py): which claims go to the reviewer, in one pass, how they're
grouped and what is stored. Work files, caches and the input graph are built in code
(test_bundle's `World`); refinement and the Biolink lookups are real, the model is a fake.

The scene: imatinib `treats` CML and is `in_clinical_trials_for` it. Three placebo-controlled
trials say it doesn't treat CML, and also show it was tested in clinical trials for it; a fourth
abstract says only that. So all four would count for the trials edge: the negative claims
through what they also support (rule 8), the fourth adding only its PMID. Imatinib also
`affects` ABL1, plainly and as causing decreased activity of it; six in vitro claims in human
say the latter, so they support both edges. A seventh's sentence wasn't found, so it counts for
nothing and isn't sent.
"""

from __future__ import annotations

import argparse
import dataclasses
import re
from pathlib import Path

import pytest
from conftest import FakeClient, Sent
from pubmed_evidence import config, llm, records, repair, review
from pubmed_evidence.cache import ClaimRepairRow, Grounding, LabelCache, PubMedCache
from test_bundle import (
    GENE_PAIR,
    INHIBITS,
    MODEL,
    PV,
    TREATS,
    TREATS_PAIR,
    TRIALS,
    World,
    edge,
    new_world,
    statement,
    stored,
)
from toy import ABL1, CML, IMATINIB

REVIEWER = config.REVIEW_MODEL
NULL = "Imatinib was no better than placebo in chronic myeloid leukemia"
TESTED = "Imatinib was tested in a phase 2 trial in chronic myeloid leukemia"
INHIBITED = "Imatinib inhibited ABL1 kinase activity"
TRIALS_PMIDS = ("101", "102", "103", "104")
GENE_PMIDS = ("201", "202", "203", "204", "205", "206")

TREATS_EDGE = edge(IMATINIB, "biolink:treats", CML, id="treats:1")
TRIALS_EDGE = edge(IMATINIB, "biolink:in_clinical_trials_for", CML, id="trials:1")
PLAIN = edge(IMATINIB, "biolink:affects", ABL1, id="gene:plain")
QUALIFIED = edge(
    IMATINIB,
    "biolink:affects",
    ABL1,
    id="gene:qualified",
    qualified_predicate="biolink:causes",
    object_aspect_qualifier="activity",
    object_direction_qualifier="decreased",
)


@pytest.fixture
def scene(tmp_path: Path) -> World:
    w = new_world(tmp_path)
    w.edges(TREATS_EDGE, TRIALS_EDGE, PLAIN, QUALIFIED)
    for pmid in TRIALS_PMIDS[:3]:
        w.abstract(TREATS_PAIR, pmid, f"{NULL}.", types=["Randomized Controlled Trial"])
        negative = stored(
            TREATS, "negative", sentence=NULL, also=(TRIALS,), finding="demonstrated_absence"
        )
        w.answer(TREATS_PAIR, pmid, negative)
    w.abstract(TREATS_PAIR, "104", f"{TESTED}.", types=["Clinical Trial, Phase II"])
    w.answer(TREATS_PAIR, "104", stored(TRIALS, sentence=TESTED))
    for pmid in (*GENE_PMIDS, "207"):
        w.abstract(GENE_PAIR, pmid, f"{INHIBITED} in vitro.")
    for pmid in GENE_PMIDS[:5]:
        w.answer(GENE_PAIR, pmid, stored(INHIBITS, sentence=INHIBITED), design="in_vitro")
    # 206's sentence was repaired; 207's wasn't found, so it counts for nothing.
    misquoted = stored(INHIBITS, sentence="imatinib blocked ABL1")
    [fixed] = w.answer(GENE_PAIR, "206", misquoted, design="in_vitro", sentence_valid=False)
    w.answer(GENE_PAIR, "207", misquoted, design="in_vitro", sentence_valid=False)
    repairer, version = repair.current()
    with LabelCache(w.cache_dir / config.LABEL_CACHE) as cache:
        cache.put_claim_repairs(
            [ClaimRepairRow(GENE_PAIR, "206", PV, MODEL, fixed, repairer, version, INHIBITED, True)]
        )
    run = records.read_label_run(w.work)
    update = {"repairer": repairer, "repair_prompt_version": version}
    records.write_label_run(w.work, run.model_copy(update=update))
    return w


ITEM = re.compile(r"^### (R\d+): PMID:(\d+)$", re.M)


def reviewer(decide=None, fail=None) -> FakeClient:
    """A fake reviewer that upholds every claim unless `decide(pmid)` says otherwise (None
    leaves it out of the reply), and raises LLMError for a message `fail(user)` is true of."""
    decide = decide or (lambda pmid: "upheld")
    fail = fail or (lambda user: False)

    def answer(sent: Sent):
        if fail(sent.user):
            return llm.LLMError("HTTP 500 after retries")
        reviews = []
        for local, pmid in ITEM.findall(sent.user):
            verdict = decide(pmid)
            if verdict is not None:
                reviews.append({"item": local, "verdict": verdict, "reason": f"{verdict} {pmid}"})
        return {"reviews": reviews}

    usage = llm.Usage([llm.Attempt(REVIEWER, 800, 200, 0, 100)])
    return FakeClient(answer, usage)


def rejecting(*pmids: str) -> FakeClient:
    return reviewer(lambda pmid: "reject" if pmid in pmids else "upheld")


def sent_pmids(client: FakeClient) -> list[set[str]]:
    """The PMIDs of the claims in each call, in the order the calls were made."""
    return [{pmid for _, pmid in ITEM.findall(s.user)} for s in client.sent]


def run_review(w: World, client: FakeClient, **kw) -> dict:
    return review.review_claims(w.work, w.cache_dir, w.graph, client=client, workers=1, **kw)


def stored_reviews(w: World) -> dict[str, tuple[str, str, str]]:
    """PMID -> (verdict, review prompt version, who answered) of the stored reviews."""
    with LabelCache(w.cache_dir / config.LABEL_CACHE) as cache:
        rows = cache.claim_reviews(PV, MODEL, REVIEWER)
    return {r.pmid: (r.verdict, r.review_prompt_version, r.answered_by) for r in rows}


def counts(s: dict) -> tuple[int, ...]:
    """(claims_to_review, already_reviewed, sent, unreviewed, calls, calls_needed)."""
    keys = ("claims_to_review", "already_reviewed", "sent", "unreviewed", "calls", "calls_needed")
    return tuple(s[k] for k in keys)


def test_one_pass_sends_every_claim_that_would_count_and_has_no_verdict(scene):
    """The negative trials through the trials edge they also support, 104 though it only adds a
    PMID to that edge, and the six claims of the gene pair: one call per pair."""
    client = rejecting("101")
    s = run_review(scene, client)

    assert sent_pmids(client) == [set(TRIALS_PMIDS), set(GENE_PMIDS)]
    assert counts(s) == (10, 0, 10, 0, 2, 2)
    assert s["verdicts"] == {"upheld": 9, "reject": 1}
    assert (s["omitted"], s["repeated"]) == (0, 0)
    assert (s["pairs"], s["pairs_not_fully_labeled"], s["pairs_not_looked_up"]) == (2, 0, 0)
    assert s["ok"]
    assert s["cost"] == pytest.approx(2 * llm.cost_of(llm.model(REVIEWER), 800, 200, 0, 100))

    _, version = review.review_prompt()
    reviews = stored_reviews(scene)
    assert set(reviews) == {*TRIALS_PMIDS, *GENE_PMIDS}
    assert reviews["101"] == ("reject", version, REVIEWER)
    assert {v for p, v in reviews.items() if p != "101"} == {("upheld", version, REVIEWER)}
    with LabelCache(scene.cache_dir / config.LABEL_CACHE) as cache:
        [row] = [r for r in cache.claim_reviews(PV, MODEL) if r.pmid == "101"]
        usage = cache.usage("review", REVIEWER, version)
    assert row.reason == "reject 101" and row.pair_id == TREATS_PAIR
    assert usage["calls"] == 2 and usage["pairs"] == 2

    run = records.read_label_run(scene.work)
    assert (run.reviewer, run.review_prompt_version) == (REVIEWER, version)
    assert run.review_reasoning_effort == llm.model(REVIEWER).effort
    assert (run.model, run.prompt_version, run.repairer) == (MODEL, PV, config.REPAIR_MODEL)


def test_claims_that_would_not_count_are_not_sent(scene):
    """Rules 6, 9 and 10: a negative claim with nothing it also supports, a hypothesis, a
    claim from a retracted paper, a contrary of the qualified edge and the plain statement,
    which only an edge select left out makes, support no edge in scope or don't count."""
    scene.edges(TREATS_EDGE, TRIALS_EDGE, QUALIFIED, scope_out=(PLAIN,))
    for pmid in ("105", "106"):
        scene.abstract(TREATS_PAIR, pmid, f"{TESTED}.")
    scene.abstract(TREATS_PAIR, "107", f"{TESTED}.", types=["Retracted Publication"])
    scene.answer(TREATS_PAIR, "105", stored(TREATS, "negative", sentence=NULL))
    scene.answer(TREATS_PAIR, "106", stored(TRIALS, sentence=TESTED, role="hypothesis"))
    scene.answer(TREATS_PAIR, "107", stored(TRIALS, sentence=TESTED))
    raised = statement(
        IMATINIB,
        "biolink:affects",
        ABL1,
        "biolink:causes",
        object_aspect_qualifier="activity",
        object_direction_qualifier="increased",
    )
    plain = statement(IMATINIB, "biolink:affects", ABL1)
    for pmid, claim in (("208", raised), ("209", plain)):
        scene.abstract(GENE_PAIR, pmid, f"{INHIBITED}.")
        scene.answer(GENE_PAIR, pmid, stored(claim, sentence=INHIBITED))

    client = reviewer()
    s = run_review(scene, client)
    assert sent_pmids(client) == [set(TRIALS_PMIDS), set(GENE_PMIDS)]
    assert counts(s) == (10, 0, 10, 0, 2, 2)


def test_a_support_only_claim_is_sent_only_with_review_support_only(scene):
    """Rule 11: "imatinib ameliorates CML" only supports the treats edge, so it isn't sent
    unless `review_support_only` asks for it."""
    relieved = "Imatinib relieved the symptoms of chronic myeloid leukemia"
    scene.abstract(TREATS_PAIR, "105", f"{relieved}.")
    ameliorates = statement(IMATINIB, "biolink:ameliorates_condition", CML)
    scene.answer(TREATS_PAIR, "105", stored(ameliorates, sentence=relieved))
    client = reviewer()
    s = run_review(scene, client)
    assert sent_pmids(client) == [set(TRIALS_PMIDS), set(GENE_PMIDS)]
    assert counts(s) == (10, 0, 10, 0, 2, 2) and not s["review_support_only"]

    client = reviewer()
    s = run_review(scene, client, review_support_only=True)
    assert sent_pmids(client) == [{"105"}]
    assert counts(s) == (1, 10, 1, 0, 1, 3) and s["review_support_only"]


def test_identical_claims_of_an_abstract_are_sent_once(scene):
    """They share a key, and so a verdict."""
    scene.abstract(TREATS_PAIR, "105", f"{TESTED}.")
    claim = stored(TRIALS, sentence=TESTED)
    scene.answer(TREATS_PAIR, "105", claim, claim)
    client = reviewer()
    s = run_review(scene, client)
    assert client.sent[0].user.count("### R") == 5
    assert counts(s) == (11, 0, 11, 0, 2, 2)


def test_a_rerun_sends_only_claims_with_no_verdict(scene):
    """A rejected claim would still count if upheld, but it has its verdict: it is reviewed
    before, not to review, and isn't sent again."""
    run_review(scene, rejecting("101"))
    again = reviewer()
    s = run_review(scene, again)
    assert again.sent == []
    assert counts(s) == (0, 10, 0, 0, 0, 2) and s["ok"]
    assert s["verdicts"] == {"upheld": 0, "reject": 0}


def test_a_new_prompt_or_effort_reviews_afresh(scene, tmp_path):
    run_review(scene, reviewer())
    edited = tmp_path / "review.md"
    edited.write_text(config.REVIEW_PROMPT.read_text() + "\nBe brief.\n", encoding="utf-8")
    fresh = reviewer()
    run_review(scene, fresh, prompt=edited)
    assert sent_pmids(fresh) == [set(TRIALS_PMIDS), set(GENE_PMIDS)]

    higher = reviewer()
    s = run_review(scene, higher, reasoning_effort="high")
    assert len(higher.sent) == 2 and s["reasoning_effort"] == "high"
    assert s["review_prompt_version"] != review.review_prompt()[1]
    run = records.read_label_run(scene.work)
    assert (run.review_prompt_version, run.review_reasoning_effort) == (
        s["review_prompt_version"],
        "high",
    )
    with LabelCache(scene.cache_dir / config.LABEL_CACHE) as cache:
        versions = {r.review_prompt_version for r in cache.claim_reviews(PV, MODEL)}
    assert len(versions) == 3


def test_after_a_failed_call_its_claims_stay_unreviewed_and_the_next_run_sends_them(scene):
    client = reviewer(fail=lambda user: "PMID:101" in user)
    s = run_review(scene, client)
    assert sent_pmids(client) == [set(TRIALS_PMIDS), set(GENE_PMIDS)]
    assert s["failed_calls"] == 1 and s["unreviewed"] == 4 and not s["ok"]
    assert records.read_label_run(scene.work).reviewer is None  # a failed pass isn't named
    assert set(stored_reviews(scene)) == set(GENE_PMIDS)

    retry = reviewer()
    s = run_review(scene, retry)
    assert sent_pmids(retry) == [set(TRIALS_PMIDS)]
    assert counts(s) == (4, 6, 4, 0, 1, 2) and s["ok"]
    assert records.read_label_run(scene.work).reviewer == REVIEWER


def test_failures_are_a_share_of_every_call_the_claims_that_would_count_need(scene, monkeypatch):
    """A rerun that sends only a call that keeps failing is judged on every call the claims
    that would count need, those reviewed before included, so a stuck call doesn't fail every
    later run. (Here a share of 50% passes, so one failed call in two is allowed.)"""
    monkeypatch.setattr(llm, "MAX_FAILED_SHARE", 0.5)
    for _ in range(2):
        s = run_review(scene, reviewer(fail=lambda user: "PMID:101" in user))
        assert s["failed_calls"] == 1 and s["calls_needed"] == 2 and s["ok"]
    assert s["calls"] == 1 and s["unreviewed"] == 4
    monkeypatch.setattr(llm, "MAX_FAILED_SHARE", 0.4)
    assert not run_review(scene, reviewer(fail=lambda user: "PMID:101" in user))["ok"]


def test_a_verdict_outside_the_schema_fails_its_call_only(scene):
    """A reply with a verdict REPLY_SCHEMA doesn't allow (strict mode should prevent it) is a
    failed call, like any unreadable reply, not the end of the step."""
    client = reviewer(lambda pmid: "accept" if pmid in TRIALS_PMIDS else "upheld")
    s = run_review(scene, client)
    assert s["calls"] == 2 and s["failed_calls"] == 1
    assert s["verdicts"] == {"upheld": 6, "reject": 0}
    assert set(stored_reviews(scene)) == set(GENE_PMIDS)


def test_claims_left_out_of_a_reply_wait_for_the_next_run(scene):
    client = reviewer(lambda pmid: None if pmid == "102" else "upheld")
    s = run_review(scene, client)
    assert s["omitted"] == 1 and s["unreviewed"] == 1 and s["ok"]
    assert "102" not in stored_reviews(scene)

    retry = reviewer()
    s = run_review(scene, retry)
    assert sent_pmids(retry) == [{"102"}] and s["unreviewed"] == 0


def test_limit_reviews_the_first_pairs_by_id(scene):
    client = reviewer()
    s = run_review(scene, client, limit=1)
    assert TREATS_PAIR < GENE_PAIR
    assert sent_pmids(client) == [set(TRIALS_PMIDS)] and s["pairs"] == 1


def test_a_pair_with_anatomy_never_looked_up_is_counted_and_not_sent(scene):
    """Its claims can't be told apart by context yet: step 6 holds the pair, and the next
    ground run looks the name up."""
    scene.abstract(GENE_PAIR, "208", f"{INHIBITED} in the liver.")
    scene.answer(GENE_PAIR, "208", stored(INHIBITS, sentence=INHIBITED, anatomy="liver"))
    client = reviewer()
    s = run_review(scene, client)
    assert sent_pmids(client) == [set(TRIALS_PMIDS)]
    assert (s["pairs"], s["pairs_not_looked_up"]) == (1, 1)

    with PubMedCache() as cache:
        cache.put_groundings([Grounding("liver", "anatomy", None, None, "test")])
    again = reviewer()
    s = run_review(scene, again)
    assert sent_pmids(again) == [{*GENE_PMIDS, "208"}] and s["pairs_not_looked_up"] == 0


def test_no_claim_that_would_count_no_client(scene, monkeypatch):
    scene.abstract(TREATS_PAIR, "106", "x")  # never answered: the pair isn't fully labeled
    built = []
    monkeypatch.setattr(llm, "client_for", lambda *a, **kw: built.append(a))
    s = review.review_claims(scene.work, scene.cache_dir, scene.graph, limit=1)
    assert built == [] and s["calls"] == 0 and s["pairs"] == 0
    assert s["pairs_not_fully_labeled"] == 1 and s["ok"]
    assert records.read_label_run(scene.work).reviewer == REVIEWER

    # Nor once every claim that would count has its verdict.
    run_review(scene, reviewer())
    s = review.review_claims(scene.work, scene.cache_dir, scene.graph)
    assert built == [] and s["calls"] == 0 and s["already_reviewed"] == 6 and s["ok"]


def _call_with(client: FakeClient, pmid: str) -> str:
    [user] = [s.user for s in client.sent if f"### PMID:{pmid}, " in s.user]
    return user


def test_message_shows_each_claim_in_words_and_structured_and_each_abstract_once(scene):
    client = reviewer()
    run_review(scene, client)

    trials = _call_with(client, "101")
    assert f"- {IMATINIB}: imatinib (SmallMolecule); other names: Gleevec; STI571" in trials
    claim = trials.split("### R1: PMID:101\n")[1].split("\n\n")[0].splitlines()
    cml = "chronic myelogenous leukemia, BCR-ABL1 positive"
    assert claim[0] == f"Claim: imatinib does not treat {cml}"
    assert claim[1] == f"Also supports: imatinib has been tested in clinical trials for {cml}"
    assert claim[2].startswith('Structured: {"polarity": "negative", "statement": {"subject": ')
    for part in (
        '"claim_role": "primary_finding"',
        '"hedged": false',
        '"negative_finding_type": "demonstrated_absence"',
        '"context": {"species": "human", "anatomy": null, "notes": null}',
        '"predicate": "biolink:in_clinical_trials_for"',
    ):
        assert part in claim[2]
    assert claim[3] == f'Evidence sentence: "{NULL}"'
    assert trials.count("### PMID:101, 2020\n") == 1
    assert "Publication types: Randomized Controlled Trial\n" in trials
    assert f"Abstract: {NULL}." in trials
    assert "### PMID:104, 2020\n" in trials and "PMID:201" not in trials

    gene = _call_with(client, "206")
    assert gene.count("### R") == 6 and "PMID:207" not in gene
    assert 'Evidence sentence: "imatinib blocked ABL1"' not in gene  # the repair is shown
    assert gene.count(f'Evidence sentence: "{INHIBITED}"') == 6
    assert "Claim: imatinib causes decreased activity of ABL1\n" in gene
    assert all(s.schema == review.REPLY_SCHEMA for s in client.sent)
    assert all(s.system == review.review_prompt()[0] for s in client.sent)


def test_batches_split_by_abstracts_and_items(monkeypatch):
    monkeypatch.setattr(config, "REVIEW_ITEMS_PER_CALL", 3)
    monkeypatch.setattr(config, "ABSTRACTS_PER_CALL", 2)
    record = None
    items = [review.Item(("a|b", pmid, f"c{i}"), record) for i in range(4) for pmid in "12"]
    items.append(review.Item(("a|b", "3", "c0"), record))
    out = review.batches({"a|b": items})
    assert [[i.pmid for _, i in batch] for _, batch in out] == [
        ["1", "2", "1"],
        ["2", "1", "2"],
        ["1", "2"],
        ["3"],
    ]
    assert [local for local, _ in out[1][1]] == ["R1", "R2", "R3"]
    assert {pair for pair, _ in out} == {"a|b"}


def test_a_claims_first_verdict_counts_and_unknown_items_fail():
    item = review.Item(("a|b", "1", "c"), None)
    reply = {
        "reviews": [
            {"item": "R1", "verdict": "upheld", "reason": " ok "},
            {"item": "R1", "verdict": "reject", "reason": "the first counts"},
        ]
    }
    assert review.parse_reply(reply, {"R1": item}) == ({"R1": ("upheld", "ok")}, 1)
    reply["reviews"].append({"item": "R9", "verdict": "reject", "reason": "no such item"})
    with pytest.raises(ValueError, match="beyond the request's 1: R9"):
        review.parse_reply(reply, {"R1": item})
    # A verdict the schema doesn't have, from a deployment that doesn't enforce it.
    reply = {"reviews": [{"item": "R1", "verdict": "accept", "reason": "old vocabulary"}]}
    with pytest.raises(ValueError, match="verdicts not upheld or reject: accept"):
        review.parse_reply(reply, {"R1": item})
    verdict = review.REPLY_SCHEMA.body["properties"]["reviews"]["items"]["properties"]["verdict"]
    assert verdict["enum"] == ["upheld", "reject"]


def test_the_review_prompt_includes_the_claim_rules_and_its_version_the_effort():
    system, version = review.review_prompt()
    assert "{{include" not in system
    assert (config.PROMPTS_DIR / "claim-rules.md").read_text().strip() in system
    assert review.review_prompt(reasoning_effort="medium")[1] == version  # the model's own
    assert review.review_prompt(reasoning_effort="high")[1] != version
    assert review.review_prompt(reasoning_effort="none")[1] != version
    with pytest.raises(llm.LLMError, match="not-a-model"):
        review.review_prompt(model="not-a-model")


def test_the_review_prompt_rejects_a_claim_posed_only_as_a_hypothesis():
    """Rule 10's review half: whatever its role, a claim the abstract only poses as a hypothesis
    or a question is rejected, while a hedged finding or asserted background is not one."""
    system = " ".join(review.review_prompt()[0].split())
    bullet = (
        "- the abstract only poses it as a hypothesis or a question, whatever its `claim_role`, "
        "`hypothesis` included (a hedged finding, or background the abstract asserts, is not a "
        "hypothesis);"
    )
    assert system.index("Reject it when:") < system.index(bullet)


def test_the_repair_prompt_version_covers_the_repair_models_effort(monkeypatch):
    _, version = repair.repair_prompt()
    model = llm.model(config.REPAIR_MODEL)
    monkeypatch.setitem(config.MODELS, model.id, dataclasses.replace(model, effort="high"))
    assert repair.repair_prompt()[1] != version
    assert repair.current() == (config.REPAIR_MODEL, repair.repair_prompt()[1])


# --- The CLI --------------------------------------------------------------------------------


def _args(w: World, *argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    review.add_arguments(parser)
    args = parser.parse_args(list(argv))
    args.work, args.cache_dir, args.input = w.work, w.cache_dir, w.graph
    return args


def test_run_returns_1_when_too_many_calls_fail(scene, monkeypatch):
    client = reviewer(fail=lambda user: "PMID:201" in user)
    built: list[tuple[str, dict]] = []

    def build(model, **kw):
        built.append((model.id, kw))
        return client

    monkeypatch.setattr(llm, "client_for", build)
    assert review.run(_args(scene, "--reasoning-effort", "none")) == 1
    assert built == [(REVIEWER, {"max_output_tokens": None, "reasoning_effort": "none"})]
    with LabelCache(scene.cache_dir / config.LABEL_CACHE) as cache:
        errors = cache.db.execute("SELECT pair_id, error FROM calls WHERE error IS NOT NULL")
        assert [tuple(r) for r in errors] == [(GENE_PAIR, "HTTP 500 after retries")]

    client.answer = reviewer().answer
    assert review.run(_args(scene)) == 0
    assert built[-1] == (REVIEWER, {"max_output_tokens": None, "reasoning_effort": None})
    assert records.read_label_run(scene.work).reviewer == REVIEWER


def test_run_fails_cleanly_without_inputs(tmp_path, scene, monkeypatch, capsys):
    empty = World(tmp_path / "empty", scene.cache_dir, scene.graph)
    assert review.run(_args(empty)) == 1  # no label_run.json
    assert "kgb-augment label" in capsys.readouterr().err
    assert review.run(_args(scene)) == 1  # no API key
    assert config.AZURE_KEY_VAR in capsys.readouterr().err
    assert review.run(_args(scene, "--model", "not-a-model")) == 1
    assert "not-a-model is not in config.MODELS" in capsys.readouterr().err
    (scene.cache_dir / config.UBERON_FILE).unlink()
    assert review.run(_args(scene)) == 1
    assert "kgb-augment ground" in capsys.readouterr().err


def test_run_prints_a_summary(scene, monkeypatch, capsys):
    monkeypatch.setattr(llm, "client_for", lambda *a, **kw: rejecting("101"))
    assert review.run(_args(scene)) == 0
    out = capsys.readouterr().out
    assert "review: 2 pairs; 0 not fully labeled" in out
    assert "0 with anatomy names never looked up (left for a ground run)" in out
    assert "10 claims that would count to review, 0 reviewed before; 10 sent: 9 upheld" in out
    assert "review: 2 calls, 0 failed" in out


def test_run_warns_of_claims_left_unreviewed(scene, monkeypatch, capsys):
    client = reviewer(fail=lambda user: "PMID:201" in user)
    monkeypatch.setattr(llm, "client_for", lambda *a, **kw: client)
    assert review.run(_args(scene)) == 1
    err = capsys.readouterr().err
    assert "6 claims that would count still have no verdict; step 6 holds their pairs" in err
    assert "1 of the 2 calls the claims that would count need failed" in err
