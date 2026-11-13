"""Scoring a run's claims, and the refinements they lead to, against a gold set, with the scores
checked by hand.

`scored`, the gold set's claims and run A's answers for it:

| PMID | gold | A's claims | A's checks |
| --- | --- | --- | --- |
| 1 | not treats | not treats; treats | found; the second fails a check |
| 2 | not treats | not treats | sentence not found, repaired |
| 3 | not treats | none | |
| 4 | not treats | not treats | sentence not found, not repaired |
| 5 | causes decreased activity | affects activity (an ancestor) | found |

PMIDs 1 to 4 are about imatinib and CML, 5 about imatinib and ABL1. As answered, A has 5 claims:
3 match exactly (1, 2, 4), 4 up to an ancestor (and 5), so precision and recall are 3/5 and 4/5.
As checked, 3 are left (1, 2, 5): 2 match exactly, 3 up to an ancestor; precision 2/3 and 1,
recall 2/5 and 3/5. Refined on the gold claims, "imatinib treats CML" is unsupported (every
claim denies it), and "imatinib causes decreased activity of ABL1" is supported by PMID 5. On
A's checked claims both are unsupported: the broader claim doesn't support the narrower edge.

`three_runs`, a gold set and three runs that differ from it in one way each:

| PMID | gold claims (all in human) | finds | misses | context |
| --- | --- | --- | --- | --- |
| 1 | treats | the same | the same | the same |
| 2 | treats, and also in clinical trials for | the same | none | the same |
| 3 | none | none | none | none |
| 4 (retracted) | treats | the same | the same | the same |
| 5 | causes decreased activity of ABL1 | the same | the same | the same, in the liver |

The gold edges: imatinib treats CML, imatinib in_clinical_trials_for CML, imatinib affects ABL1,
and imatinib causes decreased activity of ABL1. On the gold claims, each is supported: treats
by PMIDs 1 and 2 (4 is retracted), trials by 2, and both ABL1 edges by 5. With the species taken
out, "causes decreased activity in human" is the qualified ABL1 edge: a link to it from the
plain one, the gold side's only refinement. Run "finds" agrees on everything. Run "misses"
loses PMID 2: the trials edge is unsupported and the treats edge keeps PMID 1 alone, so 3 of the
5 supporting PMIDs, and 3 of 4 edges supported as on the gold claims (kappa 0: the gold side
has every edge supported). Run "context" proposes "causes decreased activity in the liver"
instead of the link: its proposals match none of the gold side's.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx
import pytest
from pubmed_evidence import (
    apply,
    config,
    evaluate,
    graphio,
    records,
    refine,
    repair,
    scope,
    sentences,
)
from pubmed_evidence.cache import (
    AbstractClaimsRow,
    CallRow,
    ClaimCheckRow,
    ClaimRepairRow,
    Grounding,
    LabelCache,
    PubMedCache,
    claim_id,
)
from pubmed_evidence.records import LabelRun
from test_apply import in_code
from test_bundle import (
    GENE_PAIR,
    HUMAN,
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
from test_ground import Fakes
from test_refine import CATEGORIES as REFINE_CATEGORIES
from test_refine import claim, expression, st
from test_refine import edge as refine_edge
from toy import ABL1, CML, IMATINIB, MDM2, NODES, TP53

from shared.schema import Edge, Statement

CATEGORIES = {n.id: list(n.category) for n in NODES}
MOUSE = "NCBITaxon:10090"
LIVER, KIDNEY = "UBERON:0002107", "UBERON:0002113"
ANATOMY, SPECIES = refine.ANATOMY, refine.SPECIES
GENE_DISEASE = graphio.pair_id(ABL1, CML)


def negative(n: int) -> dict:
    """A demonstrated absence: imatinib didn't treat CML in trial `n`."""
    return stored(
        TREATS,
        "negative",
        sentence=f"Imatinib did not improve survival in trial {n}.",
        finding="demonstrated_absence",
    )


INHIBITION = stored(INHIBITS, sentence="Imatinib inhibited ABL1 kinase activity.")
BROADER = stored(
    statement(IMATINIB, "biolink:affects", ABL1, object_aspect_qualifier="activity"),
    sentence="Imatinib inhibited ABL1 kinase activity.",
)
WRONG = stored(TREATS, sentence="Imatinib is a kinase inhibitor.")
TREATED = stored(TREATS, sentence="Imatinib treated CML.")
IN_TRIAL = stored(TREATS, sentence="In a phase 3 trial, imatinib treated CML.", also=(TRIALS,))
IN_LIVER = stored(INHIBITS, sentence="Imatinib inhibited ABL1 in the liver.", anatomy="liver")
TREATS_EDGE = edge(IMATINIB, "biolink:treats", CML)
TRIALS_EDGE = edge(IMATINIB, "biolink:in_clinical_trials_for", CML)
AFFECTS_EDGE = edge(IMATINIB, "biolink:affects", ABL1)
INHIBITS_EDGE = edge(
    IMATINIB,
    "biolink:affects",
    ABL1,
    qualified_predicate="biolink:causes",
    object_aspect_qualifier="activity",
    object_direction_qualifier="decreased",
)
# `three_runs`'s gold claims by PMID, and its runs: FINDS is the run label_run.json names.
GOLD_CLAIMS = {"1": [TREATED], "2": [IN_TRIAL], "3": [], "4": [TREATED], "5": [INHIBITION]}
FINDS, MISSES, CONTEXT = MODEL, "gpt-misses", "gpt-context"


def gold_abstract(pair: str, pmid: str, *claims: dict) -> dict:
    return {"pair_id": pair, "pmid": pmid, "gold_claims": list(claims)}


def gold_edge(e: Edge) -> dict:
    return {"edge": e.model_dump(mode="json", exclude_none=True)}


def eid(e: Edge) -> str:
    return graphio.edge_id(e.model_dump(mode="json", exclude_none=True))


def written(stored_statement: dict, **qualifiers: str) -> str:
    """A claim statement, with more qualifiers, as the report writes it."""
    s = Statement.model_validate(stored_statement)
    return sentences.sentence(s.model_copy(update={"qualifiers": s.qualifiers | qualifiers}), {})


def write_lines(path: Path, lines: list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines)
    path.write_text(text + "\n", encoding="utf-8")
    return path


def answer(
    w: World,
    pair: str,
    pmid: str,
    *claims: dict,
    design: str = "rct",
    invalid: tuple[int, ...] = (),
    not_found: tuple[int, ...] = (),
    model: str = MODEL,
    pv: str = PV,
    answered_by: str | None = None,
    created: str = "",
) -> None:
    """A label run's answer for an abstract, with code's checks of its claims: each passes and
    has its sentence found, but those at the positions given."""
    with LabelCache(w.cache_dir / config.LABEL_CACHE) as cache:
        row = AbstractClaimsRow(
            pair, pmid, pv, model, design, list(claims), created, answered_by or model
        )
        cache.put_abstract_claims([row])
        cache.put_claim_checks(
            ClaimCheckRow(pair, pmid, pv, model, claim_id(c), i not in invalid, i not in not_found)
            for i, c in enumerate(claims)
        )


def put_repair(w: World, pair: str, pmid: str, claim: dict) -> None:
    """The current repair pass found the claim's sentence."""
    with LabelCache(w.cache_dir / config.LABEL_CACHE) as cache:
        row = ClaimRepairRow(
            pair, pmid, PV, MODEL, claim_id(claim), *repair.current(), claim["evidence_sentence"]
        )
        row.sentence_valid = True
        cache.put_claim_repairs([row])


@pytest.fixture
def scored(tmp_path) -> tuple[World, Path]:
    """The gold set and run A of the module's docstring."""
    w = new_world(tmp_path)
    for pmid in "1234":
        w.abstract(TREATS_PAIR, pmid, "Imatinib in CML.", types=["Journal Article"])
    w.abstract(GENE_PAIR, "5", "Imatinib and ABL1.", types=["Journal Article"])
    gold = write_lines(
        w.work / evaluate.GOLD,
        [
            *(gold_abstract(TREATS_PAIR, p, negative(int(p))) for p in "1234"),
            gold_abstract(GENE_PAIR, "5", INHIBITION),
            gold_edge(TREATS_EDGE),
            gold_edge(INHIBITS_EDGE),
        ],
    )
    answer(w, TREATS_PAIR, "1", negative(1), WRONG, invalid=(1,))
    answer(w, TREATS_PAIR, "2", negative(2), not_found=(0,))
    put_repair(w, TREATS_PAIR, "2", negative(2))
    answer(w, TREATS_PAIR, "3")
    answer(w, TREATS_PAIR, "4", negative(4), not_found=(0,))
    answer(w, GENE_PAIR, "5", BROADER, design="in_vitro")
    return w, gold


def pair_of(pmid: str) -> str:
    return GENE_PAIR if pmid == "5" else TREATS_PAIR


def answer_all(w: World, model: str, changed: dict[str, list[dict]] | None = None) -> None:
    """A run's answers for `three_runs`'s abstracts: the gold claims, but those `changed`."""
    for pmid, claims in (GOLD_CLAIMS | (changed or {})).items():
        answer(w, pair_of(pmid), pmid, *claims, model=model)


@pytest.fixture
def three_runs(tmp_path) -> tuple[World, Path]:
    """The gold set and the three runs of the module's docstring."""
    w = new_world(tmp_path)
    for pmid in GOLD_CLAIMS:
        types = ["Journal Article", *(["Retracted Publication"] if pmid == "4" else [])]
        w.abstract(pair_of(pmid), pmid, "Imatinib.", types=types)
    with PubMedCache() as cache:
        cache.put_groundings([Grounding("liver", "anatomy", LIVER, "liver", "test")])
    gold = write_lines(
        w.work / evaluate.GOLD,
        [
            *(gold_abstract(pair_of(p), p, *c) for p, c in GOLD_CLAIMS.items()),
            *(gold_edge(e) for e in (TREATS_EDGE, TRIALS_EDGE, AFFECTS_EDGE, INHIBITS_EDGE)),
        ],
    )
    answer_all(w, FINDS)
    answer_all(w, MISSES, {"2": []})
    answer_all(w, CONTEXT, {"5": [IN_LIVER]})
    return w, gold


def cells(m: dict) -> dict[tuple[str, str], int]:
    """A support confusion matrix's nonzero cells, by (on the gold claims, on the run's)."""
    labels = evaluate.SUPPORT
    return {
        (labels[i], labels[j]): n
        for i, row in enumerate(m["confusion"])
        for j, n in enumerate(row)
        if n
    }


def test_claims_by_hand(scored):
    w, gold = scored
    result = evaluate.evaluate(gold, w.cache_dir, [(MODEL, PV)], categories=CATEGORIES)
    treats, inhibits = eid(TREATS_EDGE), eid(INHIBITS_EDGE)

    g = result["gold"]
    assert (g["edges"], g["scored"], g["without_abstracts"]) == (2, 2, 0)
    assert [(e["edge_id"], e["outcome"], e["pmids"]) for e in g["by_edge"]] == [
        (treats, "unsupported", []),
        (inhibits, "supported", ["PMID:5"]),
    ]
    # The claim in human is the edge itself once its species is out: no proposal.
    assert (g["proposed"], g["links"], g["species_folded"]) == (0, 0, 1)

    [r] = result["runs"]
    assert (r["covered"], r["abstracts"], r["fallback_served"]) == (5, 5, 0)
    assert r["claims"] == {
        "claims": 5,
        "failing_checks": 1,
        "sentences_not_found": 1,
        "sentences_repaired": 1,
    }
    answered, checked = r["as_answered"], r["as_checked"]
    assert (answered["exact"]["matched"], answered["exact"]["found"]) == (3, 5)
    assert answered["exact"]["precision"] == answered["exact"]["recall"] == pytest.approx(0.6)
    assert answered["ancestor"]["precision"] == answered["ancestor"]["recall"] == pytest.approx(0.8)
    assert checked["exact"]["precision"] == pytest.approx(2 / 3)
    assert checked["exact"]["recall"] == pytest.approx(0.4)
    assert checked["exact"]["f1"] == pytest.approx(0.5)  # 2 * 2 / (5 + 3)
    assert (checked["ancestor"]["precision"], checked["ancestor"]["recall"]) == (1.0, 0.6)

    assert (r["edges"], r["edges_covered"], r["edges_fallback_served"]) == (2, 2, 0)
    assert cells(r["support"]) == {
        ("unsupported", "unsupported"): 1,
        ("supported", "unsupported"): 1,
    }
    # Chance agreement (1 * 0 + 1 * 2) / 4 is the accuracy itself.
    assert (r["support"]["accuracy"], r["support"]["kappa"]) == (0.5, 0.0)
    assert r["pmids"] == {
        "gold": 1,
        "found": 0,
        "matched": 0,
        "precision": None,
        "recall": 0.0,
        "f1": 0.0,
    }
    assert r["proposals"]["gold"] == r["proposals"]["found"] == 0
    assert r["proposals"]["precision"] is r["proposals"]["recall"] is None
    assert r["disagreements"] == {
        "edges": [
            {
                "edge_id": inhibits,
                "on_gold": "supported",
                "on_run": "unsupported",
                "missing": ["PMID:5"],
                "extra": [],
            }
        ],
        "pairs": [],
    }
    assert (r["repairer"], r["repair_prompt_version"]) == repair.current()


def test_support_pmids_and_proposals_by_hand(three_runs):
    w, gold = three_runs
    result = evaluate.evaluate(gold, w.cache_dir, [FINDS, MISSES, CONTEXT], categories=CATEGORIES)
    treats, trials, affects, inhibits = map(
        eid, (TREATS_EDGE, TRIALS_EDGE, AFFECTS_EDGE, INHIBITS_EDGE)
    )

    g = result["gold"]
    assert result["species_in_proposals"] is False
    assert [(e["edge_id"], e["outcome"], e["pmids"], e["refined_by"]) for e in g["by_edge"]] == [
        (treats, "supported", ["PMID:1", "PMID:2"], []),  # not PMID 4: it is retracted
        (trials, "supported", ["PMID:2"], []),
        (affects, "refined", ["PMID:5"], [inhibits]),
        (inhibits, "supported", ["PMID:5"], []),
    ]
    assert g["outcomes"] == {"unsupported": 0, "supported": 3, "refined": 1}
    assert (g["proposed"], g["links"]) == (0, 1)
    assert g["proposals"] == [
        {
            "pair_id": GENE_PAIR,
            "statement": written(INHIBITS),
            "pmids": ["PMID:5"],
            "original_edge_ids": [affects],
            "linked_to": [inhibits],
        }
    ]

    finds, misses, context = result["runs"]
    for r in (finds, misses, context):
        assert (r["covered"], r["edges_covered"], r["pairs_covered"]) == (5, 4, 2)

    assert finds["support"]["accuracy"] == 1.0
    assert finds["support"]["kappa"] is None  # every edge supported on both sides
    everything = {"precision": 1.0, "recall": 1.0, "f1": 1.0}
    assert finds["pmids"] == {"gold": 5, "found": 5, "matched": 5} | everything
    assert finds["proposals"] == {"gold": 1, "found": 1, "matched": 1} | everything
    assert finds["disagreements"] == {"edges": [], "pairs": []}

    assert cells(misses["support"]) == {
        ("supported", "supported"): 3,
        ("supported", "unsupported"): 1,
    }
    # Chance agreement (4 * 3 + 0 * 1) / 16 = 0.75, the accuracy itself.
    assert (misses["support"]["accuracy"], misses["support"]["kappa"]) == (0.75, 0.0)
    supported = misses["support"]["per_label"]["supported"]
    assert (supported["precision"], supported["recall"]) == (1.0, 0.75)
    assert misses["pmids"]["gold"] == 5 and misses["pmids"]["found"] == 3
    assert (misses["pmids"]["precision"], misses["pmids"]["recall"]) == (1.0, pytest.approx(0.6))
    assert misses["pmids"]["f1"] == pytest.approx(0.75)  # 2 * 3 / (5 + 3)
    assert misses["proposals"]["f1"] == 1.0
    lost = {"on_gold": "supported", "missing": ["PMID:2"], "extra": []}
    assert misses["disagreements"] == {
        "edges": [
            {"edge_id": treats, "on_run": "supported"} | lost,
            {"edge_id": trials, "on_run": "unsupported"} | lost,
        ],
        "pairs": [],
    }

    # Context is no part of a claim's statement, so its claims are the curator's.
    assert context["as_checked"]["exact"]["f1"] == 1.0
    assert context["support"]["accuracy"] == 1.0 and context["pmids"] == finds["pmids"]
    assert context["proposals"] == {
        "gold": 1,
        "found": 1,
        "matched": 0,
        "precision": 0.0,
        "recall": 0.0,
        "f1": 0.0,
    }
    assert context["disagreements"] == {
        "edges": [],
        "pairs": [
            {
                "pair_id": GENE_PAIR,
                "missing": [written(INHIBITS)],
                "extra": [written(INHIBITS, **{ANATOMY: LIVER})],
            }
        ],
    }


def test_both_sides_take_the_species_out_of_proposals_unless_kept(three_runs):
    """Rule 7 on both sides. Run "mouse" makes the curator's claims, in mouse where the
    curator has human. With the species taken out, the default, its proposals are the
    curator's; with it kept, its ABL1 proposal is another statement. Support and PMIDs are the
    same either way."""
    w, gold = three_runs
    in_mouse = stored(INHIBITS, sentence="Imatinib inhibited ABL1 kinase activity.", species=MOUSE)
    answer_all(w, "gpt-mouse", {"5": [in_mouse]})
    runs = [(FINDS, PV), ("gpt-mouse", PV)]
    off = evaluate.evaluate(gold, w.cache_dir, runs, categories=CATEGORIES)
    on = evaluate.evaluate(
        gold, w.cache_dir, runs, categories=CATEGORIES, species_in_proposals=True
    )
    assert (off["species_in_proposals"], on["species_in_proposals"]) == (False, True)

    in_human = {SPECIES: HUMAN}
    assert [p["statement"] for p in off["gold"]["proposals"]] == [written(INHIBITS)]
    assert sorted(p["statement"] for p in on["gold"]["proposals"]) == sorted(
        [written(TREATS, **in_human), written(TRIALS, **in_human), written(INHIBITS, **in_human)]
    )
    assert (off["gold"]["species_folded"], on["gold"]["species_folded"]) == (3, 0)

    def support(result: dict) -> list[tuple[str, list[str]]]:
        return [(e["edge_id"], e["pmids"]) for e in result["gold"]["by_edge"]]

    assert support(off) == support(on)
    assert {e["outcome"] for e in on["gold"]["by_edge"]} == {"refined"}

    [finds_off, mouse_off], [finds_on, mouse_on] = off["runs"], on["runs"]
    assert finds_off["proposals"]["f1"] == finds_on["proposals"]["f1"] == 1.0
    assert mouse_off["proposals"]["f1"] == 1.0
    assert (mouse_on["proposals"]["matched"], mouse_on["proposals"]["gold"]) == (2, 3)
    assert mouse_on["disagreements"]["pairs"] == [
        {
            "pair_id": GENE_PAIR,
            "missing": [written(INHIBITS, **in_human)],
            "extra": [written(INHIBITS, **{SPECIES: MOUSE})],
        }
    ]
    for a, b in ((finds_off, finds_on), (mouse_off, mouse_on)):
        assert a["support"] == b["support"] and a["pmids"] == b["pmids"]
        assert a["pmids"]["f1"] == 1.0


def test_repairs_count_as_label_run_json_names_them(scored):
    w, gold = scored
    # label_run.json names the run, and repairs by another repairer: the one found here
    # doesn't count, so PMID 2's claim isn't checked either.
    run = LabelRun(model=MODEL, prompt_version=PV, repairer="r", repair_prompt_version="v")
    [r] = evaluate.evaluate(gold, w.cache_dir, [(MODEL, PV)], run, CATEGORIES)["runs"]
    assert (r["repairer"], r["repair_prompt_version"]) == ("r", "v")
    assert r["claims"]["sentences_repaired"] == 0
    assert r["claims"]["sentences_not_found"] == 2
    assert r["as_checked"]["exact"]["matched"] == 1


def test_matching_is_one_to_one_and_as_large_as_it_can_be():
    allowed = {(0, 0), (0, 1), (1, 0)}

    def same(g, f):
        return (g, f) in allowed

    # Taking found claim 0 for gold claim 0 would leave gold claim 1 without one.
    assert evaluate.matched([0, 1], [0, 1], same) == 2

    def equal(g, f):
        return g == f

    assert evaluate.matched([0, 0], [0], equal) == 1
    assert evaluate.matched([], [0], equal) == 0


def test_up_to_an_ancestor_takes_a_broader_statement_of_the_same_polarity():
    lattice = refine.Lattice()
    specific = ("positive", Statement.model_validate(INHIBITS))
    broader = ("positive", Statement.model_validate(BROADER["statement"]))
    same = evaluate.up_to_ancestor(lattice)
    assert same(specific, broader) and same(specific, specific)
    assert not same(broader, specific)  # more specific than the curator: over-claiming
    assert not same(specific, ("negative", broader[1]))
    assert not evaluate.exact(specific, broader)


@pytest.mark.parametrize(
    "gold, found",
    [
        ((TP53, "physically_interacts_with", MDM2), (MDM2, "physically_interacts_with", TP53)),
        ((ABL1, "associated_with", CML), (CML, "associated_with", ABL1)),
        ((ABL1, "correlated_with", CML), (CML, "correlated_with", ABL1)),
        ((IMATINIB, "directly_physically_interacts_with", ABL1), (ABL1, "interacts_with", None)),
        ((ABL1, "gene_associated_with_condition", CML), (CML, "associated_with", ABL1)),
    ],
)
def test_a_symmetric_statement_matches_either_way_round(gold, found):
    """'TP53 interacts with MDM2' and 'MDM2 interacts with TP53' are one claim, whichever node
    the curator and the model wrote first; a broader symmetric claim written from the other
    node matches up to an ancestor."""
    subject, predicate, object = found
    object = object or gold[0]

    def key(s: str, p: str, o: str) -> tuple[str, Statement]:
        read = refine.directed(Statement(subject=s, predicate=f"biolink:{p}", object=o), CATEGORIES)
        return "positive", read

    g, f = key(*gold), key(subject, predicate, object)
    same = evaluate.up_to_ancestor(refine.Lattice())
    assert same(g, f)
    assert evaluate.exact(g, f) == (predicate == gold[1])
    assert not same(g, ("negative", f[1]))


def test_claims_read_the_way_round_biolink_reads_them(tmp_path):
    """The curator wrote the claim from the disease, the run gave it from the gene with the
    disease's predicate: with the nodes' categories both read 'ABL1
    gene_associated_with_condition CML', so they match exactly."""
    w = new_world(tmp_path)
    w.abstract(GENE_DISEASE, "7", "ABL1 and CML.")
    from_disease = statement(CML, "biolink:condition_associated_with_gene", ABL1)
    from_gene = statement(ABL1, "biolink:condition_associated_with_gene", CML)
    gold = write_lines(
        w.work / evaluate.GOLD,
        [gold_abstract(GENE_DISEASE, "7", stored(from_disease, sentence="s"))],
    )
    answer(w, GENE_DISEASE, "7", stored(from_gene, sentence="s"), design="cohort")
    [r] = evaluate.evaluate(gold, w.cache_dir, [(MODEL, PV)], categories=CATEGORIES)["runs"]
    assert r["as_answered"]["exact"]["matched"] == r["as_checked"]["exact"]["matched"] == 1
    # Read as written, without the categories, they say different things.
    [r] = evaluate.evaluate(gold, w.cache_dir, [(MODEL, PV)])["runs"]
    assert r["as_answered"]["exact"]["matched"] == 0


EXPRESSION = statement(IMATINIB, "biolink:affects", ABL1, object_aspect_qualifier="expression")


def changed(direction: str) -> dict:
    """Imatinib causes a change of ABL1's expression."""
    return statement(
        IMATINIB,
        "biolink:affects",
        ABL1,
        "biolink:causes",
        object_aspect_qualifier="expression",
        object_direction_qualifier=direction,
    )


def expression_gold(w: World, liver: str, kidney: str) -> Path:
    """Imatinib affects ABL1 expression: up in the liver and down in the kidney in mouse."""
    up = stored(changed("increased"), sentence="up", species=MOUSE, anatomy=liver)
    down = stored(changed("decreased"), sentence="down", species=MOUSE, anatomy=kidney)
    lines = [
        gold_edge(edge(IMATINIB, "biolink:affects", ABL1, object_aspect_qualifier="expression")),
        gold_abstract(GENE_PAIR, "1", up),
        gold_abstract(GENE_PAIR, "2", down),
    ]
    return write_lines(w.work / evaluate.GOLD, lines)


def proposed(result: dict) -> set[str]:
    return {p["statement"] for p in result["gold"]["proposals"]}


def test_gold_anatomy_is_a_curie_or_a_name_grounded_as_the_runs_are(tmp_path):
    """With both contexts grounded, the curator's claims propose the edge up in the liver and
    down in the kidney; a name nothing grounds narrows nothing, and is counted."""
    w = new_world(tmp_path)
    fakes = Fakes()
    with PubMedCache() as cache:
        cache.put_groundings([Grounding("kidney", "anatomy", KIDNEY, "kidney", "test")])

    def evaluated(kidney: str) -> dict:
        gold = expression_gold(w, LIVER, kidney)
        return evaluate.evaluate(
            gold,
            w.cache_dir,
            [(MODEL, PV)],
            categories=CATEGORIES,
            lookup=fakes.lookup,
            nodenorm=fakes.nodenorm,
        )

    result = evaluated("Kidney ")  # the cache's name, as the ground step compares names
    assert proposed(result) == {
        written(changed("increased"), **{ANATOMY: LIVER}),
        written(changed("decreased"), **{ANATOMY: KIDNEY}),
    }
    assert result["gold"]["by_edge"][0]["pmids"] == ["PMID:1", "PMID:2"]
    assert fakes.looked_up == []  # LIVER is a CURIE, and the kidney's name is cached
    assert result["anatomy"] == {"names": 1, "grounded": 1, "ungrounded": 0, "failed": 0}
    result = evaluated("kidneys of the mice")
    assert proposed(result) == {
        written(changed("increased"), **{ANATOMY: LIVER}),
        written(changed("decreased")),
    }
    assert result["gold"]["by_edge"][0]["pmids"] == ["PMID:1", "PMID:2"]
    assert fakes.looked_up == [["kidneys of the mice"]]
    assert result["anatomy"] == {"names": 1, "grounded": 0, "ungrounded": 1, "failed": 0}


def test_names_no_ground_run_cached_are_grounded_here_and_counted(tmp_path, capsys):
    """The ground step grounds only the names of the run label_run.json names, so the curator's
    'liver' and 'kidneys' may never have been looked up: evaluate grounds them as it would, and
    the proposals name them. A failed lookup leaves a name ungrounded, counted and warned of."""
    w = new_world(tmp_path)
    gold = expression_gold(w, "liver", "kidneys")

    def broken(names):
        raise httpx.ConnectError("no route")

    result = evaluate.evaluate(
        gold, w.cache_dir, [(MODEL, PV)], categories=CATEGORIES, lookup=broken
    )
    assert proposed(result) == {written(changed("increased")), written(changed("decreased"))}
    assert result["anatomy"] == {"names": 2, "grounded": 0, "ungrounded": 0, "failed": 2}
    assert "lookup of 2 names failed" in capsys.readouterr().err
    assert "2 whose lookup failed" in evaluate.report(evaluate.load_gold(gold), result)

    fakes = Fakes()
    result = evaluate.evaluate(
        gold,
        w.cache_dir,
        [(MODEL, PV)],
        categories=CATEGORIES,
        lookup=fakes.lookup,
        nodenorm=fakes.nodenorm,
    )
    assert proposed(result) == {
        written(changed("increased"), **{ANATOMY: LIVER}),
        written(changed("decreased"), **{ANATOMY: KIDNEY}),
    }
    assert result["anatomy"] == {"names": 2, "grounded": 2, "ungrounded": 0, "failed": 0}


def test_load_gold(tmp_path, capsys):
    sample_line = {
        "pair_id": TREATS_PAIR,
        "pmid": "2",
        "title": "Title",
        "study_design": "rct",
        "claims": [{"claim_id": "x", "claim": negative(2), "valid": True}],
    }
    other = statement(IMATINIB, "biolink:treats", "MONDO:0005148")
    not_a_gene_disease_predicate = statement(ABL1, "biolink:treats", CML)
    no_role = {k: v for k, v in negative(9).items() if k != "claim_role"}
    no_level = {k: v for k, v in TREATS_EDGE.model_dump(mode="json").items() if k != "agent_type"}
    path = write_lines(
        tmp_path / "gold.jsonl",
        [
            # The pair reversed and the PMID as written by hand: read as the label step keys them.
            {**gold_abstract(f"{CML}|{IMATINIB}", "PMID:1", negative(1)), "note": "ignored"},
            # A sample line made a gold line; a study design, of any name, is ignored.
            sample_line | {"gold_study_design": "trial", "gold_claims": [negative(2)]},
            sample_line | {"pmid": "3", "gold_claims": None},  # not yet curated
            gold_abstract(TREATS_PAIR, "4"),  # an abstract that states nothing
            gold_abstract(TREATS_PAIR, "1", negative(1), negative(5)),  # a duplicate, kept
            # An edge line's other keys are ignored, an expected action among them.
            gold_edge(TREATS_EDGE) | {"gold_action": "NEGATE"},
            gold_edge(INHIBITS_EDGE) | {"gold_action": None},
            "not json",
            ["a list"],
            gold_abstract(TREATS_PAIR, None, negative(6)),
            gold_abstract(TREATS_PAIR, "6", stored(other, sentence="s")),
            gold_abstract(TREATS_PAIR, "6", no_role),
            gold_abstract(TREATS_PAIR, "6", "a claim"),
            gold_abstract(TREATS_PAIR, "6", {**negative(6), "polarity": "maybe"}),
            gold_abstract(GENE_DISEASE, "6", stored(not_a_gene_disease_predicate, sentence="s")),
            {"edge": no_level},
            {"edge": "an edge"},
        ],
    )
    gold = evaluate.load_gold(path, CATEGORIES)
    assert sorted(gold.abstracts) == [(TREATS_PAIR, "1"), (TREATS_PAIR, "2"), (TREATS_PAIR, "4")]
    assert gold.abstracts[(TREATS_PAIR, "1")].claims == (negative(1), negative(5))
    assert gold.abstracts[(TREATS_PAIR, "4")] == evaluate.GoldAbstract(TREATS_PAIR, "4", ())
    assert gold.claims == 3 and gold.by_pair() == {TREATS_PAIR: ["1", "2", "4"]}
    assert {e: g.id for e, g in gold.edges.items()} == {
        eid(TREATS_EDGE): eid(TREATS_EDGE),
        eid(INHIBITS_EDGE): eid(INHIBITS_EDGE),
    }
    assert gold.edges_by_pair() == {
        TREATS_PAIR: [gold.edges[eid(TREATS_EDGE)]],
        GENE_PAIR: [gold.edges[eid(INHIBITS_EDGE)]],
    }
    assert (gold.unlabeled, gold.invalid, gold.duplicates) == (1, 10, 1)
    err = capsys.readouterr().err
    assert f"{path}:8: not a JSON object; skipped" in err
    assert "isn't about the pair" in err and "no claim_role" in err
    assert "fits neither way" in err and "edge is not a JSON object" in err
    assert "edge is not a valid Edge: agent_type" in err
    # Without the categories nothing says which way round a predicate reads.
    assert evaluate.load_gold(path).invalid == 9


def test_answers_a_fallback_model_gave_are_left_out_with_their_pairs_edges(tmp_path):
    w = new_world(tmp_path)
    model, pv = "claude-opus-5-5", "label@f"
    gold = write_lines(
        w.work / evaluate.GOLD,
        [
            gold_abstract(TREATS_PAIR, "1", negative(1)),
            gold_abstract(TREATS_PAIR, "2", negative(2)),
            gold_abstract(GENE_PAIR, "5", INHIBITION),
            gold_edge(TREATS_EDGE),
            gold_edge(INHIBITS_EDGE),
        ],
    )
    answer(w, TREATS_PAIR, "1", negative(1), model=model, pv=pv)
    answer(w, TREATS_PAIR, "2", negative(2), model=model, pv=pv, answered_by="claude-opus-5")
    answer(w, GENE_PAIR, "5", INHIBITION, design="in_vitro", model=model, pv=pv)
    result = evaluate.evaluate(gold, w.cache_dir, [model], categories=CATEGORIES)
    [r] = result["runs"]
    assert r["prompt_version"] == pv
    assert (r["covered"], r["fallback_served"]) == (2, 1)
    assert r["as_checked"]["exact"]["gold"] == 2  # PMID 2's claim is left out
    assert (r["edges_covered"], r["edges_fallback_served"]) == (1, 1)
    assert (r["pairs_covered"], r["pairs"]) == (1, 2)
    assert cells(r["support"]) == {("supported", "supported"): 1}
    page = evaluate.report(evaluate.load_gold(gold, CATEGORIES), result)
    assert "1 more answered by a fallback model, left out of the scores, and so are the" in page


def test_a_model_alone_scores_each_prompt_version(scored):
    w, gold = scored
    # PV (scored's) answered every gold abstract first; v2 answers two later; v3 only another
    # pair, so it is skipped.
    answer(w, TREATS_PAIR, "1", negative(1), pv="label@v2", created="2099-01-01")
    answer(w, GENE_PAIR, "5", INHIBITION, design="in_vitro", pv="label@v2", created="2099-01-01")
    answer(w, GENE_DISEASE, "9", pv="label@v3", created="2099-01-02")
    results = evaluate.evaluate(gold, w.cache_dir, [MODEL], categories=CATEGORIES)["runs"]
    assert [r["prompt_version"] for r in results] == [PV, "label@v2"]
    v2 = results[1]
    assert (v2["covered"], v2["coverage"]) == (2, 0.4)
    assert v2["as_checked"]["exact"]["recall"] == 1.0  # of the two abstracts it answered
    assert (v2["edges_covered"], v2["support"]["n"]) == (1, 1)  # the treats pair is unfinished
    assert v2["support"]["accuracy"] == 1.0 and v2["pmids"]["f1"] == 1.0

    # Given with its version, a run is scored even with no answers.
    [v3] = evaluate.evaluate(gold, w.cache_dir, [f"{MODEL}@label@v3"])["runs"]
    assert v3["covered"] == 0 and v3["support"]["accuracy"] is None


def test_cost_from_logged_label_calls(scored):
    w, gold = scored
    with LabelCache(w.cache_dir / config.LABEL_CACHE) as cache:
        calls = [("p1", 0.01, None), ("p1", 0.02, None), ("p2", 0.03, None), ("p1", None, "500")]
        for pair, cost, error in calls:
            cache.log_call(CallRow("label", pair, MODEL, PV, 9000, 1000, cost=cost, error=error))
        cache.log_call(CallRow("review", "p1", MODEL, PV, 5000, 500, cost=1.0))  # another step
    [r] = evaluate.evaluate(gold, w.cache_dir, [(MODEL, PV)], categories=CATEGORIES)["runs"]
    assert r["usage"]["calls"] == 4 and r["usage"]["errors"] == 1 and r["usage"]["pairs"] == 2
    assert r["cost_per_call"] == pytest.approx(0.02)  # $0.06 over the 3 calls that worked
    assert r["cost_per_pair"] == pytest.approx(0.03)
    assert r["cost_per_10k_pairs"] == pytest.approx(300.0)
    assert r["input_tokens_per_pair"] == 18000


def test_metrics_by_hand():
    """Seven (expected, given) pairs, six agreeing. Expected totals (a, b, c, d) 2, 3, 1, 1;
    given 3, 2, 1, 1; chance agreement (6 + 6 + 1 + 1) / 49 = 2/7, so kappa (6/7 - 2/7) / (5/7)
    = 0.8."""
    labels = ("a", "b", "c", "d", "e")
    pairs = [
        ("a", "a"),
        ("a", "a"),
        ("c", "c"),
        ("b", "b"),
        ("b", "a"),
        ("d", "d"),
        ("b", "b"),
    ]
    m = evaluate.metrics(pairs, labels)
    assert (m["n"], m["correct"]) == (7, 6) and m["accuracy"] == pytest.approx(6 / 7)
    assert m["kappa"] == pytest.approx(0.8)
    a, b = m["per_label"]["a"], m["per_label"]["b"]
    assert (a["precision"], a["recall"], a["f1"]) == (pytest.approx(2 / 3), 1.0, 0.8)
    assert (b["precision"], b["recall"]) == (1.0, pytest.approx(2 / 3))
    assert m["per_label"]["e"]["f1"] is None  # in neither, so not in the macro F1
    assert m["macro_f1"] == pytest.approx(0.9)
    # One more a given as b: 5 of 7, chance (4 + 9 + 1 + 1) / 49, kappa 10/17.
    m = evaluate.metrics([("a", "b"), *pairs[1:]], labels)
    assert m["accuracy"] == pytest.approx(5 / 7) and m["kappa"] == pytest.approx(10 / 17)
    # By default the labels are an edge's support.
    assert evaluate.metrics([("supported", "unsupported")])["confusion"] == [[0, 1], [0, 0]]


def _args(work: Path, cache_dir: Path, *argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    evaluate.add_arguments(parser)
    args = parser.parse_args(list(argv))
    args.work, args.cache_dir = work, cache_dir
    return args


def test_run_writes_the_report(three_runs, capsys):
    w, _ = three_runs
    assert evaluate.run(_args(w.work, w.cache_dir)) == 0  # the run label_run.json names
    table = (w.work / evaluate.REPORT).read_text().split("## Comparison")[1].split("##")[0]
    assert table.count("| `") == 1 and f"| `{FINDS}@{PV}` |" in table
    assert "on the gold claims 4 of 4 edges are supported" in capsys.readouterr().out

    # Several runs compare in one table.
    argv = ("--run", FINDS, "--run", MISSES, "--run", CONTEXT, "--run", "nobody")
    assert evaluate.run(_args(w.work, w.cache_dir, *argv)) == 0
    page = (w.work / evaluate.REPORT).read_text()
    assert page.startswith("# Gold-set evaluation")
    assert "5 abstracts in 2 pairs with 4 claims (1 abstracts with none), and 4 edges." in page
    assert "Species in proposals: taken out, as step 6 does by default" in page
    assert (
        page.index("## The gold side")
        < page.index("## Comparison")
        < page.index(f"## `{FINDS}@{PV}`")
    )
    assert "4 of 4 edges refined on their pair's gold claims. 4 supported, 1 of them" in page
    affects, inhibits = eid(AFFECTS_EDGE), eid(INHIBITS_EDGE)
    assert f"| `{affects}` | refined | PMID:5 | `{inhibits}` |" in page
    cell = GENE_PAIR.replace("|", "\\|")  # escaped in a table
    assert f"| `{cell}` | {written(INHIBITS)} | PMID:5 | `{affects}` | `{inhibits}` |" in page
    rows = [
        f"| `{FINDS}@{PV}` | 5/5 | 1.000 / 1.000 | 1.000 / 1.000 | 4/4 | 1.000 | – "
        "| 1.000 / 1.000 | 1.000 / 1.000 | – |",
        f"| `{MISSES}@{PV}` | 5/5 | 1.000 / 0.750 | 1.000 / 0.750 | 4/4 | 0.750 | 0.000 "
        "| 1.000 / 0.600 | 1.000 / 1.000 | – |",
        f"| `{CONTEXT}@{PV}` | 5/5 | 1.000 / 1.000 | 1.000 / 1.000 | 4/4 | 1.000 | – "
        "| 1.000 / 1.000 | 0.000 / 0.000 | – |",
    ]
    for row in rows:
        assert row in page
    assert "`nobody" not in page  # nobody has no answers
    assert "### Support" in page and "### PMIDs and proposals" in page
    assert "| supported | 3 | 1 |" in page  # misses' confusion matrix
    assert "| Supporting PMIDs, by edge | 1.000 | 0.600 | 0.750 | 3 | 5 | 3 |" in page
    assert f"| `{eid(TRIALS_EDGE)}` | supported | unsupported | PMID:2 | – |" in page
    liver = written(INHIBITS, **{ANATOMY: LIVER})
    assert f"| `{cell}` | {written(INHIBITS)} | {liver} |" in page
    out = capsys.readouterr().out
    assert "3 runs; on the gold claims 4 of 4 edges are supported" in out
    assert "with 0 statements proposed and 1 linked" in out and "evaluate: wrote" in out

    # Kept, the species makes three proposals, as `apply --species-in-proposals` writes them.
    assert evaluate.run(_args(w.work, w.cache_dir, "--species-in-proposals")) == 0
    page = (w.work / evaluate.REPORT).read_text()
    assert "Species in proposals: kept." in page
    assert "3 statements proposed and 0 linked to an edge of their pair." in page


def test_run_fails_without_gold_or_answers(scored, tmp_path):
    w, gold = scored
    work = tmp_path / "elsewhere"
    records.write_label_run(work, LabelRun(model=MODEL, prompt_version=PV))
    assert evaluate.run(_args(work, w.cache_dir)) == 1  # no gold file
    empty = write_lines(tmp_path / "empty.jsonl", [gold_edge(TREATS_EDGE)])
    assert evaluate.run(_args(work, w.cache_dir, "--gold", str(empty))) == 1  # no claims
    g = str(gold)
    assert evaluate.run(_args(work, w.cache_dir, "--gold", g, "--run", "nobody")) == 1
    assert evaluate.run(_args(work, w.cache_dir, "--gold", g, "--run", f"{MODEL}@label@x")) == 1
    assert evaluate.run(_args(work, tmp_path / "nocache", "--gold", g)) == 1
    assert evaluate.run(_args(tmp_path / "nowhere", w.cache_dir, "--gold", g)) == 1  # no run
    assert not (work / evaluate.REPORT).exists()


# --- Drawing the pairs to curate -------------------------------------------------------------


def refinement(pair: str, *edges: tuple[str, str, str, str, str], abstracts: int = 1) -> dict:
    """A line of refinements.jsonl, with only what a draw reads: the number of abstracts read,
    and each edge given as (id, outcome, subject, predicate, object)."""
    lines = []
    for edge_id, outcome, subject, predicate, object in edges:
        statement = {"subject": subject, "predicate": predicate, "object": object, "negated": False}
        lines.append(
            {
                "edge_id": edge_id,
                "statement": statement,
                "supported": outcome != "unsupported",
                "refined_by": ["kgbcia-augmentation:p"] if outcome == "refined" else [],
            }
        )
    return {
        "pair_id": pair,
        "abstracts_read": abstracts,
        "edges": lines,
        "proposed_edges": [],
        "links": [],
    }


def drawn_world(tmp_path: Path) -> Path:
    """Twelve pairs of imatinib and a gene whose edge is supported (chemical-affects-gene), and
    one whose edge is unsupported; imatinib treats CML, refined; ABL1 and CML, in a pair with a
    refined edge and an unsupported one (gene-disease); and one more pair of imatinib and a gene
    whose edge is unsupported, with no abstracts read."""
    w = new_world(tmp_path)
    genes = [f"NCBIGene:{100 + i}" for i in range(14)]
    entities = [*(records.Entity(id=g, category=["biolink:Gene"]) for g in genes)]
    nodes = [records.Entity(id=n.id, category=list(n.category)) for n in NODES]
    graphio.write_jsonl(w.work / records.ENTITIES, [*nodes, *entities])
    affects = "biolink:affects"
    lines = [
        refinement(graphio.pair_id(IMATINIB, g), (f"e:{g}", "supported", IMATINIB, affects, g))
        for g in genes[:12]
    ]
    unsupported = ("e:none", "unsupported", IMATINIB, affects, genes[12])
    lines.append(refinement(graphio.pair_id(IMATINIB, genes[12]), unsupported))
    lines.append(refinement(TREATS_PAIR, ("e:treats", "refined", IMATINIB, "biolink:treats", CML)))
    associated = ("e:assoc", "refined", ABL1, "biolink:gene_associated_with_condition", CML)
    contributes = ("e:contrib", "unsupported", ABL1, "biolink:contributes_to", CML)
    lines.append(refinement(GENE_DISEASE, associated, contributes))
    unread = ("e:unread", "unsupported", IMATINIB, affects, genes[13])
    lines.append(refinement(graphio.pair_id(IMATINIB, genes[13]), unread, abstracts=0))
    graphio.write_jsonl(w.work / records.REFINEMENTS, lines)
    return w.work


def test_draw_takes_every_stratum_in_turn_smallest_first(tmp_path):
    """Five strata: supported chemical-affects-gene (12 pairs), and refined
    chemical-treats-disease, refined gene-disease, unsupported chemical-affects-gene and
    unsupported gene-disease (one each; the gene-disease pair is in both of its strata). Drawing
    four takes one from each stratum, the small ones first; unsupported gene-disease's only pair
    is drawn already, so its turn passes. The rest come from the large stratum, in an order the
    seed shuffles. The pair with no abstracts is never drawn, and is counted."""
    work = drawn_world(tmp_path)
    path, lines, without = evaluate.draw_pairs(work, 4, seed=1)
    assert path == work / evaluate.GOLD_PAIRS
    assert without == 1
    assert [(line["outcome"], line["family"]) for line in lines] == [
        ("refined", "chemical-treats-disease"),
        ("refined", "gene-disease"),
        ("unsupported", "chemical-affects-gene"),
        ("supported", "chemical-affects-gene"),
    ]
    assert lines[1]["pair_id"] == GENE_DISEASE
    assert lines[1]["edges"] == {"e:assoc": "refined", "e:contrib": "unsupported"}
    assert list(graphio.read_jsonl(path)) == lines
    six = evaluate.draw_pairs(work, 6, seed=1)[1]
    assert six[:4] == lines and len({line["pair_id"] for line in six}) == 6
    assert evaluate.draw_pairs(work, 6, seed=1)[1] == six
    others = {
        tuple(x["pair_id"] for x in evaluate.draw_pairs(work, 6, seed=s)[1]) for s in range(5)
    }
    assert len(others) > 1
    # More than there are: every pair with abstracts once.
    every = evaluate.draw_pairs(work, 100)[1]
    assert len(every) == 15 and "e:unread" not in {e for x in every for e in x["edges"]}


def test_draw_reads_the_lines_apply_writes():
    """A pair step 6 refined, as `apply.pair_record` writes it: X affects Y, which a claim of
    increased expression refines, and X affects Y's expression, which nothing supports."""
    edges = (refine_edge(st(), "e:plain"), refine_edge(expression(), "e:expression"))
    [pair] = apply.refine_pairs(in_code([claim(expression("increased"))], edges)).pairs
    line = json.loads(json.dumps(apply.pair_record(pair)))
    assert line["abstracts_read"] == 1
    [drawn] = evaluate.draw([line], scope.DEFAULT_RULES, REFINE_CATEGORIES, 1)
    assert drawn["edges"] == {"e:plain": "refined", "e:expression": "refined"}
    assert (drawn["outcome"], drawn["family"]) == ("refined", "chemical-affects-gene")


def test_draw_from_the_command_line(tmp_path, capsys):
    work = drawn_world(tmp_path)
    cache_dir = tmp_path / "cache"
    assert evaluate.run(_args(work, cache_dir, "--draw", "3", "--seed", "2")) == 0
    out = capsys.readouterr().out
    assert "1 refined pairs with no abstracts left out" in out
    assert "1 pairs drawn for refined chemical-treats-disease" in out
    assert f"wrote 3 pairs to {work / evaluate.GOLD_PAIRS}" in out
    assert not (work / evaluate.REPORT).exists()
    assert evaluate.run(_args(work, cache_dir, "--draw", "40")) == 0
    assert "only 15 refined pairs have abstracts, fewer than 40" in capsys.readouterr().err
    assert evaluate.run(_args(work, cache_dir, "--draw", "0")) == 1
    (work / records.REFINEMENTS).unlink()
    assert evaluate.run(_args(work, cache_dir, "--draw", "3")) == 1
    err = capsys.readouterr().err
    assert f"no {work / records.REFINEMENTS}; run `kgb-augment apply` first" in err


def test_parse_run():
    assert evaluate.parse_run("gpt-5.6-luna") == ("gpt-5.6-luna", None)
    assert evaluate.parse_run("gpt-5.6-luna@label@1a2b3c4d") == ("gpt-5.6-luna", "label@1a2b3c4d")
