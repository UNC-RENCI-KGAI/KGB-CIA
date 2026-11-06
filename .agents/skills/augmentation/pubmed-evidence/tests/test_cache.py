"""The caches' tables: an older cache gets the columns added since, and rows read back as
written."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from pubmed_evidence.cache import (
    Abstract,
    AbstractClaimsRow,
    CallRow,
    ClaimCheckRow,
    ClaimRepairRow,
    ClaimReviewRow,
    Grounding,
    LabelCache,
    PubMedCache,
    claim_id,
)

# The abstracts table as the first release created it.
OLD_ABSTRACTS = """
CREATE TABLE abstracts (
    pmid TEXT PRIMARY KEY, title TEXT NOT NULL, abstract TEXT NOT NULL,
    year INTEGER, retrieved TEXT NOT NULL);
INSERT INTO abstracts VALUES ('1', 'Old title', 'Old text.', 2001, '2026-11-01T00:00:00+00:00');
"""


def test_old_pubmed_cache_gains_publication_types(tmp_path: Path):
    path = tmp_path / "pubmed.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(OLD_ABSTRACTS)
    with PubMedCache(path) as cache:
        [old] = cache.get_abstracts(["1"]).values()
        assert (old.title, old.year, old.publication_types) == ("Old title", 2001, None)
        assert cache.missing_abstracts(["1", "2"]) == ["1", "2"]
        cache.put_abstracts(
            [
                Abstract("1", "Old title", "Old text.", 2001, publication_types=["Review"]),
                Abstract("2", "", publication_types=[]),
            ]
        )
    with PubMedCache(path) as cache:  # opening it again adds nothing twice
        got = cache.get_abstracts(["1", "2"])
        assert got["1"].publication_types == ["Review"] and got["1"].abstract == "Old text."
        assert got["2"].publication_types == [] and not got["2"].has_text
        assert cache.missing_abstracts(["1", "2"]) == []


# The label cache's tables as the first release created them, with a row each.
OLD_LABELS = """
CREATE TABLE labels (
    statement_id TEXT NOT NULL, pmid TEXT NOT NULL, prompt_version TEXT NOT NULL,
    model TEXT NOT NULL, label TEXT NOT NULL, quote TEXT, quote_valid INTEGER, claim TEXT,
    created TEXT NOT NULL,
    PRIMARY KEY (statement_id, pmid, prompt_version, model));
CREATE TABLE proposals (
    statement_id TEXT NOT NULL, pmid TEXT NOT NULL, prompt_version TEXT NOT NULL,
    model TEXT NOT NULL, qualifiers TEXT NOT NULL, quote TEXT, quote_valid INTEGER,
    valid INTEGER, errors TEXT NOT NULL, claim TEXT, created TEXT NOT NULL,
    PRIMARY KEY (statement_id, pmid, prompt_version, model, qualifiers));
CREATE TABLE reviews (
    kind TEXT NOT NULL, statement_id TEXT NOT NULL, pmid TEXT NOT NULL,
    prompt_version TEXT NOT NULL, model TEXT NOT NULL, qualifiers TEXT NOT NULL,
    reviewer TEXT NOT NULL, review_prompt_version TEXT NOT NULL, verdict TEXT NOT NULL,
    reason TEXT NOT NULL, created TEXT NOT NULL,
    PRIMARY KEY (kind, statement_id, pmid, prompt_version, model, qualifiers, reviewer,
                 review_prompt_version));
CREATE TABLE calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, step TEXT NOT NULL, pair_id TEXT NOT NULL,
    model TEXT NOT NULL, prompt_version TEXT NOT NULL, input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL, cost REAL, seconds REAL NOT NULL, error TEXT,
    created TEXT NOT NULL);
INSERT INTO labels VALUES ('s1', '1', 'label@1', 'm', 'confirmed', 'q', 1, 'causal', 't');
INSERT INTO proposals VALUES ('s1', '1', 'label@1', 'm', '{}', 'q', 1, 1, '[]', NULL, 't');
INSERT INTO reviews VALUES ('contradicted', 's1', '2', 'label@1', 'm', '', 'r', 'review@1',
    'accept', 'why', 't');
INSERT INTO calls (step, pair_id, model, prompt_version, input_tokens, output_tokens, cost,
    seconds, error, created) VALUES ('label', 'a|b', 'm', 'label@1', 900, 100, 0.01, 1.0, NULL,
    't');
"""


def test_old_label_cache_gains_the_new_columns(tmp_path: Path):
    path = tmp_path / "labels.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(OLD_LABELS)
    with LabelCache(path) as cache:
        usage = cache.usage("label", "m", "label@1")
        assert (usage["calls"], usage["input_tokens"], usage["cache_read_tokens"]) == (1, 900, 0)
        # New rows go into the old table, whatever its column order.
        cache.log_call(CallRow("label", "a|c", "m", "label@1", 500, 50, cache_read_tokens=400))
        assert cache.usage("label", "m", "label@1")["cache_read_tokens"] == 400


def test_call_rows_round_trip(tmp_path: Path):
    call = CallRow(
        "review",
        "a|b",
        "r",
        "review@1",
        input_tokens=1200,
        output_tokens=300,
        cost=0.02,
        cache_read_tokens=800,
        cache_write_tokens=100,
        answered_by="r",
    )
    with LabelCache(tmp_path / "labels.sqlite") as cache:
        cache.log_call(call)
    with LabelCache(tmp_path / "labels.sqlite") as cache:
        usage = cache.usage("review", "r", "review@1")
        assert (usage["input_tokens"], usage["cache_read_tokens"]) == (1200, 800)
        assert (usage["cache_write_tokens"], usage["output_tokens"]) == (100, 300)
        [answered] = cache.db.execute("SELECT answered_by FROM calls")
        assert answered["answered_by"] == "r"


# --- The claim tables and groundings --------------------------------------------------------

PAIR = "CHEBI:45783|MONDO:0005148"
STAMP = "2026-11-05T00:00:00+00:00"


def statement(predicate: str, subject="CHEBI:45783", object="MONDO:0005148") -> dict:
    return {
        "subject": subject,
        "object": object,
        "predicate": predicate,
        "qualified_predicate": None,
        "qualifiers": {},
    }


# A claim as the label step stores it: the reply's, with the pair's CURIEs for X and Y.
CLAIM = {
    "polarity": "negative",
    "statement": statement("biolink:treats"),
    "also_supports": [statement("biolink:in_clinical_trials_for")],
    "claim_role": "primary_finding",
    "hedged": False,
    "negative_finding_type": "demonstrated_absence",
    "context": {"species": "human", "anatomy": "pancreatic β-cells", "notes": "phase 3"},
    "evidence_sentence": "Imatinib did not improve HbA1c compared with placebo.",
}


def test_claim_id_hashes_the_claim_as_stored():
    expected = json.dumps(CLAIM, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert claim_id(CLAIM) == hashlib.sha256(expected.encode("utf-8")).hexdigest()[:16]
    assert claim_id(dict(reversed(list(CLAIM.items())))) == claim_id(CLAIM)  # key order
    changed = copy.deepcopy(CLAIM)
    changed["context"]["anatomy"] = "UBERON:0000006"  # grounding the stored claim changes it
    assert claim_id(changed) != claim_id(CLAIM)
    swapped = copy.deepcopy(CLAIM)
    swapped["statement"] = statement("biolink:treats", "MONDO:0005148", "CHEBI:45783")
    assert claim_id(swapped) != claim_id(CLAIM)


@pytest.mark.parametrize(
    "where",
    [
        lambda c: c["statement"].update(subject="X"),
        lambda c: c["statement"].update(object="Y"),
        lambda c: c["statement"].pop("object"),
        lambda c: c["also_supports"][0].update(subject="Y", object="X"),
        lambda c: c.pop("statement"),
    ],
)
def test_claim_id_needs_curies_for_subject_and_object(where):
    claim = copy.deepcopy(CLAIM)
    where(claim)
    with pytest.raises(ValueError, match="CURIEs"):
        claim_id(claim)


# The keys of a claim row beyond the claim: its label run, and a verdict's or repair's own.
RUN = {
    "prompt_version": "label@1",
    "model": "m",
    "reviewer": "r",
    "review_prompt_version": "review@1",
    "repairer": "r",
    "repair_prompt_version": "repair@1",
}


def claim_rows(**keys):
    """A row for each claim table, about CLAIM in PMID 1 of PAIR, under RUN's keys or these."""
    k = RUN | keys
    cid = claim_id(CLAIM)
    run = (PAIR, "1", k["prompt_version"], k["model"])
    return (
        AbstractClaimsRow(*run, "rct", [CLAIM], STAMP, "m-fallback"),
        ClaimCheckRow(*run, cid, valid=True, sentence_valid=False),
        ClaimReviewRow(
            *run,
            cid,
            k["reviewer"],
            k["review_prompt_version"],
            "reject",
            "the trial was of a different drug",
            STAMP,
        ),
        ClaimRepairRow(
            *run,
            cid,
            k["repairer"],
            k["repair_prompt_version"],
            "Imatinib did not improve HbA1c",
            True,
            STAMP,
            "r",
        ),
    )


def put_claim_rows(cache: LabelCache, rows) -> None:
    claims, check, review, repair = rows
    cache.put_abstract_claims([claims])
    cache.put_claim_checks([check])
    cache.put_claim_reviews([review])
    cache.put_claim_repairs([repair])


def read_claim_rows(cache: LabelCache, **keys):
    """Each claim table's rows under RUN's keys or these."""
    k = RUN | keys
    run = (k["prompt_version"], k["model"])
    return (
        cache.abstract_claims(*run),
        cache.claim_checks(*run),
        cache.claim_reviews(*run, k["reviewer"], k["review_prompt_version"]),
        cache.claim_repairs(*run, k["repairer"], k["repair_prompt_version"]),
    )


def test_claim_rows_round_trip(tmp_path: Path):
    rows = claim_rows()
    no_claim = AbstractClaimsRow(PAIR, "2", "label@1", "m", "narrative_review", [], STAMP)
    failed = ClaimCheckRow(
        PAIR, "1", "label@1", "m", "0" * 16, False, True, ["object_aspect_qualifier: kinase"]
    )
    unrepaired = ClaimRepairRow(PAIR, "1", "label@1", "m", "0" * 16, "r", "repair@1", None)
    unrepaired.created = STAMP
    other = AbstractClaimsRow("CHEBI:1|MONDO:1", "3", "label@1", "m", "animal", [], STAMP)
    with LabelCache(tmp_path / "labels.sqlite") as cache:
        put_claim_rows(cache, rows)
        cache.put_abstract_claims([no_claim, other])
        cache.put_claim_checks([failed])
        cache.put_claim_repairs([unrepaired])
    with LabelCache(tmp_path / "labels.sqlite") as cache:
        claims, checks, reviews, repairs = read_claim_rows(cache)
        assert sorted(claims, key=lambda r: r.pmid) == [rows[0], no_claim, other]
        assert claims[0].claims[0]["context"]["anatomy"] == "pancreatic β-cells"
        assert sorted(checks, key=lambda r: r.claim_id) == [failed, rows[1]]
        assert reviews == [rows[2]]
        assert sorted(repairs, key=lambda r: r.claim_id) == [unrepaired, rows[3]]
        # Each reader takes some pairs only.
        assert cache.abstract_claims("label@1", "m", ["CHEBI:1|MONDO:1"]) == [other]
        assert cache.answered("label@1", "m", [PAIR]) == {(PAIR, "1"), (PAIR, "2")}
        assert cache.claim_checks("label@1", "m", ["CHEBI:1|MONDO:1"]) == []
        assert cache.claim_reviews("label@1", "m", pair_ids=[PAIR]) == [rows[2]]
        assert cache.claim_repairs("label@1", "m", "r", "repair@1", []) == []


def test_claim_tables_keep_each_run_apart(tmp_path: Path):
    """A row is keyed by its label run (prompt version and model), and a verdict or repair by
    its own model and prompt version too; a row with the same key replaces the old one."""
    runs = [
        {},
        {"prompt_version": "label@2"},
        {"model": "m2"},
        {"reviewer": "r2", "repairer": "r2"},
        {"review_prompt_version": "review@2", "repair_prompt_version": "repair@2"},
    ]
    with LabelCache(tmp_path / "labels.sqlite") as cache:
        for keys in runs:
            put_claim_rows(cache, claim_rows(**keys))
        assert cache.db.execute("SELECT COUNT(*) FROM abstract_claims").fetchone()[0] == 3
        assert cache.db.execute("SELECT COUNT(*) FROM claim_checks").fetchone()[0] == 3
        assert cache.db.execute("SELECT COUNT(*) FROM claim_reviews").fetchone()[0] == 5
        assert cache.db.execute("SELECT COUNT(*) FROM claim_repairs").fetchone()[0] == 5
        for keys in runs:
            assert read_claim_rows(cache, **keys) == tuple([row] for row in claim_rows(**keys))
        assert len(cache.claim_reviews("label@1", "m")) == 3  # every reviewer and version
        # The same claim answered again in the same run, reviewed again by the same reviewer.
        claims, check, review, repair = claim_rows()
        claims.study_design, check.valid, review.verdict = "cohort", False, "upheld"
        put_claim_rows(cache, (claims, check, review, repair))
        assert read_claim_rows(cache) == ([claims], [check], [review], [repair])


# A label cache from before the claim tables, with a row in each of its tables. They stay on
# disk as they are, unread once their code is gone.
BEFORE_CLAIMS = (
    OLD_LABELS
    + """
CREATE TABLE repairs (
    kind TEXT NOT NULL, statement_id TEXT NOT NULL, pmid TEXT NOT NULL,
    prompt_version TEXT NOT NULL, model TEXT NOT NULL, qualifiers TEXT NOT NULL,
    repairer TEXT NOT NULL, repair_prompt_version TEXT NOT NULL, quote TEXT,
    quote_valid INTEGER, created TEXT NOT NULL, answered_by TEXT,
    PRIMARY KEY (kind, statement_id, pmid, prompt_version, model, qualifiers, repairer,
                 repair_prompt_version));
INSERT INTO repairs VALUES ('label', 's1', '1', 'label@1', 'm', '', 'r', 'repair@1', NULL, NULL,
    't', 'r');
"""
)


def snapshot(path: Path) -> dict[str, tuple[list[str], list[tuple]]]:
    """Every table's columns and rows, in the order they were written."""
    db = sqlite3.connect(path)
    try:
        tables = [
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name != 'sqlite_sequence'"
            )
        ]
        out = {}
        for table in tables:
            columns = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
            rows = db.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY rowid")
            out[table] = (columns, rows.fetchall())
        return out
    finally:
        db.close()


def test_old_label_cache_gains_the_claim_tables_and_keeps_its_own(tmp_path: Path):
    path = tmp_path / "labels.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(BEFORE_CLAIMS)
    raw = snapshot(path)
    with LabelCache(path) as cache:
        assert read_claim_rows(cache) == ([], [], [], [])
    before = snapshot(path)  # the old tables once they gained their added columns
    assert {"labels", "proposals", "reviews", "repairs", "calls"} <= before.keys()
    # Only calls gains columns; the old statement tables are left exactly as they are.
    for table in ("labels", "proposals", "reviews", "repairs"):
        assert before[table] == raw[table]
    rows = claim_rows()
    with LabelCache(path) as cache:
        put_claim_rows(cache, rows)
    with LabelCache(path) as cache:  # opening it again adds nothing twice
        assert read_claim_rows(cache) == tuple([row] for row in rows)
    after = snapshot(path)
    for table in ("labels", "proposals", "reviews", "repairs", "calls"):
        assert after[table] == before[table]
    for table in ("abstract_claims", "claim_checks", "claim_reviews", "claim_repairs"):
        assert len(after[table][1]) == 1


def test_a_new_label_cache_has_none_of_the_old_tables(tmp_path: Path):
    path = tmp_path / "labels.sqlite"
    LabelCache(path).close()
    tables = snapshot(path).keys()
    assert {"calls", "abstract_claims", "claim_checks", "claim_reviews", "claim_repairs"} <= tables
    assert not tables & {"labels", "proposals", "reviews", "repairs"}


def test_claim_prompt_versions_are_a_models_label_runs_oldest_first(tmp_path: Path):
    with LabelCache(tmp_path / "labels.sqlite") as cache:
        for pv, model, created in [
            ("label@b", "m", "2026-11-03"),
            ("label@a", "m", "2026-11-04"),
            ("label@b", "m", "2026-11-01"),  # its first answer makes it the oldest
            ("label@c", "m2", "2026-11-02"),
        ]:
            pmid = str(len(cache.abstract_claims(pv, model)) + 1)
            cache.put_abstract_claims(
                [AbstractClaimsRow(PAIR, pmid, pv, model, "rct", [], created)]
            )
        assert cache.claim_prompt_versions("m") == ["label@b", "label@a"]
        assert cache.claim_prompt_versions("m2") == ["label@c"]
        assert cache.claim_prompt_versions("nobody") == []


# The PubMed cache as the release before groundings left it, with a row or two.
BEFORE_GROUNDINGS = """
CREATE TABLE searches (
    query TEXT PRIMARY KEY, retrieved TEXT NOT NULL, total INTEGER NOT NULL,
    pmids TEXT NOT NULL);
CREATE TABLE abstracts (
    pmid TEXT PRIMARY KEY, title TEXT NOT NULL, abstract TEXT NOT NULL,
    year INTEGER, retrieved TEXT NOT NULL, publication_types TEXT);
CREATE TABLE nodenorm (
    curie TEXT PRIMARY KEY, entry TEXT, retrieved TEXT NOT NULL);
INSERT INTO abstracts VALUES ('1', 'Title', 'Text.', 2001, 't', '["Review"]');
INSERT INTO nodenorm VALUES ('UBERON:0002107', '{"id": {"identifier": "UBERON:0002107"}}', 't');
"""


def test_old_pubmed_cache_gains_groundings(tmp_path: Path):
    path = tmp_path / "pubmed.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(BEFORE_GROUNDINGS)
    before = snapshot(path)
    liver = Grounding("liver", "anatomy", "UBERON:0002107", "liver", "nameres", STAMP)
    missed = Grounding("tumour margin", "anatomy", source="nameres")
    with PubMedCache(path) as cache:
        assert cache.get_groundings("anatomy", ["liver"]) == {}
        cache.put_groundings([liver, missed])
    with PubMedCache(path) as cache:
        got = cache.get_groundings("anatomy", ["liver", "tumour margin", "Liver", "kidney"])
        assert got.keys() == {"liver", "tumour margin"}  # names are looked up exactly as given
        assert got["liver"] == liver
        assert got["tumour margin"].curie is None and got["tumour margin"].created
        assert cache.get_abstracts(["1"])["1"].publication_types == ["Review"]
        # The same name grounded again replaces the old answer.
        cache.put_groundings([Grounding("tumour margin", "anatomy", "UBERON:0000000", "x")])
        assert cache.get_groundings("anatomy", ["tumour margin"])["tumour margin"].label == "x"
    after = snapshot(path)
    assert {t: after[t] for t in before} == before
