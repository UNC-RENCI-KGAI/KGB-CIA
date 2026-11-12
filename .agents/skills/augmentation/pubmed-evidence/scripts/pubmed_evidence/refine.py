"""Edge refinement: which of a pair's edges its claims support, with which PMIDs, and the more
specific edges they propose.

Implements step 6's rules for one pair of nodes, cited by the numbers DESIGN.md's step 6 gives
them ("rule 2"). Pure: no I/O and no model calls. Biolink's hierarchies come from `biolink`
(offline, from the bundled model) and UBERON's from the closure the caller passes in.

- **Narrower.** S1 ⊑ S2 (S1 is narrower than or equal to S2) when they join the same subject
  and object, S1's predicate is S2's or below it (mixins included), and every qualifier S2
  sets, S1 sets to the same value or one below it: by the enum's `is_a` (upregulated below
  increased, expression below abundance), by predicate for `qualified_predicate`, by UBERON
  is_a/part_of for anatomy, and by equality for any other slot, species among them. S1 may set
  more. A negated statement is narrower than nothing, and nothing is narrower than one. A
  symmetric statement compares either way round (`narrower`).
- **A claim's statements** (`statements_of`): a positive claim's statement and its
  `also_supports` entries, and a negative claim's `also_supports` entries alone. Each gains the
  claim's grounded species and anatomy where its association allows the slot.
- **Counting.** A claim counts when its quote passed the check, its paper isn't retracted, it
  isn't a hypothesis, and review upheld it. Nothing weighs a claim.
- **Refinement** (`refine_pair`). Each in-scope edge is supported by the counting claims with a
  statement narrower than or equal to its own, and gains their PMIDs. Each of those statements
  strictly narrower than the edge is proposed, once per pair however many claims make it or
  edges it refines. A proposal an edge of the pair already makes is a link to that edge, not a
  new edge. A pair is held while a claim that would count has no review.
- **Support only** (rule 11). A statement narrower than an edge only by a predicate
  config.SUPPORT_ONLY lists under the edge's ("ameliorates" under "treats") supports the edge
  and is never proposed under it. Unless `review_support_only` (config.REVIEW_SUPPORT_ONLY), it
  needs no review: it counts without a verdict, unless the claim was rejected, and alone it
  neither sends a claim to review nor holds a pair.
- **Species in proposals** (rule 7). `without_species` takes the species out of a pair's
  proposals, as step 6 does unless config.SPECIES_IN_PROPOSALS keeps it: those that become the
  same statement merge, one that becomes an edge it refines no longer refines that edge, and
  one that becomes an edge of the pair is a link to it. A proposal keeps the species an
  original it refines has, so that it stays narrower than that original.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import cache

from pubmed_evidence import biolink, config, graphio
from shared.schema import ClaimRecord, Edge, PublicationEvidence, Statement

QUALIFIED_PREDICATE = "qualified_predicate"
# The qualifier slots a claim's grounded context fills.
SPECIES = "species_context_qualifier"
ANATOMY = "anatomical_context_qualifier"


@cache
def _enums() -> dict[str, str]:
    return biolink.qualifier_enums()


def _slots(statement: Statement) -> dict[str, str]:
    """The statement's qualifiers, `qualified_predicate` among them."""
    slots = dict(statement.qualifiers)
    if statement.qualified_predicate is not None:
        slots[QUALIFIED_PREDICATE] = statement.qualified_predicate
    return slots


@dataclass(frozen=True, eq=False)
class Lattice:
    """The order on a pair's statements. `anatomy` is UBERON's is_a/part_of closure, each term
    to its ancestors; without it, two anatomy terms are related only when equal."""

    anatomy: Mapping[str, Collection[str]] = field(default_factory=dict)

    def at_or_below(self, slot: str, value: str, top: str) -> bool:
        """Whether `value` is `top` or below it, in the hierarchy of `slot`: 'predicate',
        'qualified_predicate' or a qualifier slot. A slot without one (species, frequency)
        compares by equality."""
        if value == top:
            return True
        if slot in ("predicate", QUALIFIED_PREDICATE):
            return biolink.is_a(value, top)
        enum = _enums().get(slot)
        if enum is None:
            return top in self.anatomy.get(value, ())
        seen = set()
        while value is not None and value not in seen:
            if value == top:
                return True
            seen.add(value)
            value = biolink.enum_parent(enum, value)
        return False

    def entails(self, a: Statement, b: Statement) -> bool:
        """a ⊑ b: a is narrower than or equal to b, as written. Every slot b sets, context and
        open slots included, a sets to the same value or one below it. A negated statement
        entails nothing, and nothing entails one."""
        if a.negated or b.negated:
            return False
        if a.subject != b.subject or a.object != b.object:
            return False
        if not self.at_or_below("predicate", a.predicate, b.predicate):
            return False
        mine = _slots(a)
        return all(
            slot in mine and self.at_or_below(slot, mine[slot], value)
            for slot, value in _slots(b).items()
        )


@dataclass(frozen=True)
class Claim:
    """A claim as refinement reads it: the record, with the PMID ('PMID:n') of the abstract
    it's from and whether PubMed marks that paper retracted."""

    pmid: str
    record: ClaimRecord
    retracted: bool


def claims_of(evidence: Iterable[PublicationEvidence]) -> list[Claim]:
    """Every claim of a pair's abstracts, in order."""
    return [Claim(e.publication, c, e.retracted) for e in evidence for c in e.claims]


# --- Reading statements ---


def statement_of(edge: Edge) -> Statement:
    """What the edge says: its subject, predicate, object, qualifiers and `negated`. Its id is
    `graphio.edge_statement_id(edge)`."""
    qualifiers = {k: str(v) for k, v in graphio.qualifiers_of(edge).items()}
    qp = qualifiers.pop(QUALIFIED_PREDICATE, None)
    if qp is not None and not qp.startswith("biolink:"):
        qp = "biolink:" + qp
    return Statement(
        subject=edge.subject,
        predicate=edge.predicate,
        object=edge.object,
        qualified_predicate=qp,
        qualifiers=qualifiers,
        negated=graphio.is_negated(edge),
    )


def _turned(statement: Statement, predicate: str | None = None) -> Statement:
    """The statement read from its object: subject and object swapped, each side's qualifiers
    with them. With `predicate`, the inverse it's read with, and `qualified_predicate` turns to
    its inverse too ('causes' to 'caused by')."""

    def side(slot: str) -> str:
        for a, b in (("subject_", "object_"), ("object_", "subject_")):
            if slot.startswith(a):
                return b + slot.removeprefix(a)
        return slot

    update = {
        "subject": statement.object,
        "object": statement.subject,
        "qualifiers": {side(k): v for k, v in statement.qualifiers.items()},
    }
    if predicate is not None:
        update["predicate"] = predicate
        qp = statement.qualified_predicate
        if qp is not None:
            update["qualified_predicate"] = biolink.inverse(qp) or qp
    return statement.model_copy(update=update)


def readings(statement: Statement) -> tuple[Statement, ...]:
    """The ways the statement can be written: itself, and for a symmetric predicate between two
    nodes, the same statement read from its object ('Y interacts with X' says 'X interacts with
    Y')."""
    if statement.subject != statement.object and biolink.is_symmetric(statement.predicate):
        return statement, _turned(statement)
    return (statement,)


def _in_id_order(statement: Statement) -> list[Statement]:
    return sorted(readings(statement), key=lambda s: (s.subject, s.object))


def statement_key(statement: Statement) -> str:
    """The id two statements share when they say the same: for a symmetric predicate, the id of
    the reading with the nodes in id order, so 'MDM2 interacts with TP53' and 'TP53 interacts
    with MDM2' share one; for any other, its own id. Proposals merge and link on it, and a
    proposed edge's id hashes it."""
    return _in_id_order(statement)[0].id


def directed(statement: Statement, categories: Mapping[str, Sequence[str]]) -> Statement | None:
    """The statement the way round Biolink reads it between its nodes, whose categories are in
    `categories`: its predicate fits the subject and the object by its domain and range, with a
    predicate defined the other way round replaced by its inverse, and a non-canonical one
    turned round to its canonical inverse (`biolink.oriented`). The same statement if it
    already reads so; None if its predicate fits neither way. A node without categories
    constrains nothing."""
    read = biolink.oriented(
        tuple(categories.get(statement.subject) or ()),
        statement.predicate,
        tuple(categories.get(statement.object) or ()),
    )
    if read is None:
        return None
    predicate, turned = read
    if turned:
        return _turned(statement, predicate)
    if predicate != statement.predicate:
        return statement.model_copy(update={"predicate": predicate})
    return statement


def narrower(lattice: Lattice, a: Statement, b: Statement) -> bool:
    """Whether a is narrower than or equal to b: some reading of a entails some reading of b,
    so a symmetric statement compares either way round."""
    return any(lattice.entails(x, y) for x in readings(a) for y in readings(b))


def strictly_narrower(lattice: Lattice, a: Statement, b: Statement) -> bool:
    """Whether a is narrower than b and not the same statement."""
    return statement_key(a) != statement_key(b) and narrower(lattice, a, b)


# --- Biolink's checks ---


def _nodes(
    statement: Statement, categories: Mapping[str, Sequence[str]] | None
) -> tuple[list[str], list[str]] | None:
    """The categories of the statement's subject and object; None if either has none."""
    if categories is None:
        return None
    subject, object = categories.get(statement.subject), categories.get(statement.object)
    if not subject or not object:
        return None
    return list(subject), list(object)


def _allows(
    statement: Statement, slot: str, categories: Mapping[str, Sequence[str]] | None
) -> bool:
    """Whether the statement's association allows the qualifier slot, read either way round if
    it's symmetric. A node without categories allows every slot."""
    for reading in readings(statement):
        nodes = _nodes(reading, categories)
        if nodes is None or slot in biolink.allowed_qualifiers(
            nodes[0], reading.predicate, nodes[1]
        ):
            return True
    return False


def valid(statement: Statement, categories: Mapping[str, Sequence[str]] | None) -> bool:
    """Whether the statement, as written, can be an edge between its nodes: its predicate fits
    them by its domain and range (`biolink.fits`), and every slot it sets, `qualified_predicate`
    included, is one its association allows, with an allowed value where the slot is an enum.
    True if either node has no categories."""
    nodes = _nodes(statement, categories)
    if nodes is None:
        return True
    subject, object = nodes
    if not biolink.fits(subject, statement.predicate, object):
        return False
    allowed = biolink.allowed_qualifiers(subject, statement.predicate, object)
    return all(
        (spec := allowed.get(slot)) is not None and (spec.values is None or value in spec.values)
        for slot, value in _slots(statement).items()
    )


def _written(
    statement: Statement, categories: Mapping[str, Sequence[str]] | None
) -> Statement | None:
    """How a proposal of the statement is written: the first of its readings, in the order of
    their nodes' ids, that is `valid`; None if none is. So a symmetric statement reads the same
    whichever way round its claims put it, in id order unless only the other reading is valid
    (Biolink gives a chemical and a gene their qualifiers only with the chemical as subject)."""
    return next((s for s in _in_id_order(statement) if valid(s, categories)), None)


# --- Claims ---


def statements_of(
    claim: Claim, categories: Mapping[str, Sequence[str]] | None = None
) -> tuple[Statement, ...]:
    """What the claim states, as rule 2 compares it: a positive claim's statement and its
    `also_supports` entries; a negative claim's `also_supports` entries alone, since what it
    denies supports nothing (rule 8). Each gains the claim's grounded species and anatomy
    as `species_context_qualifier` and `anatomical_context_qualifier` where its association
    allows the slot (`categories`; a node without them allows every slot) and it doesn't set
    the slot itself. An anatomy name that didn't ground adds nothing."""
    record = claim.record
    own = (record.statement,) if record.polarity == "positive" else ()
    context = {
        slot: value
        for slot, value in ((SPECIES, record.context.species), (ANATOMY, record.context.anatomy))
        if value is not None
    }
    out = []
    for statement in (*own, *record.also_supports):
        added = {
            slot: value
            for slot, value in context.items()
            if slot not in statement.qualifiers and _allows(statement, slot, categories)
        }
        if added:
            qualifiers = {**statement.qualifiers, **added}
            statement = statement.model_copy(update={"qualifiers": qualifiers})
        out.append(statement)
    return tuple(out)


def _could_count(claim: Claim) -> bool:
    """Whether the claim counts once upheld: its quote passed the check, its paper isn't
    retracted (rule 9), and it isn't a hypothesis (rule 10)."""
    record = claim.record
    return (
        record.evidence_sentence_valid and not claim.retracted and record.claim_role != "hypothesis"
    )


def eligible(claim: Claim) -> bool:
    """Whether the claim counts once review upholds it or if review isn't required: it could
    count (`_could_count`) and review didn't reject it. Its polarity decides its statements
    (`statements_of`), not this."""
    return _could_count(claim) and claim.record.review != "reject"


def _counts(claim: Claim, require_review: bool) -> bool:
    """Whether the claim counts: eligible and upheld, or, without `require_review`, eligible
    and not yet reviewed."""
    review = claim.record.review
    return eligible(claim) and (review == "upheld" or (review is None and not require_review))


def support_only(statement: Statement, target: Statement) -> bool:
    """Whether the statement, if narrower than the target, only supports it (rule 11): its
    predicate is one config.SUPPORT_ONLY lists under the target's, or below one."""
    return any(
        biolink.is_a(statement.predicate, p) for p in config.SUPPORT_ONLY.get(target.predicate, ())
    )


def _review_support_only(flag: bool | None) -> bool:
    return config.REVIEW_SUPPORT_ONLY if flag is None else flag


def _would_count(
    lattice: Lattice,
    stated: Sequence[tuple[Statement, ...]],
    targets: Sequence[Statement],
    review_support_only: bool,
) -> list[int]:
    return [
        i
        for i, statements in enumerate(stated)
        if any(
            narrower(lattice, s, t) and (review_support_only or not support_only(s, t))
            for s in statements
            for t in targets
        )
    ]


def would_count(
    edges: Sequence[Edge],
    claims: Sequence[Claim],
    lattice: Lattice,
    categories: Mapping[str, Sequence[str]] | None = None,
    *,
    review_support_only: bool | None = None,
) -> tuple[int, ...]:
    """The indices of the claims that would count if upheld (DESIGN.md's step 5), whatever their
    review: those that could count (`_could_count`) with a statement narrower than or equal to
    some edge's in `edges`, the pair's in-scope edges. A statement that only supports the edge
    (`support_only`) needs no review, so it doesn't make a claim one of these, unless
    `review_support_only` (default config.REVIEW_SUPPORT_ONLY). Step 5 reviews those without a
    verdict, and step 6 holds the pair while one has none."""
    stated = [statements_of(c, categories) if _could_count(c) else () for c in claims]
    targets = [statement_of(e) for e in edges]
    return tuple(_would_count(lattice, stated, targets, _review_support_only(review_support_only)))


# --- Refinement ---


def _pmid_order(pmid: str) -> tuple[bool, int, str]:
    return graphio.pmid_key(graphio.pmid_of(pmid) or pmid)


def _pmids(claims: Iterable[Claim]) -> tuple[str, ...]:
    return tuple(sorted({c.pmid for c in claims}, key=_pmid_order))


def _notes(claims: Iterable[Claim]) -> tuple[str, ...]:
    """The context Biolink can't express, from the claims: their free-text notes, and anatomy
    names that didn't ground."""
    notes = set()
    for claim in claims:
        context = claim.record.context
        if context.notes:
            notes.add(context.notes)
        if context.anatomy is None and context.anatomy_name:
            notes.add(context.anatomy_name)
    return tuple(sorted(notes))


@dataclass(frozen=True)
class EdgeOutcome:
    """What the pair's claims do for one in-scope edge (rules 2 and 3). `claims` are the
    counting claims with a statement narrower than or equal to its own (indices into the claims
    given), `pmids` their distinct PMIDs ('PMID:n', in PubMed id order), and `added` those its
    `publications` don't cite. `refined_by` names the edges more specific than it that those
    claims make: its proposals' ids, and for its links the ids of the pair's edges that already
    make them. `context_notes` are its claims' free-text notes and anatomy names that didn't
    ground."""

    edge_id: str
    statement: Statement
    claims: tuple[int, ...]
    pmids: tuple[str, ...]
    added: tuple[str, ...]
    refined_by: tuple[str, ...]
    context_notes: tuple[str, ...]

    @property
    def score(self) -> int:
        """The support score: the number of distinct supporting PMIDs."""
        return len(self.pmids)

    @property
    def supported(self) -> bool:
        return bool(self.pmids)


@dataclass(frozen=True)
class Proposal:
    """A statement strictly narrower than some in-scope edge that the pair's counting claims
    make (rule 4), one per pair. `claims` are every counting claim with a statement of its
    key (indices into the claims given), `pmids` their distinct PMIDs, and `original_edge_ids`
    the in-scope edges it refines, in the order given. `existing` names the pair's edges that
    already make it, in scope or not: then it's a link to them (rule 5), not a new edge."""

    key: str
    statement: Statement
    claims: tuple[int, ...]
    pmids: tuple[str, ...]
    original_edge_ids: tuple[str, ...]
    existing: tuple[str, ...] = ()
    context_notes: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        """The id of the edge it proposes."""
        return graphio.proposed_edge_id(self.key)

    @property
    def score(self) -> int:
        return len(self.pmids)


@dataclass(frozen=True)
class PairRefinement:
    """A pair's refinement: an outcome per in-scope edge, in the order given; the proposed
    edges, by id; the links, by key; and the claims that would count but have no review
    (indices into the claims given). The pair is `held` while there are any and review is
    required. `folded` are the proposals and links `without_species` took a species out of, as
    `refine_pair` made them, the proposed edges first."""

    edges: tuple[EdgeOutcome, ...]
    proposed: tuple[Proposal, ...]
    links: tuple[Proposal, ...]
    unreviewed: tuple[int, ...]
    held: bool
    folded: tuple[Proposal, ...] = ()


@dataclass
class _Found:
    """A statement some counting claims make that is strictly narrower than some in-scope
    edges, as `refine_pair` collects it."""

    statement: Statement
    claims: set[int] = field(default_factory=set)
    edges: set[int] = field(default_factory=set)  # indices into the in-scope edges


def refine_pair(
    edges: Sequence[Edge],
    claims: Sequence[Claim],
    lattice: Lattice | None = None,
    categories: Mapping[str, Sequence[str]] | None = None,
    others: Sequence[Edge] = (),
    *,
    require_review: bool = True,
    review_support_only: bool | None = None,
) -> PairRefinement:
    """One pair's refinement from its claims (rules 1 to 6, and 11).

    `edges` are the pair's in-scope edges and `others` the ones select left out; each needs its
    `id`. `categories` maps node ids to Biolink categories, for the claims' context and the
    proposals' check (`valid`); without it nothing is checked. With `require_review`, only
    upheld claims count, and the pair is `held` while a claim that would count has no review;
    without it, which is for testing only, unreviewed claims count too.

    Each edge is supported by the counting claims with a statement narrower than or equal to
    its own, and each of those statements strictly narrower than the edge is proposed, but one
    that only supports it (`support_only`). Unless `review_support_only` (default
    config.REVIEW_SUPPORT_ONLY), such a statement counts without review too: a claim not
    rejected supports the edge through it whatever its verdict. A statement is one proposal
    however many claims make it or edges it refines. A proposal that isn't `valid` either way
    round is dropped, and its claims still support the edge. One whose
    statement an edge of the pair already makes is a link to that edge. A species narrows like
    any other qualifier; `without_species` takes it out (rule 7). The output depends on the
    claims' order only through the index tuples.
    """
    for edge in (*edges, *others):
        if edge.id is None:
            raise ValueError("an edge needs its id to be refined; give it graphio.edge_id first")
    lattice = lattice if lattice is not None else Lattice()
    review_support_only = _review_support_only(review_support_only)
    stated = [statements_of(c, categories) if _could_count(c) else () for c in claims]
    targets = [statement_of(e) for e in edges]
    unreviewed = tuple(
        i
        for i in _would_count(lattice, stated, targets, review_support_only)
        if claims[i].record.review is None
    )
    live = [i for i, c in enumerate(claims) if stated[i] and eligible(c)]

    keys = [statement_key(t) for t in targets]
    made: dict[str, dict[str, None]] = {}  # statement key -> ids of the pair's edges making it
    for edge, key in zip(edges, keys, strict=True):
        made.setdefault(key, {})[edge.id] = None
    for edge in others:
        made.setdefault(statement_key(statement_of(edge)), {})[edge.id] = None

    support: list[list[int]] = []
    found: dict[str, _Found] = {}
    for n, (target, key) in enumerate(zip(targets, keys, strict=True)):
        mine = []
        for i in live:
            below = [s for s in stated[i] if narrower(lattice, s, target)]
            if not below:
                continue
            proposing = [s for s in below if not support_only(s, target)]
            if not _counts(claims[i], require_review):
                # Not upheld: it counts only through statements that need no review.
                if review_support_only or len(proposing) == len(below):
                    continue
                proposing = []
            mine.append(i)
            for s in proposing:
                k = statement_key(s)
                if k != key:
                    f = found.setdefault(k, _Found(s))
                    f.claims.add(i)
                    f.edges.add(n)
        support.append(mine)

    proposals: dict[str, Proposal] = {}
    for key, f in found.items():
        statement = _written(f.statement, categories)
        if statement is None:
            continue
        chosen = sorted(f.claims)
        proposals[key] = Proposal(
            key=key,
            statement=statement,
            claims=tuple(chosen),
            pmids=_pmids(claims[i] for i in chosen),
            original_edge_ids=tuple(dict.fromkeys(edges[n].id for n in sorted(f.edges))),
            existing=tuple(made.get(key, ())),
            context_notes=_notes(claims[i] for i in chosen),
        )

    outcomes = []
    for n, (edge, target) in enumerate(zip(edges, targets, strict=True)):
        mine = support[n]
        pmids = _pmids(claims[i] for i in mine)
        cited = {p for p in map(graphio.pmid_of, edge.publications) if p}
        refined_by = {
            x
            for key, p in proposals.items()
            if n in found[key].edges
            for x in (p.existing or (p.id,))
        }
        outcomes.append(
            EdgeOutcome(
                edge_id=edge.id,
                statement=target,
                claims=tuple(mine),
                pmids=pmids,
                added=tuple(p for p in pmids if graphio.pmid_of(p) not in cited),
                refined_by=tuple(sorted(refined_by)),
                context_notes=_notes(claims[i] for i in mine),
            )
        )
    new = [p for p in proposals.values() if not p.existing]
    linked = [p for p in proposals.values() if p.existing]
    return PairRefinement(
        edges=tuple(outcomes),
        proposed=tuple(sorted(new, key=lambda p: p.id)),
        links=tuple(sorted(linked, key=lambda p: p.key)),
        unreviewed=unreviewed,
        held=require_review and bool(unreviewed),
    )


def _no_species(statement: Statement) -> Statement:
    qualifiers = {k: v for k, v in statement.qualifiers.items() if k != SPECIES}
    return statement.model_copy(update={"qualifiers": qualifiers})


def without_species(refinement: PairRefinement, others: Sequence[Edge] = ()) -> PairRefinement:
    """The refinement with `species_context_qualifier` taken out of its proposals and links
    (rule 7, with the flag off), except where an original it refines has that same
    species: such a proposal keeps it, so that it stays narrower than that original, and still
    refines all its originals, those without a species too. Those that become the same
    statement are one, with the union of their claims, PMIDs, notes and `original_edge_ids`. An
    original the statement has become ("E in human" becomes E) is no longer one it refines, and
    a proposal left refining none is dropped: it is that original, which its claims support
    already. One an edge of the pair makes, in scope or among `others` (the edges select left
    out, as given to `refine_pair`), is a link to that edge (rule 5). Each edge's `refined_by`
    follows; its support and PMIDs stay as they are. Each statement reads the way round
    `refine_pair` wrote it: Biolink allows a species in both readings of a symmetric statement
    or in neither, so taking it out doesn't change which reading is valid. Returns `refinement`
    itself when no proposal or link loses a species."""
    species = {o.edge_id: o.statement.qualifiers.get(SPECIES) for o in refinement.edges}

    def stated(p: Proposal) -> Statement:
        mine = p.statement.qualifiers.get(SPECIES)
        if mine is None or any(species[x] == mine for x in p.original_edge_ids):
            return p.statement
        return _no_species(p.statement)

    found = [(p, stated(p)) for p in (*refinement.proposed, *refinement.links)]
    folded = tuple(p for p, s in found if s != p.statement)
    if not folded:
        return refinement
    keys = {o.edge_id: statement_key(o.statement) for o in refinement.edges}
    made: dict[str, dict[str, None]] = {}  # statement key -> ids of the pair's edges making it
    for eid, key in keys.items():
        made.setdefault(key, {})[eid] = None
    for edge in others:
        made.setdefault(statement_key(statement_of(edge)), {})[edge.id] = None

    groups: dict[str, list[tuple[Proposal, Statement]]] = {}
    for p, s in found:
        groups.setdefault(statement_key(s), []).append((p, s))
    proposals = []
    for key, members in groups.items():
        group = [p for p, _ in members]
        refines = {x for p in group for x in p.original_edge_ids if keys[x] != key}
        if not refines:
            continue
        proposals.append(
            Proposal(
                key=key,
                statement=members[0][1],
                claims=tuple(sorted({i for p in group for i in p.claims})),
                pmids=tuple(sorted({x for p in group for x in p.pmids}, key=_pmid_order)),
                original_edge_ids=tuple(x for x in keys if x in refines),
                existing=tuple(made.get(key, ())),
                context_notes=tuple(sorted({n for p in group for n in p.context_notes})),
            )
        )
    refined_by: dict[str, set[str]] = {}  # in-scope edge id -> the edges more specific than it
    for p in proposals:
        for eid in p.original_edge_ids:
            refined_by.setdefault(eid, set()).update(p.existing or (p.id,))
    edges = tuple(
        replace(o, refined_by=tuple(sorted(refined_by.get(o.edge_id, ()))))
        for o in refinement.edges
    )
    return replace(
        refinement,
        edges=edges,
        proposed=tuple(sorted((p for p in proposals if not p.existing), key=lambda p: p.id)),
        links=tuple(sorted((p for p in proposals if p.existing), key=lambda p: p.key)),
        folded=folded,
    )
