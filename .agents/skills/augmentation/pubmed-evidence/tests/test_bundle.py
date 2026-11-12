"""A pair's bundle (bundle.py): which pairs, edges, abstracts and claims refinement gets, built
from work files and caches written in code. `World` and the claim helpers are shared with
test_review.py."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pubmed_evidence import bundle, config, graphio, records, refine, repair, scope
from pubmed_evidence.cache import (
    AbstractClaimsRow,
    ClaimCheckRow,
    ClaimRepairRow,
    ClaimReviewRow,
    Grounding,
    LabelCache,
    PubMedCache,
    claim_id,
)
from pubmed_evidence.records import LabelRun
from toy import ABL1, CML, IMATINIB, LIVER, NODES, UBERON_OBO

from shared.pubmed import Abstract, FetchedPmid, PubmedRecord
from shared.schema import Edge

MODEL = "gpt-6-luna"
PV = "label@00000000"
HUMAN = "NCBITaxon:9606"
TREATS_PAIR = graphio.pair_id(IMATINIB, CML)
GENE_PAIR = graphio.pair_id(IMATINIB, ABL1)


def statement(subject: str, predicate: str, object: str, qualified=None, **qualifiers) -> dict:
    """A claim statement as the label step stores it."""
    return {
        "subject": subject,
        "predicate": predicate,
        "object": object,
        "qualified_predicate": qualified,
        "qualifiers": dict(sorted(qualifiers.items())),
    }


TREATS = statement(IMATINIB, "biolink:treats", CML)
TRIALS = statement(IMATINIB, "biolink:in_clinical_trials_for", CML)
INHIBITS = statement(
    IMATINIB,
    "biolink:affects",
    ABL1,
    "biolink:causes",
    object_aspect_qualifier="activity",
    object_direction_qualifier="decreased",
)


def stored(
    statement: dict,
    polarity: str = "positive",
    *,
    sentence: str,
    also: tuple[dict, ...] = (),
    role: str = "primary_finding",
    hedged: bool = False,
    finding: str | None = None,
    species: str | None = HUMAN,
    anatomy: str | None = None,
    notes: str | None = None,
) -> dict:
    """A claim as `abstract_claims` stores it."""
    return {
        "polarity": polarity,
        "statement": statement,
        "also_supports": list(also),
        "claim_role": role,
        "hedged": hedged,
        "negative_finding_type": finding,
        "context": {"species": species, "anatomy": anatomy, "notes": notes},
        "evidence_sentence": sentence,
    }


def edge(subject: str, predicate: str, object: str, **kw) -> Edge:
    """A text-mined edge."""
    fields = dict(
        subject=subject,
        predicate=predicate,
        object=object,
        primary_knowledge_source="infores:textminingkp",
        knowledge_level="knowledge_assertion",
        agent_type="text_mining_agent",
    )
    return Edge(**{**fields, **kw})


@dataclass
class World:
    """A work directory, its caches and an input graph, written as a test adds to them. The
    PubMed cache is the test's own (conftest), not one in `cache_dir`."""

    work: Path
    cache_dir: Path
    graph: Path
    searched: dict[str, PubmedRecord] = field(default_factory=dict)

    def edges(self, *edges: Edge, scope_out: tuple[Edge, ...] = ()) -> None:
        """The input graph: `edges` in scope, as select would keep them, and `scope_out` not."""
        graphio.write_jsonl(self.graph / graphio.NODES, NODES)
        graphio.write_jsonl(self.graph / graphio.EDGES, [*edges, *scope_out])
        kept = {}
        for e in edges:
            sid = graphio.edge_statement_id(e)
            kept[sid] = records.Statement(
                statement_id=sid,
                pair_id=graphio.pair_id(e.subject, e.object),
                subject=e.subject,
                predicate=e.predicate,
                object=e.object,
                qualifiers=graphio.qualifiers_of(e),
                rule="r",
                edge_count=1,
            )
        graphio.write_jsonl(self.work / records.STATEMENTS, kept.values())

    def search(self, pair_id: str) -> None:
        """A pair step 2 searched and found nothing for."""
        self.searched.setdefault(
            pair_id, PubmedRecord(pair_id=pair_id, query="q", retrieved="2026-11-05", total=0)
        )
        graphio.write_jsonl(self.work / records.PUBMED, self.searched.values())

    def abstract(
        self,
        pair_id: str,
        pmid: str,
        text: str,
        *,
        types: list[str] | None = None,
        origin: str = "search",
        year: int | None = 2020,
    ) -> None:
        """An abstract step 2 fetched for the pair, in the PubMed cache."""
        self.search(pair_id)
        self.searched[pair_id].pmids.append(FetchedPmid(pmid=pmid, origin=origin))
        graphio.write_jsonl(self.work / records.PUBMED, self.searched.values())
        with PubMedCache() as cache:
            cache.put_abstracts([Abstract(pmid, f"Title {pmid}", text, year, "", types)])

    def answer(
        self,
        pair_id: str,
        pmid: str,
        *claims: dict,
        design: str = "rct",
        valid: bool = True,
        sentence_valid: bool = True,
        checked: bool = True,
    ) -> list[str]:
        """The label run's answer for an abstract, with code's checks of its claims; the claims'
        ids."""
        ids = [claim_id(c) for c in claims]
        with LabelCache(self.cache_dir / config.LABEL_CACHE) as cache:
            row = AbstractClaimsRow(pair_id, pmid, PV, MODEL, design, list(claims))
            row.answered_by = MODEL
            cache.put_abstract_claims([row])
            if checked:
                cache.put_claim_checks(
                    ClaimCheckRow(pair_id, pmid, PV, MODEL, cid, valid, sentence_valid)
                    for cid in ids
                )
        return ids

    def load(self, **kw) -> bundle.Bundles:
        run = records.read_label_run(self.work)
        return bundle.load(self.work, self.cache_dir, self.graph, run, **kw)


def new_world(tmp_path: Path) -> World:
    """A work directory with the toy entities and a label run, empty caches, the toy UBERON
    release, and the treats pair searched."""
    w = World(tmp_path / "work", tmp_path / "cache", tmp_path / "graph")
    graphio.write_jsonl(w.work / records.ENTITIES, [scope.entity_of(n) for n in NODES])
    records.write_label_run(w.work, LabelRun(model=MODEL, prompt_version=PV, prompt="label.md"))
    w.cache_dir.mkdir(parents=True)
    (w.cache_dir / config.UBERON_FILE).write_text(UBERON_OBO)
    LabelCache(w.cache_dir / config.LABEL_CACHE).close()
    PubMedCache().close()
    w.search(TREATS_PAIR)
    return w


@pytest.fixture
def world(tmp_path: Path) -> World:
    return new_world(tmp_path)


def test_a_pair_is_bundled_once_every_abstract_is_answered(world):
    world.edges(edge(IMATINIB, "biolink:treats", CML))
    world.abstract(TREATS_PAIR, "1", "Imatinib treats CML.")
    world.abstract(TREATS_PAIR, "2", "Imatinib was given.")
    world.abstract(TREATS_PAIR, "3", "")  # no text: never asked about
    world.answer(TREATS_PAIR, "1", stored(TREATS, sentence="Imatinib treats CML."))
    loaded = world.load()
    assert loaded.pairs == [] and loaded.unlabeled == [TREATS_PAIR]

    world.answer(TREATS_PAIR, "2")  # no claim
    [b] = world.load().pairs
    assert b.pair_id == TREATS_PAIR
    assert [e.publication for e in b.evidence] == ["PMID:1", "PMID:2"]
    assert [len(e.claims) for e in b.evidence] == [1, 0]
    assert len(b.claims) == len(b.keys) == 1


def test_a_searched_pair_with_no_abstracts_is_bundled_with_no_evidence(world):
    world.edges(edge(IMATINIB, "biolink:treats", CML))
    [b] = world.load().pairs
    assert b.evidence == () and b.claims == [] and len(b.edges) == 1


def test_edges_in_scope_with_their_ids(world):
    own = edge(IMATINIB, "biolink:treats", CML, id="drugcentral:1")
    no_id = edge(IMATINIB, "biolink:treats", CML, primary_knowledge_source="infores:chembl")
    earlier = edge(IMATINIB, "biolink:treats", CML, id=graphio.ID_PREFIX + "abc")
    unscoped = edge(IMATINIB, "biolink:contraindicated_in", CML)
    world.edges(own, no_id, scope_out=(earlier, unscoped))
    world.search(GENE_PAIR)  # searched, but no edge of it in scope
    loaded = world.load()
    [b] = loaded.pairs
    assert [e.id for e in b.edges] == [
        "drugcentral:1",
        graphio.edge_id(no_id.model_dump(exclude_none=True)),
    ]
    assert b.edges[1].id.startswith(graphio.EDGE_ID_PREFIX)
    # The pair's other edges come too, for proposals to link to; never one a run added.
    assert [e.predicate for e in b.others] == ["biolink:contraindicated_in"]
    assert b.others[0].id == graphio.edge_id(unscoped.model_dump(exclude_none=True))
    assert loaded.counts["pairs searched with no edge in scope"] == 1


def test_edges_come_with_their_publications_as_the_source_cited_them(world):
    """An earlier step 6 appended the PMIDs that support an edge to its publications, and
    listed them in lit_added_publications: they are taken back out, so the edge is refined as
    its source gave it."""
    cited = edge(IMATINIB, "biolink:treats", CML, id="t", publications=["PMID:1", "PMID:2"])
    world.edges(cited)
    path = world.graph / graphio.EDGES
    earlier = {**cited.model_dump(exclude_none=True), "lit_added_publications": ["PMID:2"]}
    graphio.write_jsonl(path, [earlier])
    [b] = world.load().pairs
    assert b.edges[0].publications == ["PMID:1"]


def test_evidence_keeps_what_pubmed_says_and_both_designs(world):
    world.edges(edge(IMATINIB, "biolink:treats", CML))
    world.abstract(TREATS_PAIR, "10", "a", types=["Randomized Controlled Trial", "Journal Article"])
    world.abstract(TREATS_PAIR, "9", "b", types=["Journal Article"], origin="source", year=None)
    world.abstract(TREATS_PAIR, "11", "c", types=["Retracted Publication", "Review"])
    world.answer(TREATS_PAIR, "10", design="cohort")
    world.answer(TREATS_PAIR, "9", design="animal")
    world.answer(TREATS_PAIR, "11", design="cohort")
    [b] = world.load().pairs
    nine, ten, eleven = b.evidence  # by PMID, as numbers
    assert (ten.study_design, ten.study_design_source, ten.extractor_study_design) == (
        "rct",
        "pubmed",
        "cohort",
    )
    assert ten.publication_types == ["Randomized Controlled Trial", "Journal Article"]
    assert (ten.supporting_document_type, ten.supporting_document_year) == ("research", 2020)
    assert ten.found_by == ["search"] and not ten.retracted
    assert (nine.study_design, nine.study_design_source) == ("animal", "extractor")
    assert nine.found_by == [] and nine.supporting_document_year is None
    assert eleven.retracted and eleven.study_design == "narrative_review"


def test_claims_failing_a_check_are_left_out(world):
    world.edges(edge(IMATINIB, "biolink:treats", CML))
    for pmid in "12":
        world.abstract(TREATS_PAIR, pmid, "Imatinib treats CML.")
    world.answer(TREATS_PAIR, "1", stored(TREATS, sentence="Imatinib treats CML."))
    world.answer(TREATS_PAIR, "2", stored(TREATS, sentence="Imatinib treats CML."), valid=False)
    loaded = world.load()
    [b] = loaded.pairs
    assert [len(e.claims) for e in b.evidence] == [1, 0]
    assert [k[1] for k in b.keys] == ["1"]
    assert loaded.counts["claims failing a check"] == 1


def test_a_pair_with_a_claim_never_checked_is_not_bundled(world):
    """A label run stopped between storing its answers and checking them leaves claims with no
    check: the pair isn't refined on the rest, but left unchecked, like one with an abstract
    unanswered, until a label run checks them."""
    world.edges(edge(IMATINIB, "biolink:treats", CML))
    for pmid in "12":
        world.abstract(TREATS_PAIR, pmid, "Imatinib treats CML.")
    world.answer(TREATS_PAIR, "1", stored(TREATS, sentence="Imatinib treats CML."))
    world.answer(TREATS_PAIR, "2", stored(TREATS, sentence="Imatinib treats CML."), checked=False)
    loaded = world.load()
    assert loaded.pairs == [] and loaded.unlabeled == [TREATS_PAIR]
    assert loaded.counts["pairs with claims never checked"] == 1


def test_claims_read_the_way_round_biolink_puts_them(world):
    """Biolink's domain and range set the subject and object order: 'CML treats imatinib' is
    read 'imatinib treats CML', and a predicate that fits neither way (a chemical has no
    phenotype) fails the claim; the stored claims, and so their keys, are as the model gave
    them."""
    world.edges(edge(IMATINIB, "biolink:treats", CML))
    world.abstract(TREATS_PAIR, "1", "x")
    backwards = stored(statement(CML, "biolink:treats", IMATINIB), sentence="x")
    misfit = stored(statement(IMATINIB, "biolink:has_phenotype", CML), sentence="x")
    [kept, _] = world.answer(TREATS_PAIR, "1", backwards, misfit)
    loaded = world.load()
    [b] = loaded.pairs
    [record] = b.evidence[0].claims
    assert record.statement.model_dump(exclude={"negated"}) == TREATS
    assert b.keys == ((TREATS_PAIR, "1", kept),)
    assert loaded.counts["claims failing a check"] == 1
    reread = bundle.claim_record(backwards, sentence_found=True)
    assert (reread.statement.subject, reread.statement.object) == (CML, IMATINIB)


def test_a_sentence_counts_as_found_from_its_repair(world):
    world.edges(edge(IMATINIB, "biolink:treats", CML))
    world.abstract(TREATS_PAIR, "1", "Imatinib treats CML.")
    world.abstract(TREATS_PAIR, "2", "Imatinib treats CML.")
    claim = stored(TREATS, sentence="imatinib cured CML")
    [fixed] = world.answer(TREATS_PAIR, "1", claim, sentence_valid=False)
    world.answer(TREATS_PAIR, "2", claim, sentence_valid=False)
    repairer, version = repair.current()
    with LabelCache(world.cache_dir / config.LABEL_CACHE) as cache:
        row = ClaimRepairRow(TREATS_PAIR, "1", PV, MODEL, fixed, repairer, version, "Imatinib")
        row.sentence_valid = True
        cache.put_claim_repairs([row])
    # Repairs count once label_run.json names them.
    before = world.load().pairs[0].evidence[0].claims[0]
    assert not before.evidence_sentence_valid and not before.evidence_sentence_repaired
    run = records.read_label_run(world.work)
    update = {"repairer": repairer, "repair_prompt_version": version}
    records.write_label_run(world.work, run.model_copy(update=update))
    [b] = world.load().pairs
    one, two = (e.claims[0] for e in b.evidence)
    assert (one.evidence_sentence, one.evidence_sentence_valid) == ("Imatinib", True)
    assert one.evidence_sentence_repaired
    assert (two.evidence_sentence, two.evidence_sentence_valid) == ("imatinib cured CML", False)


def test_records_carry_grounded_anatomy_species_and_who_answered(world):
    world.edges(edge(IMATINIB, "biolink:treats", CML))
    world.abstract(TREATS_PAIR, "1", "x")
    in_liver = stored(TREATS, sentence="x", anatomy="Liver", notes="45 mg", species=None)
    in_margin = stored(TREATS, sentence="x", anatomy="tumour margin")
    world.answer(TREATS_PAIR, "1", in_liver, in_margin)
    with PubMedCache() as cache:
        cache.put_groundings(
            [
                Grounding("liver", "anatomy", LIVER, "liver", "test"),
                Grounding("tumour margin", "anatomy", None, None, "test"),
            ]
        )
    [b] = world.load().pairs
    liver, margin = b.evidence[0].claims
    assert (liver.context.anatomy, liver.context.anatomy_name) == (LIVER, "Liver")
    assert (liver.context.species, liver.context.notes) == (None, "45 mg")
    assert (margin.context.anatomy, margin.context.anatomy_name) == (None, "tumour margin")
    assert margin.context.species == HUMAN and margin.answered_by == MODEL
    assert liver.evidence_sentence_valid and not liver.evidence_sentence_repaired


def test_a_pair_with_a_name_the_ground_step_never_looked_up_is_not_bundled(world):
    """A claim whose anatomy name has no groundings row would count in every context, with
    nothing said. Its pair is listed apart, for step 6 to hold, until a ground run looks the
    name up; a name it looked up and didn't ground is a note, as designed. A claim failing a
    check doesn't hold its pair: it counts for nothing anyway."""
    world.edges(edge(IMATINIB, "biolink:treats", CML))
    for pmid in "12":
        world.abstract(TREATS_PAIR, pmid, "x")
    world.answer(TREATS_PAIR, "1", stored(TREATS, sentence="x", anatomy="bone marrow"))
    world.answer(TREATS_PAIR, "2", stored(TREATS, sentence="x", anatomy="spleen"), valid=False)
    loaded = world.load()
    assert loaded.pairs == [] and loaded.unlabeled == []
    assert loaded.not_looked_up == [TREATS_PAIR]
    assert loaded.counts["pairs with anatomy names never grounded"] == 1
    with PubMedCache() as cache:
        cache.put_groundings([Grounding("bone marrow", "anatomy", None, None, "test")])
    loaded = world.load()
    [b] = loaded.pairs
    [record] = b.evidence[0].claims
    assert (record.context.anatomy, record.context.anatomy_name) == (None, "bone marrow")
    assert "pairs with anatomy names never grounded" not in loaded.counts
    assert loaded.not_looked_up == []


def test_reviews_are_the_given_reviewers_and_keys_follow_the_claims(world):
    world.edges(edge(IMATINIB, "biolink:treats", CML))
    world.abstract(TREATS_PAIR, "2", "x")
    world.abstract(TREATS_PAIR, "1", "x")
    a, b = stored(TREATS, sentence="x"), stored(TREATS, "negative", sentence="x")
    [first] = world.answer(TREATS_PAIR, "2", a)
    ids = world.answer(TREATS_PAIR, "1", b, a)
    with LabelCache(world.cache_dir / config.LABEL_CACHE) as cache:
        cache.put_claim_reviews(
            [
                ClaimReviewRow(TREATS_PAIR, "2", PV, MODEL, first, "rev", "v1", "reject", "no"),
                ClaimReviewRow(TREATS_PAIR, "1", PV, MODEL, ids[0], "rev", "v2", "upheld", "ok"),
            ]
        )
    [unreviewed] = world.load().pairs
    assert all(c.record.review is None for c in unreviewed.claims)
    assert unreviewed.keys == (
        (TREATS_PAIR, "1", ids[0]),
        (TREATS_PAIR, "1", ids[1]),
        (TREATS_PAIR, "2", first),
    )
    assert [c.pmid for c in unreviewed.claims] == ["PMID:1", "PMID:1", "PMID:2"]
    [one] = world.load(reviewer="rev", review_prompt_version="v1").pairs
    assert [c.record.review for c in one.claims] == [None, None, "reject"]
    assert one.claims[2].record.review_reason == "no"


def test_load_names_the_step_to_run_first(world):
    world.edges(edge(IMATINIB, "biolink:treats", CML))
    (world.cache_dir / config.UBERON_FILE).unlink()
    with pytest.raises(FileNotFoundError, match="kgb-augment ground"):
        world.load()
    (world.work / records.STATEMENTS).unlink()
    with pytest.raises(FileNotFoundError, match="kgb-augment select"):
        world.load()


def test_an_invalid_edge_in_scope_is_an_error(world):
    world.edges(edge(IMATINIB, "biolink:treats", CML))
    path = world.graph / graphio.EDGES
    bad = {"subject": IMATINIB, "predicate": "treats", "object": CML}
    graphio.write_jsonl(path, [*graphio.read_jsonl(path), bad])
    with pytest.raises(ValueError, match="edge 2 of .* is not a valid Edge"):
        world.load()


def test_an_id_two_different_edges_give_is_an_error(world):
    """KGX ids name one edge. Identical records may repeat (they are one edge, refined once),
    but two different records with one id, in scope or not, would get one edge's outcome."""
    treats = edge(IMATINIB, "biolink:treats", CML, id="dup")
    world.edges(treats, treats)
    assert [len(b.edges) for b in world.load().pairs] == [2]
    for other in (
        edge(IMATINIB, "biolink:treats", CML, id="dup", publications=["PMID:1"]),
        edge(IMATINIB, "biolink:contraindicated_in", CML, id="dup"),  # out of scope
    ):
        world.edges(treats, scope_out=(other,))
        with pytest.raises(ValueError, match="edges 1 and 2 of .* share the id 'dup'"):
            world.load()


def test_the_lattice_is_the_pinned_uberon_release(world):
    world.edges(edge(IMATINIB, "biolink:treats", CML))
    loaded = world.load()
    assert loaded.uberon_version == "uberon/releases/2026-11-05/uberon-basic.owl"
    assert isinstance(loaded.lattice, refine.Lattice)
    assert loaded.lattice.at_or_below(refine.ANATOMY, LIVER, "UBERON:0001007")
    assert loaded.categories[CML] == ["biolink:Disease"]
