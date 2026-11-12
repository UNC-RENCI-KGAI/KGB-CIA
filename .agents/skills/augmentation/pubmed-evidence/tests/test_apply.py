"""Step 6 (apply.py): refining the checked edges from their pairs' claims, and writing the graph,
the pair refinements and the report. Work files, caches and the input graph are built in code
(test_bundle's `World`), or a bundle is (`in_code`, from test_refine's helpers); refine.py and
the Biolink lookups are real, and no model is called.

The scene: imatinib `treats` CML, which two human RCTs support, one of them cited by the edge;
a third trial found no benefit and a fourth only shows imatinib was tested in clinical trials
for CML, so neither supports it. Without its species, "imatinib treats CML in human" is the
treats edge itself, so by default it is no proposal. And imatinib `affects` ABL1, plain and
qualified: in vitro claims say imatinib causes decreased activity of ABL1, which the qualified
edge already says (a link from the plain edge), and one says so in the liver (a proposed edge,
refining both).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
from pubmed_evidence import apply, bundle, config, graphio, records, repair, scope
from pubmed_evidence.cache import (
    ClaimRepairRow,
    ClaimReviewRow,
    Grounding,
    LabelCache,
    PubMedCache,
    claim_id,
)
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
from test_refine import ACTIVITY, ANATOMY, CATEGORIES, LATTICE, LIVER, X, Y, claim, expression, st
from test_refine import edge as refine_edge
from toy import ABL1, CML, IMATINIB, MDM2, TP53, UBERON_VERSION

from shared.pubmed import default_cache_path
from shared.schema import AugmentedEdge, Edge, PublicationEvidence, Statement

REVIEWER, REVIEW_PV = "review-model", "review@00000000"
WORKS = "Imatinib induced durable responses in chronic myeloid leukemia"
NULL = "Imatinib was no better than placebo in chronic myeloid leukemia"
TESTED = "Imatinib was tested in a phase 2 trial in chronic myeloid leukemia"
INHIBITED = "Imatinib inhibited ABL1 kinase activity"
GENE_PMIDS = ("201", "202", "203", "204", "205", "206")

TREATS_EDGE = edge(IMATINIB, "biolink:treats", CML, id="treats:1", publications=["PMID:101"])
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
IN_LIVER = Statement(
    subject=IMATINIB,
    predicate="biolink:affects",
    object=ABL1,
    qualified_predicate="biolink:causes",
    qualifiers={
        "anatomical_context_qualifier": LIVER,
        "object_aspect_qualifier": "activity",
        "object_direction_qualifier": "decreased",
    },
)
IN_LIVER_ID = graphio.proposed_edge_id(IN_LIVER.id)
IN_HUMAN = Statement(
    subject=IMATINIB,
    predicate="biolink:treats",
    object=CML,
    qualifiers={"species_context_qualifier": HUMAN},
)
GENE_SUPPORT = [f"PMID:{p}" for p in (*GENE_PMIDS, "208")]


@pytest.fixture
def world(tmp_path: Path) -> World:
    return new_world(tmp_path)


def scene(w: World, *extra: Edge) -> None:
    """The scene's edges (and `extra` ones), abstracts and claims. PMID 206's sentence was
    repaired; 207's wasn't found, so it counts for nothing."""
    w.edges(TREATS_EDGE, PLAIN, QUALIFIED, *extra)
    for pmid in ("101", "102"):
        w.abstract(TREATS_PAIR, pmid, f"{WORKS}.", types=["Randomized Controlled Trial"])
        w.answer(TREATS_PAIR, pmid, stored(TREATS, sentence=WORKS))
    w.abstract(TREATS_PAIR, "103", f"{NULL}.", types=["Randomized Controlled Trial"])
    negative = stored(
        TREATS, "negative", sentence=NULL, also=(TRIALS,), finding="demonstrated_absence"
    )
    w.answer(TREATS_PAIR, "103", negative)
    w.abstract(TREATS_PAIR, "104", f"{TESTED}.", types=["Clinical Trial, Phase II"])
    w.answer(TREATS_PAIR, "104", stored(TRIALS, sentence=TESTED))
    for pmid in (*GENE_PMIDS, "207", "208"):
        w.abstract(GENE_PAIR, pmid, f"{INHIBITED} in vitro.")
    in_vitro = stored(INHIBITS, sentence=INHIBITED, species=None)
    for pmid in GENE_PMIDS[:5]:
        w.answer(GENE_PAIR, pmid, in_vitro, design="in_vitro")
    misquoted = stored(INHIBITS, sentence="imatinib blocked ABL1", species=None)
    [fixed] = w.answer(GENE_PAIR, "206", misquoted, design="in_vitro", sentence_valid=False)
    w.answer(GENE_PAIR, "207", misquoted, design="in_vitro", sentence_valid=False)
    liver = stored(INHIBITS, sentence=INHIBITED, species=None, anatomy="liver")
    w.answer(GENE_PAIR, "208", liver, design="in_vitro")
    with PubMedCache() as cache:
        cache.put_groundings([Grounding("liver", "anatomy", LIVER, "liver", "test")])
    repairer, version = repair.current()
    with LabelCache(w.cache_dir / config.LABEL_CACHE) as cache:
        cache.put_claim_repairs(
            [ClaimRepairRow(GENE_PAIR, "206", PV, MODEL, fixed, repairer, version, INHIBITED, True)]
        )
    run = records.read_label_run(w.work)
    update = {"repairer": repairer, "repair_prompt_version": version}
    records.write_label_run(w.work, run.model_copy(update=update))


def review(w: World, verdicts: dict[str, str | None] | None = None) -> None:
    """A review of the run's claims: each upheld, unless `verdicts` gives its PMID another
    verdict, or None to leave it unreviewed; and label_run.json names the review."""
    verdicts = verdicts or {}
    with LabelCache(w.cache_dir / config.LABEL_CACHE) as cache:
        rows = [
            ClaimReviewRow(r.pair_id, r.pmid, PV, MODEL, claim_id(c), REVIEWER, REVIEW_PV, v, v)
            for r in cache.abstract_claims(PV, MODEL)
            for c in r.claims
            if (v := verdicts.get(r.pmid, "upheld")) is not None
        ]
        cache.put_claim_reviews(rows)
    run = records.read_label_run(w.work)
    update = {"reviewer": REVIEWER, "review_prompt_version": REVIEW_PV}
    records.write_label_run(w.work, run.model_copy(update=update))


def run_apply(
    w: World, out: str = "out", graph: Path | None = None, **kw
) -> tuple[dict, list[dict]]:
    graph = graph or w.graph
    report = apply.apply_claims(graph, w.work.parent / out, w.cache_dir, w.work, **kw)
    return report, list(graphio.read_jsonl(w.work.parent / out / graphio.EDGES))


def refinements(w: World) -> dict[str, dict]:
    return {d["pair_id"]: d for d in graphio.read_jsonl(w.work / apply.REFINEMENTS)}


def evidence_of(edge: dict) -> dict[str, dict]:
    return {e["publication"]: e for e in edge["lit_evidence"]}


def supports(edge: dict) -> dict[str, list[bool]]:
    """PMID -> whether each of its claims supports the edge."""
    return {e["publication"]: [c["supports"] for c in e["claims"]] for e in edge["lit_evidence"]}


def dump(e: Edge) -> dict:
    return json.loads(e.model_dump_json(exclude_none=True))


def args_for(w: World, *argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    apply.add_arguments(parser)
    args = parser.parse_args(list(argv))
    args.input, args.output = w.graph, w.work.parent / "out"
    args.work, args.cache_dir = w.work, w.cache_dir
    return args


def in_code(claims, edges, others=()) -> bundle.Bundles:
    """test_refine's pair of X and Y as a bundle built in code: one abstract per claim, a
    Retracted Publication where the claim is retracted, with test_refine's categories and
    UBERON closure."""
    evidence = tuple(
        PublicationEvidence(
            publication=c.pmid,
            publication_types=["Retracted Publication"] if c.retracted else [],
            supporting_document_type="research",
            retracted=c.retracted,
            found_by=["search"],
            study_design="in_vitro",
            study_design_source="extractor",
            claims=[c.record],
        )
        for c in claims
    )
    pair = graphio.pair_id(X, Y)
    keys = tuple((pair, c.pmid, f"c{i}") for i, c in enumerate(claims))
    pairs = [bundle.Bundle(pair, tuple(edges), evidence, keys, tuple(others))]
    entities = {n: records.Entity(id=n, category=cats) for n, cats in CATEGORIES.items()}
    return bundle.Bundles(pairs, entities, LATTICE, None)


# --- Checked edges --------------------------------------------------------------------------


def test_a_supported_edge_keeps_its_record_and_gains_its_pmids(world, capsys):
    cited = edge(
        IMATINIB,
        "biolink:treats",
        CML,
        publications=["PMID:1", "doi:10.1/x"],
        affinity=8.1,
        kgbcia_source_records=["tcrd.x#1"],
    )
    unscoped = edge(IMATINIB, "biolink:contraindicated_in", CML, id="out:1")
    world.edges(cited, scope_out=(unscoped,))
    works = stored(TREATS, sentence=WORKS, species=None)
    for pmid in ("1", "2", "3"):
        world.abstract(TREATS_PAIR, pmid, f"{WORKS}.", types=["Randomized Controlled Trial"])
        world.answer(TREATS_PAIR, pmid, works)
    world.abstract(TREATS_PAIR, "4", "No benefit was seen.")
    world.answer(TREATS_PAIR, "4", stored(TREATS, "negative", sentence="No benefit was seen"))
    world.abstract(TREATS_PAIR, "5", f"{WORKS}.", types=["Retracted Publication"])
    world.answer(TREATS_PAIR, "5", works)  # rule 9: a retracted paper's claim doesn't count
    world.abstract(TREATS_PAIR, "6", "Imatinib was given.")
    world.answer(TREATS_PAIR, "6")  # no claim
    world.abstract(TREATS_PAIR, "7", f"We hypothesize that {WORKS}.")
    world.answer(TREATS_PAIR, "7", stored(TREATS, sentence=WORKS, species=None, role="hypothesis"))
    review(world)
    run = records.read_label_run(world.work)
    records.write_label_run(world.work, run.model_copy(update={"uberon_version": "v"}))
    report, edges = run_apply(world)

    checked, plain = edges
    # The record as read, slot for slot, with its id; its new PMIDs appended to publications.
    original = dump(cited)
    assert {k: checked[k] for k in original if k != "publications"} == {
        k: v for k, v in original.items() if k != "publications"
    }
    assert checked["publications"] == ["PMID:1", "doi:10.1/x", "PMID:2", "PMID:3"]
    assert checked["id"] == graphio.edge_id(original)
    assert checked["augmentation"] == "checked" and checked["original_edge_ids"] == [checked["id"]]
    # Its own citation counts in its score, and isn't added again.
    assert checked["lit_support_score"] == 3
    assert checked["lit_supporting_publications"] == ["PMID:1", "PMID:2", "PMID:3"]
    assert checked["lit_added_publications"] == ["PMID:2", "PMID:3"]
    assert checked["lit_refined_by"] == [] and checked["lit_context_notes"] == []
    # Negative, retracted and hypothesis claims support nothing.
    assert supports(checked) == {
        "PMID:1": [True],
        "PMID:2": [True],
        "PMID:3": [True],
        "PMID:4": [False],
        "PMID:5": [False],
        "PMID:6": [],
        "PMID:7": [False],
    }
    one, five = evidence_of(checked)["PMID:1"], evidence_of(checked)["PMID:5"]
    assert one["found_by"] == ["source", "search"]
    assert evidence_of(checked)["PMID:2"]["found_by"] == ["search"]
    assert (one["study_design"], one["study_design_source"]) == ("rct", "pubmed")
    assert one["publication_types"] == ["Randomized Controlled Trial"]
    assert one["claims"][0]["review"] == "upheld"
    assert five["retracted"] and five["publication_types"] == ["Retracted Publication"]
    AugmentedEdge.model_validate(checked)
    # Out of scope: as read, with its id.
    assert plain == dump(unscoped)

    [line] = refinements(world).values()
    assert line == {
        "pair_id": TREATS_PAIR,
        "abstracts_read": 7,
        "edges": [
            {
                "edge_id": checked["id"],
                "statement": {
                    "subject": IMATINIB,
                    "predicate": "biolink:treats",
                    "object": CML,
                    "negated": False,
                },
                "supported": True,
                "support_score": 3,
                "supporting_publications": ["PMID:1", "PMID:2", "PMID:3"],
                "added_publications": ["PMID:2", "PMID:3"],
                "refined_by": [],
            }
        ],
        "proposed_edges": [],
        "links": [],
    }

    assert report["edges_checked"] == report["edges_supported"] == 1
    assert report["pairs_refined"] == 1 and report["publications_added"] == 2
    assert report["claims"] == {
        "used": 6,
        "counting": 3,
        "upheld": 6,
        "rejected": 0,
        "unreviewed": 0,
        "sentences_repaired": 0,
    }
    assert report["uberon_version"] == UBERON_VERSION
    assert report["run"]["uberon_version"] == "v"
    # PMIDs 4, 6 and 7 were cached without publication types.
    assert report["skipped"] == {"abstracts without publication types": 3}
    assert "3 abstracts have no publication types" in capsys.readouterr().err
    nodes = (world.work.parent / "out" / graphio.NODES).read_text().splitlines()
    assert nodes == (world.graph / graphio.NODES).read_text().splitlines()


def test_records_are_written_back_as_read(world):
    """Slots step 6 doesn't write keep their exact values: no defaults added, no nulls dropped,
    key order kept."""
    world.edges(edge(IMATINIB, "biolink:treats", CML))  # the scope; the graph is below
    nodes = [
        '{"id":"CHEBI:45783","category":["biolink:SmallMolecule"],"name":"imatinib"}',
        '{"id":"MONDO:0011996","category":["biolink:Disease"],"extra":null}',
    ]
    edges = [
        '{"subject":"CHEBI:45783","predicate":"biolink:treats","object":"MONDO:0011996",'
        '"primary_knowledge_source":"infores:drugcentral","knowledge_level":"knowledge_assertion",'
        '"agent_type":"manual_agent","p_value":null,"original_subject":null,"score":NaN,'
        '"publications":["PMID:1"],"supporting_data_source":["infores:dailymed"],"z_last":true}',
        '{"subject":"MONDO:0011996","predicate":"biolink:has_phenotype","object":"CHEBI:45783",'
        '"primary_knowledge_source":"infores:x","knowledge_level":"not_provided",'
        '"agent_type":"not_provided","p_value":null,"id":"x:1"}',
    ]
    (world.graph / graphio.NODES).write_text("\n".join(nodes) + "\n")
    (world.graph / graphio.EDGES).write_text("\n".join(edges) + "\n")
    run_apply(world)
    out = world.work.parent / "out"
    assert (out / graphio.NODES).read_text().splitlines() == nodes
    lines = (out / graphio.EDGES).read_text().splitlines()
    assert lines[0].startswith(edges[0][:-1] + ',"id":"kgbcia-edge:')
    assert '"augmentation":"checked","original_edge_ids":' in lines[0]
    assert '"lit_support_score":0,' in lines[0]
    assert lines[1] == edges[1]


def test_unsupported_and_unchecked_edges(world, capsys):
    """An unsupported edge is checked, with score 0 and its publications as they were: here a
    pair searched without abstracts. Unchecked edges pass through as read, without an earlier
    run's fields: the treats pair has an abstract unanswered (a failed call), and the affects
    pair wasn't searched."""
    gene_disease = edge(
        ABL1, "biolink:gene_associated_with_condition", CML, publications=["PMID:9"]
    )
    unsearched = edge(IMATINIB, "biolink:affects", ABL1)
    stale = {
        **dump(TREATS_EDGE),
        "lit_action": "KEEP",
        "lit_superseded_by": [],
        "evidence_score": 1.0,
        "lit_support_score": 1,
    }
    world.edges(TREATS_EDGE, gene_disease, unsearched)
    graphio.write_jsonl(
        world.graph / graphio.EDGES, [stale, dump(gene_disease), dump(unsearched)], compact=True
    )
    world.abstract(TREATS_PAIR, "1", "Imatinib treats CML.")
    world.abstract(TREATS_PAIR, "2", "Imatinib was given.")
    world.answer(TREATS_PAIR, "1", stored(TREATS, sentence="Imatinib treats CML."))
    world.search(graphio.pair_id(ABL1, CML))  # searched, nothing found
    report, edges = run_apply(world)

    assert edges[0] == dump(TREATS_EDGE)
    assert edges[2] == {**dump(unsearched), "id": graphio.edge_id(dump(unsearched))}
    unsupported = edges[1]
    assert unsupported["publications"] == ["PMID:9"]
    assert unsupported["augmentation"] == "checked"
    assert (unsupported["lit_support_score"], unsupported["lit_evidence"]) == (0, [])
    for name in ("supporting", "added"):
        assert unsupported[f"lit_{name}_publications"] == []
    assert report["unchecked"] == {
        "edges_not_fully_labeled": 1,
        "edges_not_searched": 1,
        "pairs_not_fully_labeled": 1,
    }
    assert report["edges_checked"] == report["edges_unsupported"] == 1
    assert report["edges_supported"] == 0 and report["ids_assigned"] == 2
    [line] = refinements(world).values()
    assert line["abstracts_read"] == 0
    assert line["edges"][0]["supported"] is False and line["edges"][0]["support_score"] == 0
    assert "1 pairs aren't fully labeled" in capsys.readouterr().err


def test_a_pair_with_claims_never_checked_is_left_unchecked(world, capsys):
    """Two claims of the treats pair were never checked (a label run stopped before checking
    them): its edge is written as read and counted, not refined on the other two."""
    world.edges(TREATS_EDGE)
    for pmid in ("101", "102", "103", "104"):
        world.abstract(TREATS_PAIR, pmid, f"{WORKS}.", types=["Randomized Controlled Trial"])
        claim_ = stored(TREATS, sentence=WORKS)
        world.answer(TREATS_PAIR, pmid, claim_, checked=pmid in ("101", "102"))
    review(world)
    report, edges = run_apply(world)
    assert edges == [dump(TREATS_EDGE)]
    assert report["edges_checked"] == 0 and refinements(world) == {}
    assert report["unchecked"]["pairs_not_fully_labeled"] == 1
    assert report["unchecked"]["edges_not_fully_labeled"] == 1
    assert report["skipped"]["pairs with claims never checked"] == 1
    assert "1 pairs aren't fully labeled" in capsys.readouterr().err


# --- Holds ----------------------------------------------------------------------------------


def test_a_pair_with_a_name_never_looked_up_is_held(world, capsys):
    """A claim names the bone marrow, which the ground step never looked up (its lookup failed):
    it would count in every context, so the pair is held, its edge written as read and counted,
    until a ground run looks the name up."""
    world.edges(TREATS_EDGE)
    for pmid in ("101", "102"):
        world.abstract(TREATS_PAIR, pmid, f"{WORKS}.", types=["Randomized Controlled Trial"])
        anatomy = "bone marrow" if pmid == "102" else None
        world.answer(TREATS_PAIR, pmid, stored(TREATS, sentence=WORKS, anatomy=anatomy))
    review(world)
    for kw in ({}, {"require_review": False}):
        report, edges = run_apply(world, **kw)
        assert edges == [dump(TREATS_EDGE)] and refinements(world) == {}
        assert report["held"] == {
            "pairs_unreviewed": 0,
            "pairs_anatomy_not_looked_up": 1,
            "edges": 1,
        }
        assert report["unchecked"]["pairs_not_fully_labeled"] == 0
        assert report["skipped"]["pairs with anatomy names never grounded"] == 1
        err = capsys.readouterr().err
        assert "1 pairs have a claim naming anatomy the ground step never looked up" in err
        assert "kgb-augment ground" in err and "aren't fully labeled" not in err

    with PubMedCache() as cache:
        cache.put_groundings([Grounding("bone marrow", "anatomy", None, None, "test")])
    report, edges = run_apply(world)
    assert edges[0]["lit_supporting_publications"] == ["PMID:101", "PMID:102"]
    assert edges[0]["lit_context_notes"] == ["bone marrow"]
    assert report["held"]["edges"] == 0
    assert "pairs with anatomy names never grounded" not in report["skipped"]


def test_an_unreviewed_claim_holds_its_pair_only_when_it_would_count(world, capsys):
    """203 would count toward the affects edges and has no review: their pair is held, and its
    edges are written as read. 103's in_clinical_trials_for supports no edge in scope, so its
    missing review holds nothing."""
    scene(world)
    review(world, {"203": None, "103": None})
    report, edges = run_apply(world)
    treats, plain, qualified = edges
    assert (plain, qualified) == (dump(PLAIN), dump(QUALIFIED))
    assert treats["augmentation"] == "checked" and treats["lit_support_score"] == 2
    assert list(refinements(world)) == [TREATS_PAIR]
    assert report["held"] == {"pairs_unreviewed": 1, "pairs_anatomy_not_looked_up": 0, "edges": 2}
    assert report["edges_checked"] == 1 and report["pairs_refined"] == 1
    assert report["claims"]["unreviewed"] == 2
    err = capsys.readouterr().err
    assert f"1 pairs have claims that would count but no review ({REVIEWER} hasn't" in err


def test_a_support_only_claim_counts_unreviewed_and_proposes_nothing(world):
    """Rule 11: an unreviewed "imatinib ameliorates CML" adds its PMID to the treats edge and
    proposes nothing; with `review_support_only`, its missing review holds the pair."""
    scene(world)
    relieved = "Imatinib relieved the symptoms of chronic myeloid leukemia"
    world.abstract(TREATS_PAIR, "105", f"{relieved}.")
    ameliorates = statement(IMATINIB, "biolink:ameliorates_condition", CML)
    world.answer(TREATS_PAIR, "105", stored(ameliorates, sentence=relieved, species=None))
    review(world, {"105": None})
    report, edges = run_apply(world)
    treats = edges[0]
    assert treats["lit_supporting_publications"] == ["PMID:101", "PMID:102", "PMID:105"]
    assert treats["lit_refined_by"] == [] and report["proposed"]["edges"] == 1  # in the liver
    assert report["held"]["pairs_unreviewed"] == 0
    assert report["run"]["review_support_only"] is False

    report, edges = run_apply(world, review_support_only=True)
    assert edges[0] == dump(TREATS_EDGE)
    assert report["held"]["pairs_unreviewed"] == 1 and report["run"]["review_support_only"]


def test_without_a_review_pairs_are_held_and_no_review_writes_them(world, capsys):
    scene(world)
    report, edges = run_apply(world)
    assert edges == [dump(TREATS_EDGE), dump(PLAIN), dump(QUALIFIED)]
    assert report["held"] == {"pairs_unreviewed": 2, "pairs_anatomy_not_looked_up": 0, "edges": 3}
    assert report["proposed"] == {"edges": 0, "links": 0, "species_folded": 0}
    assert report["claims"]["unreviewed"] == 12 and report["refinements"] == 0
    assert "(the run has no review)" in capsys.readouterr().err

    # --no-review counts them, for testing only.
    report, edges = run_apply(world, "out2", require_review=False)
    assert [e["id"] for e in edges[3:]] == [IN_LIVER_ID]
    assert edges[0]["lit_supporting_publications"] == ["PMID:101", "PMID:102"]
    assert report["held"]["pairs_unreviewed"] == 0 and report["run"]["require_review"] is False


def test_a_rejected_claim_does_not_count(world):
    scene(world)
    review(world, {"201": "reject", "208": "reject"})
    _, edges = run_apply(world)
    treats, plain, qualified = edges  # the proposal in the liver rests on 208 alone
    assert qualified["lit_supporting_publications"] == GENE_SUPPORT[1:-1]
    assert plain["lit_refined_by"] == ["gene:qualified"] and qualified["lit_refined_by"] == []
    [claim_] = evidence_of(qualified)["PMID:201"]["claims"]
    assert (claim_["review"], claim_["supports"]) == ("reject", False)


def test_a_retracted_papers_claims_do_not_count():
    """Rule 9: apply carries each abstract's retraction to its claims."""
    claims = [claim(st()), claim(expression("increased"), pmid="PMID:2", retracted=True)]
    loaded = in_code(claims, [refine_edge(st())])
    assert [e.retracted for e in loaded.pairs[0].evidence] == [False, True]
    refined = apply.refine_pairs(loaded)
    [(_, outcome)] = refined.outcomes.values()
    assert outcome.pmids == ("PMID:1",) and refined.pairs[0].refinement.proposed == ()


# --- Proposed edges and links ---------------------------------------------------------------


def test_proposed_edges_and_links(world):
    scene(world)
    review(world)
    report, edges = run_apply(world)
    treats, plain, qualified, proposed = edges

    # The treats edge: two RCTs, one it cites; "in human" without its species is the edge.
    assert treats["publications"] == ["PMID:101", "PMID:102"]
    assert treats["lit_support_score"] == 2 and treats["lit_added_publications"] == ["PMID:102"]
    assert treats["lit_refined_by"] == []
    assert supports(treats) == {
        "PMID:101": [True],
        "PMID:102": [True],
        "PMID:103": [False],
        "PMID:104": [False],
    }
    # Both affects edges are supported by every in vitro claim with its quote found (206's is
    # the repair's; 207's wasn't found). The plain edge's proposal in the liver is a new edge;
    # its proposal of what the qualified edge says is a link to it.
    for e in (plain, qualified):
        assert e["lit_supporting_publications"] == e["lit_added_publications"] == GENE_SUPPORT
        assert e["publications"] == GENE_SUPPORT and e["lit_support_score"] == 7
        assert supports(e)["PMID:207"] == [False]
    assert plain["lit_refined_by"] == ["gene:qualified", IN_LIVER_ID]
    assert qualified["lit_refined_by"] == [IN_LIVER_ID]
    claim_ = evidence_of(qualified)["PMID:206"]["claims"][0]
    assert claim_["evidence_sentence_repaired"] and claim_["evidence_sentence"] == INHIBITED

    # The proposed edge, with this stage's provenance, naming both originals it refines.
    assert {k: proposed[k] for k in list(proposed)[:15]} == {
        "id": IN_LIVER_ID,
        "subject": IMATINIB,
        "predicate": "biolink:affects",
        "object": ABL1,
        "qualified_predicate": "biolink:causes",
        "anatomical_context_qualifier": LIVER,
        "object_aspect_qualifier": "activity",
        "object_direction_qualifier": "decreased",
        "primary_knowledge_source": config.INFORES,
        "aggregator_knowledge_source": [],
        "supporting_data_source": ["infores:pubmed"],
        "knowledge_level": "knowledge_assertion",
        "agent_type": "text_mining_agent",
        "publications": ["PMID:208"],
        "original_edge_ids": ["gene:plain", "gene:qualified"],
    }
    assert proposed["augmentation"] == "proposed" and proposed["lit_support_score"] == 1
    assert proposed["lit_supporting_publications"] == ["PMID:208"]
    assert "lit_refined_by" not in proposed and "kgbcia_source_records" not in proposed
    # Its evidence marks the claims that make its statement.
    assert supports(proposed)["PMID:208"] == [True]
    assert supports(proposed)["PMID:201"] == [False]
    AugmentedEdge.model_validate(proposed)

    lines = refinements(world)
    assert list(lines) == sorted([TREATS_PAIR, GENE_PAIR])
    assert lines[TREATS_PAIR]["proposed_edges"] == lines[TREATS_PAIR]["links"] == []
    gene = lines[GENE_PAIR]
    assert [e["refined_by"] for e in gene["edges"]] == [
        ["gene:qualified", IN_LIVER_ID],
        [IN_LIVER_ID],
    ]
    [new] = gene["proposed_edges"]
    assert new == {
        "id": IN_LIVER_ID,
        **{k: proposed[k] for k in list(proposed)[1:8]},
        "negated": False,
        "publications": ["PMID:208"],
        "support_score": 1,
        "original_edge_ids": ["gene:plain", "gene:qualified"],
    }
    [link] = gene["links"]
    assert link == {
        "linked_to": ["gene:qualified"],
        **{k: qualified[k] for k in ("subject", "predicate", "object", "qualified_predicate")},
        "object_aspect_qualifier": "activity",
        "object_direction_qualifier": "decreased",
        "negated": False,
        "publications": GENE_SUPPORT[:-1],
        "support_score": 6,
        "original_edge_ids": ["gene:plain"],
    }

    assert {k: report[k] for k in list(report)[5:15]} == {
        "edges_read": 3,
        "ids_assigned": 0,
        "earlier_edges_dropped": 0,
        "pairs_refined": 2,
        "edges_checked": 3,
        "edges_supported": 3,
        "edges_unsupported": 0,
        "edges_refined": 2,
        "publications_added": 15,
        "proposed": {"edges": 1, "links": 1, "species_folded": 1},
    }
    assert report["held"] == {"pairs_unreviewed": 0, "pairs_anatomy_not_looked_up": 0, "edges": 0}
    assert report["claims"] == {
        "used": 12,
        "counting": 9,
        "upheld": 12,
        "rejected": 0,
        "unreviewed": 0,
        "sentences_repaired": 1,
    }
    assert report["refinements"] == 2 and report["edges_written"] == 4
    assert report["run"]["species_in_proposals"] is False
    lines = apply.summary(report)
    assert "apply: checked 3 edges: 3 supported, 0 unsupported, 2 refined" in lines[2]
    assert lines[3] == (
        "apply: proposed 1 new edges and 1 links to edges that already say it; species in "
        "proposals: taken out, 1 species-qualified proposals folded (--species-in-proposals "
        "keeps them)"
    )
    assert lines[-1].endswith("and 2 pair refinements")


def test_species_in_proposals_writes_e_in_human(world, capsys):
    """Rule 7: with the flag on, "imatinib treats CML in human" is written, and the treats edge
    names it; its support and PMIDs are the same either way."""
    scene(world)
    review(world)
    _, off = run_apply(world)
    report, on = run_apply(world, "out2", species_in_proposals=True)
    human_id = graphio.proposed_edge_id(IN_HUMAN.id)
    assert [e["id"] for e in on[3:]] == sorted([IN_LIVER_ID, human_id])
    human = next(e for e in on if e["id"] == human_id)
    assert human["species_context_qualifier"] == HUMAN
    assert human["original_edge_ids"] == ["treats:1"]
    assert human["publications"] == ["PMID:101", "PMID:102"]
    assert on[0]["lit_refined_by"] == [human_id] and off[0]["lit_refined_by"] == []
    for before, after in zip(off[:3], on[:3], strict=True):
        assert {k: v for k, v in before.items() if k != "lit_refined_by"} == {
            k: v for k, v in after.items() if k != "lit_refined_by"
        }
    assert report["proposed"] == {"edges": 2, "links": 1, "species_folded": 0}
    assert report["run"]["species_in_proposals"] is True
    assert [p["id"] for p in refinements(world)[TREATS_PAIR]["proposed_edges"]] == [human_id]

    # The same from the command line.
    assert apply.run(args_for(world, "--species-in-proposals")) == 0
    assert "species in proposals: kept" in capsys.readouterr().out
    written = list(graphio.read_jsonl(world.work.parent / "out" / graphio.EDGES))
    assert written == on


def test_two_originals_refined_by_one_statement_make_one_edge(world):
    """The plain edge and one that names activity both refine to the qualified statement in the
    liver: one proposed edge, naming both; its evidence is found against both originals'
    publications."""
    activity = edge(
        IMATINIB,
        "biolink:affects",
        ABL1,
        id="gene:activity",
        primary_knowledge_source="infores:other",
        publications=["PMID:201"],
        object_aspect_qualifier="activity",
    )
    scene(world, activity)
    review(world)
    _, edges = run_apply(world)
    [proposed] = [e for e in edges if e["id"] == IN_LIVER_ID]
    assert proposed["original_edge_ids"] == ["gene:plain", "gene:qualified", "gene:activity"]
    assert edges[3]["lit_refined_by"] == ["gene:qualified", IN_LIVER_ID]
    assert edges[3]["publications"] == GENE_SUPPORT
    assert edges[3]["lit_added_publications"] == GENE_SUPPORT[1:]
    assert evidence_of(proposed)["PMID:201"]["found_by"] == ["source", "search"]
    assert evidence_of(edges[1])["PMID:201"]["found_by"] == ["search"]
    assert evidence_of(edges[3])["PMID:201"]["found_by"] == ["source", "search"]


def test_a_proposed_edge_finds_its_sources_only_in_its_originals(world):
    """`source` in a proposed edge's `found_by` means its originals cite the PMID (the schema's
    `found_by`). An edge of the pair it doesn't refine, about ABL1's expression, cites PMID 201:
    201 is `source` on that edge, but not on the proposed edge."""
    by_expression = edge(
        IMATINIB,
        "biolink:affects",
        ABL1,
        id="gene:expression",
        publications=["PMID:201"],
        object_aspect_qualifier="expression",
    )
    scene(world, by_expression)
    review(world)
    _, edges = run_apply(world)
    [proposed] = [e for e in edges if e["id"] == IN_LIVER_ID]
    assert proposed["original_edge_ids"] == ["gene:plain", "gene:qualified"]
    assert evidence_of(proposed)["PMID:201"]["found_by"] == ["search"]
    [cited] = [e for e in edges if e["id"] == "gene:expression"]
    assert evidence_of(cited)["PMID:201"]["found_by"] == ["source", "search"]


def test_a_proposal_an_edge_select_left_out_makes_links_to_it(world):
    """Rule 5: the qualified edge is out of scope (as a capped statement below --min-score
    would be), so it isn't checked; the plain edge's proposal still links to it rather than
    adding a duplicate."""
    scene(world)
    world.edges(TREATS_EDGE, PLAIN, scope_out=(QUALIFIED,))
    review(world)
    report, edges = run_apply(world)
    treats, plain, qualified, proposed = edges
    assert plain["lit_refined_by"] == ["gene:qualified", IN_LIVER_ID]
    assert qualified == dump(QUALIFIED)  # written as read: not checked
    assert proposed["original_edge_ids"] == ["gene:plain"]
    [link] = refinements(world)[GENE_PAIR]["links"]
    assert link["linked_to"] == ["gene:qualified"]
    assert [e["edge_id"] for e in refinements(world)[GENE_PAIR]["edges"]] == ["gene:plain"]
    assert report["proposed"]["edges"] == report["proposed"]["links"] == 1
    assert report["edges_checked"] == 2


def test_a_symmetric_proposal_links_to_an_original_written_the_other_way_round(world):
    """'TP53 interacts with MDM2' refines to 'TP53 physically interacts with MDM2', which the
    graph already has as 'MDM2 physically interacts with TP53': a link, not a new edge, and the
    claims support both."""
    pair = graphio.pair_id(TP53, MDM2)
    broad = edge(TP53, "biolink:interacts_with", MDM2, id="iw")
    held = edge(MDM2, "biolink:physically_interacts_with", TP53, id="piw")
    world.edges(broad, held)
    bound = "TP53 bound MDM2 directly"
    for pmid in ("1", "2"):
        world.abstract(pair, pmid, f"{bound}.")
        binds = statement(TP53, "biolink:physically_interacts_with", MDM2)
        world.answer(pair, pmid, stored(binds, sentence=bound, species=None), design="in_vitro")
    review(world)
    report, edges = run_apply(world)
    assert len(edges) == 2 and report["proposed"]["links"] == 1
    assert [e["lit_supporting_publications"] for e in edges] == [["PMID:1", "PMID:2"]] * 2
    assert edges[0]["lit_refined_by"] == ["piw"] and edges[1]["lit_refined_by"] == []
    [link] = refinements(world)[pair]["links"]
    assert link["linked_to"] == ["piw"] and link["original_edge_ids"] == ["iw"]


def test_a_pair_record_lists_an_edge_listed_twice_once():
    claims = [
        claim(expression("increased"), pmid="PMID:2"),
        claim(expression("increased"), pmid="PMID:3", anatomy=LIVER),
        claim(st(), pmid="PMID:4", species=HUMAN),
    ]
    edges = [refine_edge(st(), "e:1"), refine_edge(st(), "e:1")]
    others = [refine_edge(expression("increased"), "o:1")]
    refined = apply.refine_pairs(in_code(claims, edges, others))
    [pair] = refined.pairs
    assert list(refined.outcomes) == ["e:1"] and refined.folded == 1
    [p] = pair.refinement.proposed
    assert p.statement == expression("increased", **{ANATOMY: LIVER})
    record = apply.pair_record(pair)
    assert [e["edge_id"] for e in record["edges"]] == ["e:1"]
    assert record["edges"][0]["refined_by"] == sorted([p.id, "o:1"])
    assert record["edges"][0]["supporting_publications"] == ["PMID:2", "PMID:3", "PMID:4"]
    assert [x["id"] for x in record["proposed_edges"]] == [p.id]
    assert [x["linked_to"] for x in record["links"]] == [["o:1"]]
    # With species in proposals, "X affects Y in human" is a third.
    kept = apply.refine_pairs(in_code(claims, edges), species_in_proposals=True)
    assert len(kept.pairs[0].refinement.proposed) == 3 and kept.folded == 0


def test_proposals_that_become_one_without_species_are_one_edge():
    """Rule 7, the flag off by default: "X increases Y's activity" and the same in human are one
    proposed edge, with both PMIDs, its evidence marking both claims. The edge they refine has
    the same support and PMIDs with the flag on, which writes two."""
    claims = [claim(ACTIVITY, pmid="PMID:1"), claim(ACTIVITY, pmid="PMID:2", species=HUMAN)]
    loaded = in_code(claims, [refine_edge(st())])
    off = apply.refine_pairs(loaded)
    [pair] = off.pairs
    [p] = pair.refinement.proposed
    written = apply.proposed_edge(pair, p)
    assert "species_context_qualifier" not in written and off.folded == 1
    assert written["publications"] == written["lit_supporting_publications"] == ["PMID:1", "PMID:2"]
    assert written["lit_support_score"] == 2 and written["original_edge_ids"] == ["e:1"]
    assert supports(written) == {"PMID:1": [True], "PMID:2": [True]}
    assert apply.pair_record(pair)["edges"][0]["refined_by"] == [p.id]
    on = apply.refine_pairs(loaded, species_in_proposals=True)
    assert len(on.pairs[0].refinement.proposed) == 2 and on.folded == 0
    [(_, a)], [(_, b)] = off.outcomes.values(), on.outcomes.values()
    assert (a.claims, a.pmids, a.added) == (b.claims, b.pmids, b.added)


# --- Reruns ---------------------------------------------------------------------------------


def test_rerunning_on_its_own_output_writes_the_same_graph(world):
    scene(world)
    review(world)
    run_apply(world, "clean")
    # The same graph, with an earlier format's evidence on one edge and an earlier run's
    # proposed edge this run doesn't make.
    earlier = {**dump(TREATS_EDGE), "id": graphio.ID_PREFIX + "0123456789abcdef"}
    old = {**dump(QUALIFIED), "evidence": [], "evidence_score": 0.0, "negates_original": False}
    old |= {"same_statement_edge_ids": [], "lit_action": "KEEP", "lit_rule_trace": ["0"]}
    rows = [dump(TREATS_EDGE), dump(PLAIN), old, earlier]
    graphio.write_jsonl(world.graph / graphio.EDGES, rows, compact=True)
    report, _ = run_apply(world)
    assert report["earlier_edges_dropped"] == 1
    out, clean = world.work.parent / "out", world.work.parent / "clean"
    for name in (graphio.NODES, graphio.EDGES):
        assert (out / name).read_bytes() == (clean / name).read_bytes()

    # Select keeps the same scope on the output: the proposed edge is skipped.
    def kept(graph: Path, work: str) -> set[str]:
        scope.select(graph, world.work.parent / work)
        path = world.work.parent / work / records.STATEMENTS
        return {s.statement_id for s in records.read_records(path, records.SelectedStatement)}

    assert kept(out, "select-out") == kept(world.graph, "select-in")

    # The output as input: its appended PMIDs are taken back out before the edges are refined,
    # so the same graph again, its proposed edge made anew.
    again, _ = run_apply(world, "out2", graph=out)
    assert again["earlier_edges_dropped"] == 1 and again["ids_assigned"] == 0
    assert again["publications_added"] == report["publications_added"] == 15
    for name in (graphio.NODES, graphio.EDGES):
        assert (world.work.parent / "out2" / name).read_bytes() == (out / name).read_bytes()


# --- Inputs and failures --------------------------------------------------------------------


def test_cli_flags_override_label_run(world, capsys):
    scene(world)
    review(world)
    run = records.read_label_run(world.work)
    records.write_label_run(world.work, run.model_copy(update={"model": "other"}))
    assert apply.run(args_for(world)) == 1
    assert "no claims for other" in capsys.readouterr().err
    assert apply.run(args_for(world, "--model", MODEL)) == 0
    out = capsys.readouterr().out
    assert f"run model={MODEL}" in out and "proposed 1 new edges and 1 links" in out
    assert apply.run(args_for(world, "--model", MODEL, "--no-review")) == 0
    assert "--no-review: unreviewed claims counted" in capsys.readouterr().out


def test_failed_run_leaves_no_output(world, monkeypatch):
    scene(world)
    review(world)
    run_apply(world, "first")
    before = (world.work / apply.REFINEMENTS).read_bytes()

    def broken(*args, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(apply, "proposed_edge", broken)
    with pytest.raises(RuntimeError):
        run_apply(world)
    assert list((world.work.parent / "out").iterdir()) == []
    assert (world.work / apply.REFINEMENTS).read_bytes() == before
    assert not list(world.work.glob(".*.tmp"))


def test_missing_label_run_fails(world, capsys):
    (world.work / records.LABEL_RUN).unlink()
    world.edges(TREATS_EDGE)
    assert apply.run(args_for(world)) == 1
    assert "no label run" in capsys.readouterr().err
    assert not (world.work.parent / "out").exists()


def test_missing_caches_and_work_files_fail(world, capsys):
    scene(world)
    # Each is checked before the ones above it in this list.
    for path, why in (
        (world.cache_dir / config.UBERON_FILE, "run `kgb-augment ground` first"),
        (world.work / records.PUBMED, "run `kgb-augment pubmed` first"),
        (world.work / records.STATEMENTS, "run `kgb-augment select` first"),
        (default_cache_path(), "no pubmed cache"),
        (world.cache_dir / config.LABEL_CACHE, "no label cache"),
    ):
        path.unlink()
        assert apply.run(args_for(world)) == 1
        assert why in capsys.readouterr().err


def test_output_dir_must_not_be_input_dir(world, capsys):
    scene(world)
    args = args_for(world)
    args.output = world.graph / "." / ".." / world.graph.name
    before = (world.graph / graphio.EDGES).read_bytes()
    assert apply.run(args) == 1
    assert "is the input dir" in capsys.readouterr().err
    assert (world.graph / graphio.EDGES).read_bytes() == before


def test_two_different_edges_with_one_id_fail_loudly(world, capsys):
    """Two different edges sharing an id would be written with one's outcome: apply refuses the
    graph instead."""
    scene(
        world, edge(IMATINIB, "biolink:directly_physically_interacts_with", ABL1, id="gene:plain")
    )
    review(world)
    assert apply.run(args_for(world)) == 1
    err = capsys.readouterr().err
    assert "share the id 'gene:plain'" in err
    assert not (world.work.parent / "out").exists()


def test_an_invalid_edge_fails_loudly(world, capsys):
    scene(world)
    with (world.graph / graphio.EDGES).open("a") as f:
        f.write('{"subject":"A:1","predicate":"treats","object":"B:2"}\n')
    assert apply.run(args_for(world)) == 1
    err = capsys.readouterr().err
    assert "edge 4 of" in err and "not a valid Edge" in err
    assert list((world.work.parent / "out").iterdir()) == []
