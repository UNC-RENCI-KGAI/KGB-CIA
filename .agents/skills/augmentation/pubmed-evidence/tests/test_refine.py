"""Edge refinement (refine.py): the narrower relation, a claim's statements, which claims count,
support and PMIDs, proposals and links, species in proposals, and DESIGN.md's properties over
random claim sets. Biolink is real; UBERON is a hand-made closure.

The helpers at the top (nodes, contexts, LATTICE, CATEGORIES, st, expression, drug, genes,
claim and edge) are shared: other test files import them from here."""

from __future__ import annotations

import dataclasses
import random

import pytest
from pubmed_evidence import graphio, refine

from shared.schema import ClaimContext, ClaimRecord, Edge, PublicationEvidence, Statement

# A chemical and a gene, a drug and a disease, and two genes for symmetric predicates.
X, Y = "CHEBI:1", "NCBIGene:2"
D, DZ = "CHEBI:3", "MONDO:4"
TP53, MDM2 = "NCBIGene:7157", "NCBIGene:4193"
HUMAN, MOUSE, RAT = "NCBITaxon:9606", "NCBITaxon:10090", "NCBITaxon:10116"
LIVER, KIDNEY, DIGESTIVE, ORGAN = (
    "UBERON:0002107",
    "UBERON:0002113",
    "UBERON:0001007",
    "UBERON:0000062",
)
# Liver is part of the digestive system; liver and kidney are both organs, but neither is
# below the other.
LATTICE = refine.Lattice(anatomy={LIVER: {DIGESTIVE, ORGAN}, KIDNEY: {ORGAN}, DIGESTIVE: set()})
CATEGORIES = {
    X: ["biolink:SmallMolecule"],
    Y: ["biolink:Gene"],
    D: ["biolink:Drug"],
    DZ: ["biolink:Disease"],
    TP53: ["biolink:Gene"],
    MDM2: ["biolink:Gene"],
}

ASPECT, DIRECTION = "object_aspect_qualifier", "object_direction_qualifier"
SPECIES, ANATOMY = refine.SPECIES, refine.ANATOMY


def st(
    predicate: str = "affects",
    qp: str | None = None,
    subject: str = X,
    object: str = Y,
    negated: bool = False,
    **qualifiers: str,
) -> Statement:
    return Statement(
        subject=subject,
        predicate=f"biolink:{predicate}",
        object=object,
        qualified_predicate=f"biolink:{qp}" if qp else None,
        qualifiers=qualifiers,
        negated=negated,
    )


def expression(direction: str | None = None, **more: str) -> Statement:
    """X causes <direction> expression of Y, or X affects expression of Y."""
    if direction is None:
        return st(**{ASPECT: "expression"}, **more)
    return st(qp="causes", **{ASPECT: "expression", DIRECTION: direction}, **more)


def drug(predicate: str = "treats", negated: bool = False, **qualifiers: str) -> Statement:
    """D <predicate> DZ."""
    return st(predicate, subject=D, object=DZ, negated=negated, **qualifiers)


def genes(predicate: str, subject: str = TP53, object: str = MDM2, **qualifiers: str) -> Statement:
    """<subject> <predicate> <object>, between TP53 and MDM2."""
    return st(predicate, subject=subject, object=object, **qualifiers)


def claim(
    statement: Statement,
    polarity: str = "positive",
    pmid: str = "PMID:1",
    *,
    also: tuple[Statement, ...] = (),
    species: str | None = None,
    anatomy: str | None = None,
    anatomy_name: str | None = None,
    notes: str | None = None,
    review: str | None = "upheld",
    valid: bool = True,
    role: str = "primary_finding",
    retracted: bool = False,
) -> refine.Claim:
    """A claim of the statement, upheld and with its quote found unless told otherwise."""
    record = ClaimRecord(
        polarity=polarity,
        statement=statement,
        also_supports=list(also),
        claim_role=role,
        hedged=False,
        context=ClaimContext(
            species=species, anatomy=anatomy, anatomy_name=anatomy_name, notes=notes
        ),
        evidence_sentence="X did something to Y.",
        evidence_sentence_valid=valid,
        review=review,
    )
    return refine.Claim(pmid, record, retracted)


def edge(statement: Statement, id: str = "e:1", publications: tuple[str, ...] = ()) -> Edge:
    """An edge that makes the statement."""
    slots = dict(statement.qualifiers)
    if statement.qualified_predicate:
        slots["qualified_predicate"] = statement.qualified_predicate
    if statement.negated:
        slots["negated"] = True
    return Edge(
        id=id,
        subject=statement.subject,
        predicate=statement.predicate,
        object=statement.object,
        primary_knowledge_source="infores:example-source",
        knowledge_level="knowledge_assertion",
        agent_type="manual_agent",
        publications=list(publications),
        **slots,
    )


def refined(
    edges, claims, others=(), categories=CATEGORIES, species=True, **kwargs
) -> refine.PairRefinement:
    """The pair's refinement; without `species`, with the species taken out of its proposals, as
    step 6 does by default (rule 7)."""
    r = refine.refine_pair(edges, claims, LATTICE, categories, others, **kwargs)
    return r if species else refine.without_species(r, others)


def bare(statement: Statement) -> Statement:
    """The statement without its species."""
    qualifiers = {k: v for k, v in statement.qualifiers.items() if k != SPECIES}
    return statement.model_copy(update={"qualifiers": qualifiers})


TRIAL = drug("in_clinical_trials_for")


# --- The narrower relation ---


@pytest.mark.parametrize(
    "a, b, expected",
    [
        # A predicate below another, mixins included.
        (st("physically_interacts_with"), st("interacts_with"), True),
        (drug(), drug("treats_or_applied_or_studied_to_treat"), True),
        (TRIAL, drug("treats_or_applied_or_studied_to_treat"), True),
        (drug("ameliorates_condition"), drug(), True),  # a mixin of treats
        (TRIAL, drug(), False),
        (drug("treats_or_applied_or_studied_to_treat"), drug(), False),
        # An enum value below another: upregulated below increased, expression below abundance
        # below activity_or_abundance.
        (expression("upregulated"), expression("increased"), True),
        (expression("increased"), expression("upregulated"), False),
        (st(**{ASPECT: "expression"}), st(**{ASPECT: "activity_or_abundance"}), True),
        (st(**{ASPECT: "activity"}), st(**{ASPECT: "expression"}), False),
        # qualified_predicate: needed when the broader statement has it.
        (st(**{ASPECT: "expression", DIRECTION: "increased"}), expression("increased"), False),
        (expression("increased"), st(**{ASPECT: "expression", DIRECTION: "increased"}), True),
        # Anatomy through UBERON's closure, species by equality.
        (st(**{ANATOMY: LIVER}), st(**{ANATOMY: DIGESTIVE}), True),
        (st(**{ANATOMY: DIGESTIVE}), st(**{ANATOMY: LIVER}), False),
        (st(**{ANATOMY: LIVER}), st(**{ANATOMY: KIDNEY}), False),
        (st(**{SPECIES: HUMAN}), st(**{SPECIES: HUMAN}), True),
        (st(**{SPECIES: HUMAN}), st(**{SPECIES: MOUSE}), False),
        # An added qualifier is narrower; E's qualifier missing is not.
        (expression("increased"), st(), True),
        (st(**{SPECIES: HUMAN}), st(), True),
        (st(), st(**{SPECIES: HUMAN}), False),
        (expression(), expression("increased"), False),
        # The other way round is not.
        (st(subject=Y, object=X), st(), False),
        # A negated statement entails nothing, and nothing entails one.
        (st(negated=True), st(negated=True), False),
        (expression("increased", negated=True), st(negated=True), False),
        (st(), st(negated=True), False),
        (st(negated=True), st(), False),
    ],
)
def test_entails(a, b, expected):
    assert LATTICE.entails(a, b) is expected


def test_without_a_closure_anatomy_compares_by_equality():
    bare = refine.Lattice()
    assert not bare.entails(st(**{ANATOMY: LIVER}), st(**{ANATOMY: DIGESTIVE}))
    assert bare.entails(st(**{ANATOMY: LIVER}), st(**{ANATOMY: LIVER}))


def test_narrower_reads_a_symmetric_statement_either_way_round():
    ab, ba = genes("physically_interacts_with"), genes("physically_interacts_with", MDM2, TP53)
    for broader in (genes("interacts_with"), genes("interacts_with", MDM2, TP53)):
        assert refine.narrower(LATTICE, ab, broader)
        assert refine.narrower(LATTICE, ba, broader)
        assert refine.strictly_narrower(LATTICE, ba, broader)
    assert refine.narrower(LATTICE, ab, ba)
    assert not refine.strictly_narrower(LATTICE, ab, ba)
    # A predicate that isn't symmetric compares only as written.
    assert not refine.narrower(LATTICE, genes("regulates", MDM2, TP53), genes("regulates"))


# --- Reading statements ---


def test_statement_of_an_edge():
    s = expression("increased", **{ANATOMY: LIVER})
    e = edge(s)
    assert refine.statement_of(e) == s
    assert refine.statement_of(e).id == graphio.edge_statement_id(e)
    assert refine.statement_of(edge(drug(negated=True))).negated


def test_statements_read_the_way_round_biolink_puts_them():
    """A predicate fits its subject and object by its domain and range. One defined the other
    way round becomes its inverse; a non-canonical one is turned round to its canonical
    inverse, each side's qualifiers and `causes` with it."""
    gene_disease = st("gene_associated_with_condition", subject=Y, object=DZ)
    wrong = st("condition_associated_with_gene", subject=Y, object=DZ)
    assert refine.directed(wrong, CATEGORIES) == gene_disease
    assert refine.directed(gene_disease, CATEGORIES) is gene_disease
    lof = "loss_of_function_variant_form"
    from_disease = st(
        "gene_associated_with_condition", subject=DZ, object=Y, object_form_or_variant_qualifier=lof
    )
    assert refine.directed(from_disease, CATEGORIES) == st(
        "gene_associated_with_condition",
        subject=Y,
        object=DZ,
        subject_form_or_variant_qualifier=lof,
    )
    inverse = st("condition_associated_with_gene", subject=DZ, object=Y)
    assert refine.directed(inverse, CATEGORIES) == gene_disease
    affected = st("affected_by", "causes", subject=Y, object=X, **{ASPECT: "activity"})
    assert refine.directed(affected, CATEGORIES) == st(
        "affects", "caused_by", subject=X, object=Y, subject_aspect_qualifier="activity"
    )
    # Symmetric: as written either way round; neither way: None.
    binds = st("binds", subject=Y, object=X)
    assert refine.directed(binds, CATEGORIES) is binds
    assert refine.directed(st("has_adverse_event"), CATEGORIES) is None
    # Without the nodes' categories nothing is wrong, but the canonical direction is known.
    assert refine.directed(st("treated_by", subject=DZ, object=D), {}) == drug()


def test_readings_and_statement_key():
    ab, ba = genes("interacts_with"), genes("interacts_with", MDM2, TP53)
    assert refine.readings(ab) == (ab, ba)
    assert refine.statement_key(ab) == refine.statement_key(ba) == ba.id  # MDM2's id is lower
    assert refine.readings(st()) == (st(),)
    assert refine.statement_key(st()) == st().id


def test_claims_of_keeps_order_and_retraction():
    def abstract(pmid: str, retracted: bool, claims: list[ClaimRecord]) -> PublicationEvidence:
        return PublicationEvidence(
            publication=pmid,
            publication_types=["Journal Article"],
            supporting_document_type="research",
            retracted=retracted,
            found_by=["search"],
            study_design="rct",
            study_design_source="extractor",
            claims=claims,
        )

    a, b, c = (claim(st(), pmid=f"PMID:{i}").record for i in range(3))
    evidence = [abstract("PMID:1", False, [a, b]), abstract("PMID:2", True, [c])]
    assert refine.claims_of(evidence) == [
        refine.Claim("PMID:1", a, False),
        refine.Claim("PMID:1", b, False),
        refine.Claim("PMID:2", c, True),
    ]


# --- Support and PMIDs ---


def test_a_claim_equal_to_e_supports_it_and_proposes_nothing():
    r = refined([edge(expression("increased"))], [claim(expression("increased"))])
    (o,) = r.edges
    assert (o.edge_id, o.statement) == ("e:1", expression("increased"))
    assert (o.supported, o.score, o.claims, o.pmids, o.added) == (
        True,
        1,
        (0,),
        ("PMID:1",),
        ("PMID:1",),
    )
    assert o.refined_by == ()
    assert (r.proposed, r.links, r.unreviewed, r.held) == ((), (), (), False)


def test_a_narrower_claim_supports_e_and_proposes_its_statement():
    r = refined([edge(st())], [claim(expression("increased"), pmid="PMID:7")])
    (o,) = r.edges
    (p,) = r.proposed
    assert (o.supported, o.pmids, o.refined_by) == (True, ("PMID:7",), (p.id,))
    assert (p.key, p.statement) == (expression("increased").id, expression("increased"))
    assert p.id == graphio.proposed_edge_id(p.key)
    assert p.id.startswith(graphio.ID_PREFIX)
    assert (p.claims, p.pmids, p.score, p.original_edge_ids, p.existing) == (
        (0,),
        ("PMID:7",),
        1,
        ("e:1",),
        (),
    )


def test_the_score_counts_pmids_e_cites_and_added_leaves_them_out():
    e = edge(st(), publications=("PMID:2", "doi:10.1/x"))
    claims = [claim(st(), pmid="PMID:10"), claim(st(), pmid="PMID:2"), claim(st(), pmid="PMID:9")]
    (o,) = refined([e], claims).edges
    assert o.pmids == ("PMID:2", "PMID:9", "PMID:10")  # by number
    assert o.score == 3
    assert o.added == ("PMID:9", "PMID:10")


def test_one_pmid_with_two_claims_counts_once():
    claims = [claim(st(), pmid="PMID:3"), claim(expression("increased"), pmid="PMID:3")]
    r = refined([edge(st())], claims)
    (o,) = r.edges
    assert (o.claims, o.pmids, o.score) == ((0, 1), ("PMID:3",), 1)
    assert r.proposed[0].pmids == ("PMID:3",)


# --- Context ---


def test_a_claim_in_the_liver_supports_e_in_the_digestive_system_and_proposes_e_in_the_liver():
    e = st(**{ANATOMY: DIGESTIVE})
    r = refined([edge(e)], [claim(st(), anatomy=LIVER, anatomy_name="liver")])
    (o,) = r.edges
    (p,) = r.proposed
    assert o.supported
    assert p.statement == st(**{ANATOMY: LIVER})
    assert o.context_notes == ()  # a grounded name is no note


def test_a_claim_without_anatomy_does_not_support_e_in_the_liver():
    (o,) = refined([edge(st(**{ANATOMY: LIVER}))], [claim(st())]).edges
    assert not o.supported
    assert (o.score, o.claims, o.pmids, o.added) == (0, (), (), ())


def test_context_is_added_where_the_association_allows_it():
    """Species and anatomy for a chemical and a gene; species alone for a drug that treats a
    disease, whose association has no anatomical context. Without categories, both."""
    both = {SPECIES: HUMAN, ANATOMY: LIVER}
    c = claim(st(), species=HUMAN, anatomy=LIVER)
    assert refine.statements_of(c, CATEGORIES) == (st(**both),)
    treats = claim(drug(), species=HUMAN, anatomy=LIVER)
    assert refine.statements_of(treats, CATEGORIES) == (drug(**{SPECIES: HUMAN}),)
    assert refine.statements_of(treats) == (drug(**both),)
    assert refine.statements_of(treats, {}) == (drug(**both),)
    # A symmetric statement written gene first takes the anatomy its other reading allows.
    turned = claim(st("physically_interacts_with", subject=Y, object=X), anatomy=LIVER)
    assert refine.statements_of(turned, CATEGORIES) == (
        st("physically_interacts_with", subject=Y, object=X, **{ANATOMY: LIVER}),
    )
    # A slot the statement sets itself stays as it is.
    own = claim(st(**{ANATOMY: KIDNEY}), anatomy=LIVER)
    assert refine.statements_of(own, CATEGORIES) == (st(**{ANATOMY: KIDNEY}),)


def test_a_human_claim_proposes_e_in_human():
    r = refined([edge(drug())], [claim(drug(), species=HUMAN, anatomy=LIVER)])
    (o,) = r.edges
    (p,) = r.proposed
    assert o.supported
    assert p.statement == drug(**{SPECIES: HUMAN})


def test_an_ungrounded_anatomy_name_adds_nothing_and_is_a_context_note():
    c = claim(st(), anatomy_name="hepatic stellate cells", notes="HepG2 cells")
    assert refine.statements_of(c, CATEGORIES) == (st(),)
    r = refined([edge(st())], [c])
    (o,) = r.edges
    assert o.supported and r.proposed == ()
    assert o.context_notes == ("HepG2 cells", "hepatic stellate cells")
    r = refined([edge(st())], [claim(expression("increased"), anatomy_name="islets")])
    assert r.proposed[0].context_notes == r.edges[0].context_notes == ("islets",)


# --- Which claims count ---


def test_a_positive_claims_also_supports_count():
    c = claim(drug(), also=(TRIAL,))
    assert refine.statements_of(c) == (drug(), TRIAL)
    r = refined([edge(TRIAL)], [c])
    assert r.edges[0].supported


def test_a_negative_claims_also_supports_count_but_not_its_statement():
    """Rule 8: a failed trial's in_clinical_trials_for counts; what it denies doesn't."""
    c = claim(drug(), "negative", also=(TRIAL,))
    assert refine.statements_of(c) == (TRIAL,)
    r = refined([edge(drug(), id="e:treats"), edge(TRIAL, id="e:trial")], [c])
    assert [o.supported for o in r.edges] == [False, True]
    assert refine.statements_of(claim(drug(), "negative")) == ()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"review": "reject"},
        {"valid": False},
        {"retracted": True},  # rule 9
        {"role": "hypothesis"},  # rule 10
    ],
)
def test_claims_that_do_not_count(kwargs):
    c = claim(expression("increased"), **kwargs)
    r = refined([edge(st())], [c])
    assert not r.edges[0].supported
    assert r.proposed == r.links == ()
    assert not refine.eligible(c)


def test_background_and_hedged_claims_count():
    background = claim(st(), role="background")
    record = background.record.model_copy(update={"claim_role": "primary_finding", "hedged": True})
    hedged = dataclasses.replace(background, pmid="PMID:2", record=record)
    (o,) = refined([edge(st())], [background, hedged]).edges
    assert o.pmids == ("PMID:1", "PMID:2")


def test_unreviewed_claims_hold_the_pair_only_when_they_would_count():
    claims = [
        claim(st(), pmid="PMID:1"),
        claim(expression("increased"), pmid="PMID:2", review=None),
        claim(st("interacts_with"), pmid="PMID:3", review=None),  # supports nothing
        claim(st(), "negative", pmid="PMID:4", review=None),
        claim(st(), pmid="PMID:5", review=None, valid=False),
    ]
    r = refined([edge(st())], claims)
    assert (r.unreviewed, r.held) == ((1,), True)
    # Unreviewed, it doesn't count: no PMID and no proposal from it.
    assert (r.edges[0].pmids, r.proposed) == (("PMID:1",), ())
    assert refine.would_count([edge(st())], claims, LATTICE, CATEGORIES) == (0, 1)
    r = refined([edge(st())], [claims[0], *claims[2:]])
    assert (r.unreviewed, r.held) == ((), False)


def test_without_require_review_unreviewed_claims_count():
    claims = [
        claim(st(), pmid="PMID:1"),
        claim(expression("increased"), pmid="PMID:2", review=None),
        claim(st(), pmid="PMID:3", review="reject"),  # still doesn't count
    ]
    r = refined([edge(st())], claims, require_review=False)
    assert (r.unreviewed, r.held) == ((1,), False)
    assert r.edges[0].pmids == ("PMID:1", "PMID:2")
    assert r.proposed[0].statement == expression("increased")


def test_would_count_ignores_the_review():
    """Step 5 reviews every claim that would count if upheld: a rejected claim would too."""
    claims = [
        claim(expression("increased"), review="reject"),
        claim(st(), review=None),
        claim(st()),
        claim(st(), review=None, role="hypothesis"),
        claim(st(), review=None, retracted=True),
        claim(drug(), review=None),  # another pair's statement: supports nothing here
    ]
    assert refine.would_count([edge(st())], claims, LATTICE) == (0, 1, 2)
    assert refine.would_count([], claims, LATTICE) == ()


def test_a_negated_e_is_never_supported():
    r = refined([edge(st(negated=True))], [claim(st()), claim(st(), "negative")])
    assert not r.edges[0].supported
    assert (r.proposed, r.links, r.unreviewed) == ((), (), ())


# --- Proposals and links ---


def test_two_originals_refined_by_one_statement_give_one_proposal():
    claims = [claim(expression("increased"), pmid=f"PMID:{i}") for i in (4, 3)]
    r = refined([edge(st(), id="e:1"), edge(expression(), id="e:2")], claims)
    (p,) = r.proposed
    assert p.statement == expression("increased")
    assert (p.original_edge_ids, p.claims, p.pmids) == (
        ("e:1", "e:2"),
        (0, 1),
        ("PMID:3", "PMID:4"),
    )
    assert [o.refined_by for o in r.edges] == [(p.id,), (p.id,)]


def test_a_proposal_an_edge_of_the_pair_makes_is_a_link():
    """Rule 5: in scope, the edge is supported by the claim itself; left out by select, it
    is linked to and not duplicated. Either way E names it in refined_by."""
    claims = [claim(expression("increased"))]
    inside = refined([edge(st(), id="e:1"), edge(expression("increased"), id="e:2")], claims)
    assert inside.proposed == ()
    (link,) = inside.links
    assert (link.statement, link.existing, link.original_edge_ids) == (
        expression("increased"),
        ("e:2",),
        ("e:1",),
    )
    assert [o.refined_by for o in inside.edges] == [("e:2",), ()]
    assert [o.supported for o in inside.edges] == [True, True]

    others = [edge(st("interacts_with"), id="e:other"), edge(expression("increased"), id="e:out")]
    outside = refined([edge(st(), id="e:1")], claims, others)
    assert outside.proposed == ()
    (link,) = outside.links
    assert (link.existing, link.pmids, link.key) == (("e:out",), ("PMID:1",), link.statement.id)
    assert outside.edges[0].refined_by == ("e:out",)
    # Without that edge, the proposal is a new edge.
    (p,) = refined([edge(st(), id="e:1")], claims, others[:1]).proposed
    assert p.statement == expression("increased")


def test_a_symmetric_proposal_either_way_round_is_one_edge():
    """'TP53 interacts with MDM2' and 'MDM2 interacts with TP53' both refine to physically
    interacts: one statement, so one proposed edge, written with its nodes in id order whichever
    way round its claims and originals read."""
    claims = [claim(genes("physically_interacts_with"), pmid=f"PMID:{i}") for i in (1, 2)]
    claims.append(claim(genes("physically_interacts_with", MDM2, TP53), pmid="PMID:3"))
    edges = [
        edge(genes("interacts_with"), id="e:ab"),
        edge(genes("interacts_with", MDM2, TP53), id="e:ba"),
    ]
    r = refined(edges, claims)
    (p,) = r.proposed
    turned = genes("physically_interacts_with", MDM2, TP53)
    assert p.statement == turned  # MDM2's id is lower
    assert p.key == refine.statement_key(genes("physically_interacts_with")) == turned.id
    assert (p.original_edge_ids, p.claims) == (("e:ab", "e:ba"), (0, 1, 2))
    assert [o.refined_by for o in r.edges] == [(p.id,), (p.id,)]
    assert [o.pmids for o in r.edges] == [("PMID:1", "PMID:2", "PMID:3")] * 2
    (q,) = refined(edges[::-1], claims[::-1]).proposed
    assert (q.id, q.statement, q.original_edge_ids) == (p.id, turned, ("e:ba", "e:ab"))
    # Made already the other way round: a link.
    original = edge(genes("physically_interacts_with"), id="e:piw")
    (link,) = refined(edges[:1], claims, [original]).links
    assert link.existing == ("e:piw",)


def test_a_symmetric_proposal_is_written_the_way_round_biolink_allows_it():
    """Biolink gives a chemical and a gene an anatomical context only with the chemical as
    subject: in id order the gene would come first, so the proposal reads the other way."""
    chemical = "PUBCHEM.COMPOUND:5"
    categories = {**CATEGORIES, chemical: ["biolink:SmallMolecule"]}
    interacts = st("interacts_with", subject=chemical, object=Y)
    in_liver = st("physically_interacts_with", subject=chemical, object=Y, **{ANATOMY: LIVER})
    assert Y < chemical
    assert not refine.valid(refine.readings(in_liver)[1], categories)
    c = claim(st("physically_interacts_with", subject=chemical, object=Y), anatomy=LIVER)
    r = refined([edge(interacts)], [c], categories=categories)
    (p,) = r.proposed
    assert p.statement == in_liver
    assert p.key == refine.readings(in_liver)[1].id


def test_an_invalid_proposal_is_dropped_and_e_stays_supported():
    """A gold-style claim, which skips label's checks, with a slot its association doesn't
    allow: a drug that treats a disease has no anatomical context."""
    c = claim(drug(**{ANATOMY: LIVER}))
    assert not refine.valid(drug(**{ANATOMY: LIVER}), CATEGORIES)
    r = refined([edge(drug())], [c])
    assert r.proposed == r.links == ()
    (o,) = r.edges
    assert (o.supported, o.refined_by) == (True, ())
    # Without categories, nothing is checked.
    assert refined([edge(drug())], [c], categories=None).proposed[0].statement == c.record.statement


@pytest.mark.parametrize(
    "statement, expected",
    [
        (expression("increased"), True),
        (st(**{SPECIES: HUMAN, ANATOMY: LIVER}), True),
        (st("has_adverse_event"), False),  # the predicate doesn't fit a chemical and a gene
        (drug(**{ANATOMY: LIVER}), False),  # a slot the association doesn't allow
        (st(**{DIRECTION: "sideways"}), False),  # a value the enum doesn't have
        (st(qp="affects", **{ASPECT: "expression"}), False),  # qualified_predicate: causes only
        (st("has_adverse_event", subject="CHEBI:99"), True),  # a node without categories
    ],
)
def test_valid(statement, expected):
    assert refine.valid(statement, CATEGORIES) is expected


# --- Species in proposals (rule 7) ---

ACTIVITY = st(qp="causes", **{ASPECT: "activity", DIRECTION: "increased"})


def test_the_specs_example_with_species_in_proposals_and_without():
    """E is 'X affects Y'. Abstract 1 says X increases Y's activity, abstract 2 says so in
    human. With species in proposals, two proposals, the second in human; without, one, with
    both PMIDs. Either way E gains both."""
    claims = [claim(ACTIVITY, pmid="PMID:1"), claim(ACTIVITY, pmid="PMID:2", species=HUMAN)]
    on = refined([edge(st())], claims)
    in_human = ACTIVITY.model_copy(update={"qualifiers": {**ACTIVITY.qualifiers, SPECIES: HUMAN}})
    assert {p.key: p.pmids for p in on.proposed} == {
        ACTIVITY.id: ("PMID:1",),
        in_human.id: ("PMID:2",),
    }
    assert on.folded == ()
    off = refined([edge(st())], claims, species=False)
    (p,) = off.proposed
    assert (p.statement, p.claims, p.pmids, p.original_edge_ids, p.existing) == (
        ACTIVITY,
        (0, 1),
        ("PMID:1", "PMID:2"),
        ("e:1",),
        (),
    )
    assert off.edges[0].refined_by == (p.id,)
    assert off.folded == tuple(q for q in on.proposed if q.key == in_human.id)
    for r in (on, off):
        assert (r.edges[0].pmids, r.edges[0].added) == (("PMID:1", "PMID:2"),) * 2
    # No species in any proposal: the same refinement.
    plain = refined([edge(st())], claims[:1])
    assert refine.without_species(plain) is plain


def test_proposals_that_merge_keep_the_notes_of_each():
    """Without the species, the activity claims in human and in mouse make one proposal, and its
    context notes are both claims' notes, as the proposal of a claim without a species has its
    claims' notes."""
    claims = [
        claim(ACTIVITY, pmid="PMID:1", species=HUMAN, notes="HepG2 cells"),
        claim(ACTIVITY, pmid="PMID:2", species=MOUSE, notes="pancreatic islets"),
    ]
    on = refined([edge(st())], claims)
    assert sorted(p.context_notes for p in on.proposed) == [
        ("HepG2 cells",),
        ("pancreatic islets",),
    ]
    (p,) = refined([edge(st())], claims, species=False).proposed
    assert p.context_notes == ("HepG2 cells", "pancreatic islets")


def test_a_proposal_that_becomes_the_edge_it_refines_is_dropped():
    """'D treats DZ in human' without its species is E itself: no proposal, and E keeps its
    support and PMIDs. Where E in human is an edge of the pair too, E no longer links to it, and
    it is still supported."""
    r = refined([edge(drug())], [claim(drug(), species=HUMAN, pmid="PMID:2")], species=False)
    assert (r.proposed, r.links) == ((), ())
    (o,) = r.edges
    assert (o.supported, o.pmids, o.added, o.refined_by) == (True, ("PMID:2",), ("PMID:2",), ())
    assert [p.statement for p in r.folded] == [drug(**{SPECIES: HUMAN})]

    edges = [edge(drug(), id="e:1"), edge(drug(**{SPECIES: HUMAN}), id="e:human")]
    r = refined(edges, [claim(drug(), species=HUMAN)], species=False)
    assert (r.proposed, r.links) == ((), ())
    assert [o.refined_by for o in r.edges] == [(), ()]
    assert [o.supported for o in r.edges] == [True, True]
    assert [p.existing for p in r.folded] == [("e:human",)]


def test_a_proposal_that_becomes_an_edge_of_the_pair_is_a_link_to_it():
    """'X increases Y's expression in human' refines 'X affects Y'. Without its species, it is
    what an edge select left out says: a link to that edge (rule 5), merged with the link a claim
    without a species makes. Where an in-scope edge says it, it no longer refines that edge, and
    links the broader one to it: the same as a claim without a species gives."""
    human = claim(expression("increased"), pmid="PMID:2", species=HUMAN)
    plain = claim(expression("increased"), pmid="PMID:1")
    out = [edge(expression("increased"), id="o:1")]
    (p,) = refined([edge(st())], [human], out).proposed
    assert p.statement == expression("increased", **{SPECIES: HUMAN})
    for claims in ([human], [plain, human]):
        r = refined([edge(st())], claims, out, species=False)
        assert r.proposed == ()
        (link,) = r.links
        assert (link.statement, link.existing, link.original_edge_ids) == (
            expression("increased"),
            ("o:1",),
            ("e:1",),
        )
        assert link.pmids == tuple(c.pmid for c in claims)
        assert r.edges[0].refined_by == ("o:1",)

    edges = [edge(st(), id="e:1"), edge(expression("increased"), id="e:2")]
    (p,) = refined(edges, [human]).proposed
    assert p.original_edge_ids == ("e:1", "e:2")
    off = refined(edges, [human], species=False)
    without = refined(edges, [claim(expression("increased"), pmid="PMID:2")])
    assert off.proposed == without.proposed == ()
    assert off.links == without.links
    (link,) = off.links
    assert (link.existing, link.original_edge_ids, link.pmids) == (("e:2",), ("e:1",), ("PMID:2",))
    assert [o.refined_by for o in off.edges] == [o.refined_by for o in without.edges]
    assert [o.refined_by for o in off.edges] == [("e:2",), ()]
    assert [o.pmids for o in off.edges] == [("PMID:2",)] * 2


@pytest.mark.parametrize(
    "original, said",
    [
        (st(**{SPECIES: HUMAN}), expression("increased")),
        (drug(**{SPECIES: HUMAN}), drug("ameliorates_condition")),
    ],
)
def test_a_proposal_keeps_the_species_of_an_original_it_refines(original, said):
    """'X affects Y in human' is refined by 'X increases Y expression in human'. Without the
    species, the proposal would not be narrower than it, so it keeps the species with the flag
    off too. A claim of the same without a species supports no edge, and adds no PMID."""
    edges = [edge(original, id="e:human")]
    claims = [claim(said, species=HUMAN), claim(said, pmid="PMID:2")]
    on = refined(edges, claims)
    off = refined(edges, claims, species=False)
    assert off == on
    (p,) = off.proposed
    in_human = said.model_copy(update={"qualifiers": {**said.qualifiers, SPECIES: HUMAN}})
    assert (p.statement, p.pmids) == (in_human, ("PMID:1",))
    assert refine.strictly_narrower(LATTICE, p.statement, original)
    assert off.edges[0].refined_by == (p.id,)


def test_a_proposal_keeps_its_species_when_its_originals_mix_one_with_it_and_one_without():
    """Edges in human and in mouse, and one without a species, are refined by what their claims
    say in each. Each proposal keeps its species and refines both its originals; a claim in
    rat, whose proposal refines only the edge without a species, loses its species, and merges
    with a claim that names none."""
    edges = [
        edge(st(), id="e:1"),
        edge(st(**{SPECIES: HUMAN}), id="e:human"),
        edge(st(**{SPECIES: MOUSE}), id="e:mouse"),
    ]
    claims = [
        claim(expression("increased"), pmid="PMID:1", species=HUMAN),
        claim(expression("increased"), pmid="PMID:2", species=MOUSE),
    ]
    on = refined(edges, claims)
    off = refined(edges, claims, species=False)
    assert off == on
    assert [
        (p.pmids, p.original_edge_ids) for p in sorted(off.proposed, key=lambda p: p.pmids)
    ] == [
        (("PMID:1",), ("e:1", "e:human")),
        (("PMID:2",), ("e:1", "e:mouse")),
    ]
    rat = claim(expression("increased"), pmid="PMID:3", species=RAT)
    plain = claim(expression("increased"), pmid="PMID:4")
    on = refined(edges, [*claims, rat, plain])
    off = refined(edges, [*claims, rat, plain], species=False)
    assert [p.statement.qualifiers.get(SPECIES) for p in off.folded] == [RAT]
    by_species = {p.statement.qualifiers.get(SPECIES): p for p in off.proposed}
    assert set(by_species) == {HUMAN, MOUSE, None}
    plain_one = by_species[None]
    assert (plain_one.statement, plain_one.pmids, plain_one.original_edge_ids) == (
        expression("increased"),
        ("PMID:3", "PMID:4"),
        ("e:1",),
    )
    assert by_species[HUMAN] in on.proposed and by_species[MOUSE] in on.proposed
    assert [o.refined_by for o in off.edges] == [
        tuple(sorted(p.id for p in off.proposed)),
        (by_species[HUMAN].id,),
        (by_species[MOUSE].id,),
    ]
    assert [o.pmids for o in off.edges] == [o.pmids for o in on.edges]


# --- Errors ---


def test_an_edge_needs_an_id():
    nameless = edge(st()).model_copy(update={"id": None})
    with pytest.raises(ValueError, match="needs its id"):
        refined([nameless], [])
    with pytest.raises(ValueError, match="needs its id"):
        refined([edge(st())], [], [nameless])


# --- The properties, over random claim sets ---

CHEM_POOL = [
    st(),
    st("interacts_with"),
    st("physically_interacts_with"),
    st("regulates"),
    expression(),
    expression("increased"),
    expression("decreased"),
    expression("upregulated"),
    st(qp="causes", **{ASPECT: "activity", DIRECTION: "increased"}),
    st(qp="causes", **{ASPECT: "activity", DIRECTION: "decreased"}),
    st(**{ASPECT: "abundance"}),
    st(**{ASPECT: "activity_or_abundance"}),
]
CHEM_EDGES = [
    st(),
    st("interacts_with"),
    expression(),
    expression("increased"),
    st(**{ANATOMY: DIGESTIVE}),
    expression(**{SPECIES: HUMAN}),
]
DRUG_POOL = [
    drug(),
    drug("ameliorates_condition"),
    drug("preventative_for_condition"),
    drug("contraindicated_in"),
    TRIAL,
    drug("in_preclinical_trials_for"),
    drug("treats_or_applied_or_studied_to_treat"),
    drug("associated_with"),
]
DRUG_EDGES = [
    drug(),
    drug("treats_or_applied_or_studied_to_treat"),
    TRIAL,
    drug(**{SPECIES: HUMAN}),
]
GENE_POOL = [
    genes(p, a, b)
    for p in (
        "interacts_with",
        "physically_interacts_with",
        "directly_physically_interacts_with",
        "genetically_interacts_with",
        "regulates",
    )
    for a, b in ((TP53, MDM2), (MDM2, TP53))
]
GENE_EDGES = [
    genes("interacts_with"),
    genes("interacts_with", MDM2, TP53),
    genes("physically_interacts_with", MDM2, TP53),
]
FAMILIES = [(CHEM_POOL, CHEM_EDGES), (DRUG_POOL, DRUG_EDGES), (GENE_POOL, GENE_EDGES)]
TRIALS = 300


def random_claim(rng: random.Random, pool: list[Statement], **fixed) -> refine.Claim:
    """A claim with a random statement, polarity, context, verdict, quote check, role and
    retraction, from a small pool of PMIDs so that they repeat; `fixed` overrides any."""
    anatomy = rng.choice([None, None, LIVER, KIDNEY, DIGESTIVE])
    drawn = {
        "polarity": rng.choice(["positive"] * 3 + ["negative"]),
        "pmid": f"PMID:{rng.randint(1, 8)}",
        "also": tuple(rng.sample(pool, rng.choice([0, 0, 1]))),
        "species": rng.choice([None, None, HUMAN, MOUSE]),
        "anatomy": anatomy,
        "anatomy_name": rng.choice([None, "islets"]) if anatomy is None else None,
        "notes": rng.choice([None, None, "HepG2 cells"]),
        "review": rng.choice(["upheld", "upheld", "reject", None]),
        "valid": rng.random() < 0.9,
        "role": rng.choice(["primary_finding"] * 6 + ["background", "hypothesis"]),
        "retracted": rng.random() < 0.05,
    }
    statement = fixed.pop("statement", None) or rng.choice(pool)
    return claim(statement, **{**drawn, **fixed})


def random_case(rng: random.Random):
    """A pair's in-scope edges and the edges select left out, its claims, the family's
    statements, and whether review is required."""
    pool, scoped = rng.choice(FAMILIES)
    edges = [edge(s, id=f"e:{i}") for i, s in enumerate(rng.sample(scoped, rng.randint(1, 3)))]
    left_out = rng.sample(pool + scoped, rng.randint(0, 2))
    others = [edge(s, id=f"o:{i}") for i, s in enumerate(left_out)]
    claims = [random_claim(rng, pool) for _ in range(rng.randint(0, 14))]
    return edges, others, claims, pool, rng.random() < 0.7


def counting_claim(rng: random.Random, pool: list[Statement]) -> refine.Claim:
    return random_claim(
        rng,
        pool,
        review="upheld",
        valid=True,
        role=rng.choice(["primary_finding", "background"]),
        retracted=False,
    )


def counts(c: refine.Claim, require_review: bool) -> bool:
    """Rule 1 and rules 9 and 10, written out apart from refine.py's own."""
    r = c.record
    reviewed = r.review == "upheld" or (r.review is None and not require_review)
    return (
        reviewed and r.evidence_sentence_valid and not c.retracted and r.claim_role != "hypothesis"
    )


def mapped(r: refine.PairRefinement, origin: list[int | None]) -> refine.PairRefinement:
    """The refinement with each claim index replaced by `origin`'s for it, sorted, so that runs
    over the same claims in another order, or with others added, compare equal."""

    def back(indices: tuple[int, ...]) -> tuple:
        return tuple(sorted(origin[i] for i in indices))

    return dataclasses.replace(
        r,
        edges=tuple(dataclasses.replace(o, claims=back(o.claims)) for o in r.edges),
        proposed=tuple(dataclasses.replace(p, claims=back(p.claims)) for p in r.proposed),
        links=tuple(dataclasses.replace(p, claims=back(p.claims)) for p in r.links),
        unreviewed=back(r.unreviewed),
        folded=tuple(dataclasses.replace(p, claims=back(p.claims)) for p in r.folded),
    )


def inserted(rng: random.Random, claims: list, extra: list) -> tuple[list, list[int | None]]:
    """`extra` mixed into `claims` at random places, with each one's index in `claims` (None
    for the extra ones)."""
    tagged = [(c, i) for i, c in enumerate(claims)] + [(c, None) for c in extra]
    order = sorted(range(len(tagged)), key=lambda _: rng.random())
    return [tagged[k][0] for k in order], [tagged[k][1] for k in order]


@pytest.mark.parametrize("species", [True, False])
def test_property_1_a_counting_claim_never_removes_anything(species):
    rng = random.Random(1)
    for _ in range(TRIALS):
        edges, others, claims, pool, require = random_case(rng)
        before = refined(edges, claims, others, species=species, require_review=require)
        more, _ = inserted(rng, claims, [counting_claim(rng, pool)])
        after = refined(edges, more, others, species=species, require_review=require)
        for old, new in zip(before.edges, after.edges, strict=True):
            assert set(old.pmids) <= set(new.pmids)
            assert new.supported or not old.supported
        for kind in ("proposed", "links"):
            grown = {p.key: set(p.pmids) for p in getattr(after, kind)}
            for p in getattr(before, kind):
                assert set(p.pmids) <= grown[p.key]


@pytest.mark.parametrize("species", [True, False])
def test_property_2_the_order_of_the_claims_does_not_change_the_output(species):
    rng = random.Random(2)
    for _ in range(TRIALS):
        edges, others, claims, _, require = random_case(rng)
        shuffled, origin = inserted(rng, claims, [])
        before = refined(edges, claims, others, species=species, require_review=require)
        after = refined(edges, shuffled, others, species=species, require_review=require)
        assert mapped(after, origin) == mapped(before, list(range(len(claims))))


def ignored_claims(rng: random.Random, edges: list[Edge], pool: list[Statement]) -> list:
    """Claims rule 6 and rules 9 and 10 ignore, reviewed or not: negative claims, statements
    that support no in-scope edge (contraries, broader, sibling and other-branch statements),
    unquoted, rejected, retracted and hypothesis claims."""
    targets = [refine.statement_of(e) for e in edges]
    contraries = [
        expression("decreased"),
        expression("downregulated"),
        drug("contraindicated_in"),
        drug("exacerbates_condition"),
    ]
    review = rng.choice(["upheld", None])
    out = [
        random_claim(rng, pool, polarity="negative", also=(), review=review),
        random_claim(rng, pool, valid=False, review=review),
        random_claim(rng, pool, review="reject"),
        random_claim(rng, pool, retracted=True, review=review),
        random_claim(rng, pool, role="hypothesis", review=review),
    ]
    for _ in range(rng.randint(1, 6)):
        c = random_claim(
            rng, pool, statement=rng.choice(pool + contraries), review=review, valid=True
        )
        if not any(
            refine.narrower(LATTICE, s, t)
            for s in refine.statements_of(c, CATEGORIES)
            for t in targets
        ):
            out.append(c)
    return rng.sample(out, rng.randint(1, len(out)))


@pytest.mark.parametrize("species", [True, False])
def test_property_3_ignored_claims_change_nothing(species):
    rng = random.Random(3)
    for _ in range(TRIALS):
        edges, others, claims, pool, require = random_case(rng)
        before = refined(edges, claims, others, species=species, require_review=require)
        more, origin = inserted(rng, claims, ignored_claims(rng, edges, pool))
        after = refined(edges, more, others, species=species, require_review=require)
        assert mapped(after, origin) == mapped(before, list(range(len(claims))))


@pytest.mark.parametrize("species", [True, False])
def test_property_4_no_proposal_duplicates_an_edge_of_the_pair(species):
    rng = random.Random(4)
    for _ in range(TRIALS):
        edges, others, claims, _, require = random_case(rng)
        r = refined(edges, claims, others, species=species, require_review=require)
        made: dict[str, list[str]] = {}
        for e in (*edges, *others):
            made.setdefault(refine.statement_key(refine.statement_of(e)), []).append(e.id)
        assert not any(p.key in made for p in r.proposed)
        for link in r.links:
            assert list(link.existing) == made[link.key]


def kept(statement: Statement, originals: list[Statement]) -> bool:
    """Whether rule 7 leaves the statement's species in with the flag off: an original it
    refines has that same species."""
    mine = statement.qualifiers.get(SPECIES)
    return mine is not None and any(o.qualifiers.get(SPECIES) == mine for o in originals)


def as_proposed(statement: Statement, originals: list[Statement], species: bool) -> Statement:
    """The statement as a proposal that refines `originals` writes it: without its species when
    the flag is off, unless `kept`."""
    return statement if species or kept(statement, originals) else bare(statement)


def proposes(statement: Statement, targets: list[Statement], species: bool) -> str | None:
    """The key of the proposal the statement makes, if it makes one: it is strictly narrower
    than some of `targets`, the in-scope edges, and written `as_proposed`."""
    originals = [t for t in targets if refine.strictly_narrower(LATTICE, statement, t)]
    if not originals:
        return None
    return refine.statement_key(as_proposed(statement, originals, species))


@pytest.mark.parametrize("species", [True, False])
def test_property_5_every_proposal_is_strictly_narrower_canonical_and_valid(species):
    """Strictly narrower than each original as written, its species included. With the flag
    off, a proposal has a species only when an original it refines has that same one."""
    rng = random.Random(5)
    seen = with_species = 0
    for _ in range(TRIALS):
        edges, others, claims, _, require = random_case(rng)
        r = refined(edges, claims, others, species=species, require_review=require)
        by_id = {e.id: refine.statement_of(e) for e in edges}
        for p in (*r.proposed, *r.links):
            seen += 1
            assert p.original_edge_ids
            originals = [by_id[x] for x in p.original_edge_ids]
            for original in originals:
                assert refine.strictly_narrower(LATTICE, p.statement, original)
            if SPECIES in p.statement.qualifiers:
                with_species += 1
                assert species or kept(p.statement, originals)
            assert refine.directed(p.statement, CATEGORIES) == p.statement
            assert refine.valid(p.statement, CATEGORIES)
            assert p.key == refine.statement_key(p.statement)
    assert seen > TRIALS  # the cases do propose
    assert with_species > TRIALS / 30  # some keep a species, with the flag off too


@pytest.mark.parametrize("species", [True, False])
def test_property_6_pmids_are_exactly_those_of_the_counting_claims(species):
    """A proposal's claims are those with a statement that proposes it: strictly narrower than
    some original, and the same once rule 7 has taken the species out of it, if the flag is
    off."""
    rng = random.Random(6)
    for _ in range(TRIALS):
        edges, others, claims, _, require = random_case(rng)
        r = refined(edges, claims, others, species=species, require_review=require)
        counting = [i for i, c in enumerate(claims) if counts(c, require)]
        stated = {i: refine.statements_of(claims[i], CATEGORIES) for i in counting}
        targets = [refine.statement_of(e) for e in edges]
        for p in (*r.proposed, *r.links):
            makers = [
                i
                for i in counting
                if any(proposes(s, targets, species) == p.key for s in stated[i])
            ]
            assert p.claims == tuple(makers)
            assert set(p.pmids) == {claims[i].pmid for i in makers}
            assert p.score == len(p.pmids) == len(set(p.pmids))
        for o in r.edges:
            supporters = [
                i
                for i in counting
                if any(refine.narrower(LATTICE, s, o.statement) for s in stated[i])
            ]
            assert o.claims == tuple(supporters)
            assert set(o.pmids) == {claims[i].pmid for i in supporters}
            assert o.score == len(o.pmids)


def test_property_7_without_species_proposals_merge_and_support_stays():
    """Rule 7 with the flag off. A proposal or link keeps its species only when an original it
    refines has that same species. Each is the merge of those refine made that say its
    statement once rule 7 has taken their species out, read the same way round: its claims and
    PMIDs exactly the union of theirs, its originals theirs in the order given but any it has
    become (none left: dropped). It is canonical and valid, strictly narrower than each
    original as written, and links exactly to the pair's edges that make it. Each edge's
    refined_by follows, and its support and PMIDs are what they are with the species."""
    rng = random.Random(7)
    seen = 0
    for _ in range(TRIALS):
        edges, others, claims, _, require = random_case(rng)
        r = refined(edges, claims, others, require_review=require)
        f = refine.without_species(r, others)
        made: dict[str, list[str]] = {}
        for e in (*edges, *others):
            ids = made.setdefault(refine.statement_key(refine.statement_of(e)), [])
            ids += [] if e.id in ids else [e.id]
        by_id = {e.id: refine.statement_of(e) for e in edges}
        found = [
            (p, as_proposed(p.statement, [by_id[x] for x in p.original_edge_ids], False))
            for p in (*r.proposed, *r.links)
        ]
        assert f.folded == tuple(p for p, s in found if s != p.statement)
        seen += len(f.folded)
        groups: dict[str, list[tuple[refine.Proposal, Statement]]] = {}
        for p, s in found:
            groups.setdefault(refine.statement_key(s), []).append((p, s))
        out = {p.key: p for p in (*f.proposed, *f.links)}
        for key, members in groups.items():
            group = [p for p, _ in members]
            refines = {
                x
                for p in group
                for x in p.original_edge_ids
                if refine.statement_key(by_id[x]) != key
            }
            if not refines:
                assert key not in out
                continue
            q = out.pop(key)
            assert all(s == q.statement for _, s in members)  # the same reading
            assert q.key == refine.statement_key(q.statement)
            assert q.claims == tuple(sorted({i for p in group for i in p.claims}))
            assert q.context_notes == tuple(sorted({n for p in group for n in p.context_notes}))
            assert set(q.pmids) == {claims[i].pmid for i in q.claims}
            assert q.score == len(q.pmids) == len(set(q.pmids))
            assert q.original_edge_ids == tuple(x for x in by_id if x in refines)
            originals = [by_id[x] for x in q.original_edge_ids]
            for original in originals:
                assert refine.strictly_narrower(LATTICE, q.statement, original)
            assert SPECIES not in q.statement.qualifiers or kept(q.statement, originals)
            assert refine.directed(q.statement, CATEGORIES) == q.statement
            assert refine.valid(q.statement, CATEGORIES)
            assert list(q.existing) == made.get(key, [])
        assert out == {}
        assert not any(p.existing for p in f.proposed) and all(p.existing for p in f.links)
        for old, new in zip(r.edges, f.edges, strict=True):
            assert (new.claims, new.pmids, new.added, new.context_notes) == (
                old.claims,
                old.pmids,
                old.added,
                old.context_notes,
            )
            assert set(new.refined_by) == {
                x
                for p in (*f.proposed, *f.links)
                if new.edge_id in p.original_edge_ids
                for x in (p.existing or (p.id,))
            }
    assert seen > TRIALS / 10  # the cases do make proposals with a species
