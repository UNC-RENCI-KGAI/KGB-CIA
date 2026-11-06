"""Biolink lookups through the Biolink Model Toolkit (`bmt`), offline.

`bmt.Toolkit()` downloads the model and predicate map from GitHub by default. This module loads
the Biolink 4.4.6 YAML bundled in the `biolink-model` package instead (the version
`shared/schema.py` uses) with an empty predicate map, so it needs no network and tests can use
it. The Toolkit is built once and shared, and every lookup is cached: steps call them per edge.

Used by step 1 (category and predicate ancestry), `sentences.py` (predicate names), step 3
(the predicates a reply schema offers, with their `opposite_of` partners; which qualifiers a
pair's association class allows; and checking the LLM's claims) and `refine.py` (the predicate
and qualifier hierarchies, and whether a statement fits its domain and range). `oriented` reads
a statement the way round a predicate's domain, range, inverse and canonical direction put it:
the domain and range enforce the subject and object order, a predicate that fits only the other
way round becomes its inverse, and one that fits neither way can't be read at all. The label
step's predicate lists and checks, the claims refinement compares, and select's count of edges
written otherwise all read statements this way.

`bmt.get_associations` is not used to find an edge's association class, because it matches far
too loosely for these edges. It accepts any class that leaves the predicate unconstrained, so
classes whose ranges are broad mixins fit everything: `FunctionalAssociation` and
`GeneToGoTermAssociation` take any object that is an `OntologyClass` (Disease is one), and
`ReactionToCatalystAssociation` any `MolecularEntity` subject. It also reads `slot_usage` only
from the class, its parent and its mixins, and checks only `subproperty_of` on the predicate, so
it misses classes such as `GeneToDiseaseAssociation` whose predicate range is an enum. Here
`associations` reads the induced (fully inherited) subject, predicate and object slots of every
association class, keeps the classes whose ranges hold the edge's categories and whose predicate
constraint holds its predicate, prefers classes that constrain the predicate, and drops any
class another fitting class beats on all three.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from functools import cache, lru_cache
from importlib.resources import files
from typing import Any

from bmt import Toolkit
from bmt.utils import format_element

BIOLINK_VERSION = "4.4.6"

# Qualifier slots every association inherits from `association` itself. They describe the
# statistics or basis of a finding rather than what it claims, so they stay out of step 3.
GENERIC_QUALIFIERS = frozenset(
    {"qualifier", "qualifiers", "statistical significance qualifier", "association basis qualifier"}
)
# Biolink 4.4.6 constrains `qualified_predicate` (with `subproperty_of: causes`) only on the
# chemical-affects classes; elsewhere its range is a bare uriorcurie. Every Biolink qualifier
# example uses biolink:causes, so that is what other classes allow too.
DEFAULT_QUALIFIED_PREDICATE = "causes"

_CURIE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*:[^\s:]\S*$")
_lock = threading.Lock()


@dataclass(frozen=True)
class QualifierSpec:
    """A qualifier slot an association allows, and its permissible values.

    `values` is None when the range is open (an ontology class or free text) rather than an
    enum; `range` names the enum or class either way. For `qualified_predicate`, `values` lists
    the predicate CURIEs the association allows (its `subproperty_of` and their descendants).
    """

    slot: str
    values: tuple[str, ...] | None
    range: str | None = None
    description: str = ""


@dataclass(frozen=True)
class _Association:
    name: str
    curie: str
    depth: int
    subject: tuple[str, ...]
    object: tuple[str, ...]
    # None: any predicate. Otherwise the predicates (with their descendants) the class allows.
    predicate: tuple[str, ...] | None


def toolkit() -> Toolkit:
    """The shared `bmt.Toolkit`, loaded from the bundled Biolink YAML."""
    with _lock:
        return _toolkit()


@cache
def _toolkit() -> Toolkit:
    schema = files("biolink_model") / "schema" / "biolink_model.yaml"
    return Toolkit(str(schema), predicate_map={"predicate mappings": []})


@lru_cache(maxsize=4096)
def _element(name: str):
    # bmt's get_element scans every element on a miss, so cache both hits and misses.
    return toolkit().get_element(name)


@lru_cache(maxsize=4096)
def _name(curie: str) -> str | None:
    """The element name of a CURIE or name ('biolink:SmallMolecule' -> 'small molecule')."""
    element = _element(curie)
    return element.name if element is not None else None


@lru_cache(maxsize=4096)
def _curie(name: str) -> str:
    element = _element(name)
    return format_element(element) if element is not None else name


@lru_cache(maxsize=4096)
def _desc(name: str) -> frozenset[str]:
    """Names of `name` and everything below it, through is_a and mixins."""
    return frozenset(toolkit().get_descendants(name, mixin=True))


@lru_cache(maxsize=4096)
def _depth(name: str | None) -> int:
    return len(toolkit().get_ancestors(name, mixin=True)) if name else 0


def _under(name: str, ancestor: str) -> bool:
    return name in _desc(ancestor)


def descendants(curie: str) -> set[str]:
    """`curie` and every class or predicate below it, as 'biolink:' CURIEs (mixins included)."""
    return set(_descendants(curie))


@lru_cache(maxsize=4096)
def _descendants(curie: str) -> frozenset[str]:
    name = _name(curie)
    if name is None:
        return frozenset({curie})
    return frozenset(_curie(n) for n in _desc(name))


def _as_tuple(x: list[str] | tuple[str, ...] | str) -> tuple[str, ...]:
    return (x,) if isinstance(x, str) else tuple(x)


def is_a(curies: list[str] | str, ancestors: list[str] | str) -> bool:
    """True if any of `curies` is one of `ancestors` or below one of them."""
    return _is_a(_as_tuple(curies), _as_tuple(ancestors))


@lru_cache(maxsize=65536)
def _is_a(curies: tuple[str, ...], ancestors: tuple[str, ...]) -> bool:
    return any(_curie(c) in _descendants(a) for a in ancestors for c in curies)


def is_symmetric(predicate: str) -> bool:
    """True if Biolink marks the predicate symmetric (e.g. 'biolink:interacts_with')."""
    return _is_symmetric(predicate)


@lru_cache(maxsize=1024)
def _is_symmetric(predicate: str) -> bool:
    name = _name(predicate)
    return bool(name and toolkit().is_symmetric(name))


def opposites() -> frozenset[tuple[str, str]]:
    """Pairs of predicates Biolink marks `opposite_of` each other, as CURIEs, in both orders:
    Biolink 4.4.6 declares some in one direction only (`contraindicated in` -> `treats`). Slots
    that aren't predicates (interbase coordinates, `prevented by`) are left out."""
    return _opposites()


@cache
def _opposites() -> frozenset[tuple[str, str]]:
    tk = toolkit()
    pairs: set[tuple[str, str]] = set()
    for name, slot in tk.view.all_slots().items():
        annotation = (slot.annotations or {}).get("opposite_of")
        if annotation is None:
            continue
        other = annotation.value
        if tk.is_predicate(name) and tk.is_predicate(other):
            a, b = _curie(name), _curie(other)
            pairs |= {(a, b), (b, a)}
    return frozenset(pairs)


def inverse(predicate: str) -> str | None:
    """The predicate's Biolink `inverse`, declared on either side of the pair ('biolink:treats'
    -> 'biolink:treated_by'); the predicate itself if it's symmetric; None if it has neither."""
    return _inverse(predicate)


@lru_cache(maxsize=1024)
def _inverse(predicate: str) -> str | None:
    name = _name(predicate)
    if name is None:
        return None
    other = toolkit().get_inverse(name)
    if other:
        return _curie(other)
    return predicate if is_symmetric(predicate) else None


def is_canonical(predicate: str) -> bool:
    """Whether Biolink marks the predicate as the canonical one of an inverse pair
    (`canonical_predicate`): 'treats' is, 'treated by' isn't."""
    name = _name(predicate)
    return bool(name and toolkit().is_translator_canonical_predicate(name))


@lru_cache(maxsize=1024)
def _constraints(predicate: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The classes a predicate's subject and its object must fit (names): its own domain and
    range, and its inverse's range and domain, since Biolink sometimes declares them on one
    side of the pair only. A missing one constrains nothing, and neither does a range that
    isn't a class."""
    view = toolkit().view

    def classes(*names: str | None) -> tuple[str, ...]:
        return tuple(dict.fromkeys(n for n in names if n and view.get_class(n) is not None))

    own = _element(predicate)
    if own is None:
        return (), ()
    other = inverse(predicate)
    twin = _element(other) if other is not None and other != predicate else None
    subject = classes(own.domain, twin.range if twin is not None else None)
    object = classes(own.range, twin.domain if twin is not None else None)
    return subject, object


@lru_cache(maxsize=65536)
def _overlap(a: str, b: str) -> bool:
    """Whether some class is at or below both `a` and `b` (names; mixins included): a node of
    `a` could be a `b`. 'gene or gene product' and 'gene' overlap; 'disease' and 'gene' don't."""
    return not _desc(a).isdisjoint(_desc(b))


@lru_cache(maxsize=4096)
def _leaves(categories: tuple[str, ...]) -> tuple[str, ...]:
    """The most specific of a node's categories (names): KGX lists every ancestor and mixin of
    them too, and `named thing` would overlap anything."""
    tk = toolkit()
    above = {a for c in categories for a in tk.get_ancestors(c, mixin=True, reflexive=False)}
    return tuple(c for c in categories if c not in above) or categories


def most_specific(categories: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """The node's categories below none of its others, as CURIEs ('biolink:Gene' of a list
    with all its ancestors); unknown ones are left out."""
    names = tuple(dict.fromkeys(n for n in map(_name, categories) if n))
    return tuple(_curie(n) for n in _leaves(names)) if names else ()


def _holds(categories: tuple[str, ...], classes: tuple[str, ...]) -> bool:
    names = tuple(dict.fromkeys(n for n in map(_name, categories) if n))
    if not names:
        return True
    leaves = _leaves(names)
    return all(any(_overlap(leaf, cls) for leaf in leaves) for cls in classes)


def fits(
    subject_categories: list[str] | tuple[str, ...],
    predicate: str,
    object_categories: list[str] | tuple[str, ...],
) -> bool:
    """Whether a statement from a subject with these categories to an object with these could
    use the predicate, by its Biolink domain and range (`_constraints`). A category fits a class
    when some class is below both, mixins included, so a broad category fits a narrower domain
    and a rule's `GeneOrGeneProduct` fits `gene`. Unknown or missing categories, and a predicate
    with no domain or range, constrain nothing."""
    return _fits(tuple(subject_categories), predicate, tuple(object_categories))


@lru_cache(maxsize=65536)
def _fits(subject: tuple[str, ...], predicate: str, object: tuple[str, ...]) -> bool:
    domain, range_ = _constraints(predicate)
    return _holds(subject, domain) and _holds(object, range_)


def oriented(
    subject_categories: list[str] | tuple[str, ...],
    predicate: str,
    object_categories: list[str] | tuple[str, ...],
) -> tuple[str, bool] | None:
    """How Biolink reads 'subject predicate object' between nodes of these categories, as
    (predicate, turned): the statement uses that predicate, from its subject to its object, or,
    if `turned`, from its object to its subject. None if the predicate fits neither way.

    A predicate that fits the subject and object (`fits`) stays. One defined the other way
    round is replaced by its inverse ('gene condition_associated_with_gene disease' reads 'gene
    gene_associated_with_condition disease'); a symmetric one fits either way. Then a predicate
    Biolink doesn't take as canonical is turned round to its canonical inverse where that fits
    ('disease condition_associated_with_gene gene' is 'gene gene_associated_with_condition
    disease'), so the same statement always reads the same way. A predicate with no inverse
    that fits only the other way round can't be put this way round, so it fits neither way.
    """
    return _oriented(tuple(subject_categories), predicate, tuple(object_categories))


@lru_cache(maxsize=65536)
def _oriented(
    subject: tuple[str, ...], predicate: str, object: tuple[str, ...]
) -> tuple[str, bool] | None:
    other = inverse(predicate)
    if _fits(subject, predicate, object):
        p = predicate
    elif other == predicate and _fits(object, predicate, subject):
        p = predicate
    elif other is not None and other != predicate and _fits(subject, other, object):
        p = other
    else:
        return None
    q = inverse(p)
    if (
        q is not None
        and q != p
        and not is_canonical(p)
        and is_canonical(q)
        and _fits(object, q, subject)
    ):
        return q, True
    return p, False


def element_name(curie: str) -> str:
    """The element's human name, e.g. 'treats or applied or studied to treat'."""
    name = _name(curie)
    if name is not None:
        return name
    return curie.split(":", 1)[-1].replace("_", " ")


def _slot_ranges(slot) -> tuple[str, ...]:
    ranges = [x.range for x in [*slot.any_of, *slot.exactly_one_of] if x.range]
    return tuple(ranges) if ranges else ((slot.range,) if slot.range else ())


@cache
def _association_table() -> tuple[_Association, ...]:
    tk = toolkit()
    view = tk.view
    table = []
    for name in tk.get_descendants("association", mixin=True):
        cls = view.get_class(name)
        if cls is None or cls.abstract or cls.mixin:
            continue
        predicate = view.induced_slot("predicate", name)
        # A class's own `subproperty_of` is more specific than an enum range it inherits
        # (druggable gene to disease: `target for`, inside GeneToDiseasePredicateEnum).
        allowed: tuple[str, ...] | None = None
        if predicate.subproperty_of:
            allowed = (predicate.subproperty_of,)
        elif predicate.range and view.get_enum(predicate.range):
            values = view.get_enum(predicate.range).permissible_values
            allowed = tuple(n for n in (_name(v) for v in values) if n)
        table.append(
            _Association(
                name=name,
                curie=_curie(name),
                depth=_depth(name),
                subject=_slot_ranges(view.induced_slot("subject", name)),
                object=_slot_ranges(view.induced_slot("object", name)),
                predicate=allowed,
            )
        )
    return tuple(table)


def _match_range(ranges: tuple[str, ...], categories: tuple[str, ...]) -> str | None:
    """The most specific of `ranges` holding one of `categories`, or None."""
    hits = [r for r in ranges if any(_under(c, r) for c in categories)]
    return max(hits, key=lambda r: (_depth(r), r)) if hits else None


def _fit(a: _Association, subjects: tuple[str, ...], predicate: str, objects: tuple[str, ...]):
    """(subject range, predicate constraint, object range) the edge matched, or None."""
    s = _match_range(a.subject, subjects)
    o = _match_range(a.object, objects)
    if s is None or o is None:
        return None
    if a.predicate is None:
        return (s, None, o)
    hits = [p for p in a.predicate if _under(predicate, p)]
    if not hits:
        return None
    return (s, max(hits, key=lambda p: (_depth(p), p)), o)


@lru_cache(maxsize=4096)
def _concrete(name: str) -> frozenset[str]:
    """What `name` covers: its descendants that are not mixins (all of them if none are)."""
    view = toolkit().view
    desc = _desc(name)
    concrete = frozenset(d for d in desc if not getattr(view.get_element(d), "mixin", False))
    return concrete or desc


def _at_least_as_specific(x: tuple, y: tuple) -> bool:
    """x's matched ranges and predicate each cover no more than y's do (None = any).

    Compared by what they cover rather than by is_a, because a mixin range such as
    `gene or gene product` is not below `named thing` although everything it covers is.
    """
    return all(
        b is None or (a is not None and _concrete(a) <= _concrete(b))
        for a, b in zip(x, y, strict=True)
    )


def associations(
    subject_categories: list[str], predicate: str, object_categories: list[str]
) -> list[str]:
    """Association classes that fit the edge, most specific first."""
    return list(_associations(tuple(subject_categories), predicate, tuple(object_categories)))


@lru_cache(maxsize=4096)
def _associations(
    subject_categories: tuple[str, ...], predicate: str, object_categories: tuple[str, ...]
) -> tuple[str, ...]:
    subjects = tuple(n for n in map(_name, subject_categories) if n)
    objects = tuple(n for n in map(_name, object_categories) if n)
    pred = _name(predicate)
    if not subjects or not objects or pred is None:
        return ()
    fits = {}
    for a in _association_table():
        matched = _fit(a, subjects, pred, objects)
        if matched is not None:
            fits[a] = matched
    # A class that constrains the predicate says more about the edge than one that takes any
    # predicate, whose broad ranges are often all that let it fit.
    if any(m[1] is not None for m in fits.values()):
        fits = {a: m for a, m in fits.items() if m[1] is not None}
    kept = [
        a
        for a, m in fits.items()
        if not any(
            _at_least_as_specific(n, m) and not _at_least_as_specific(m, n)
            for b, n in fits.items()
            if b is not a
        )
    ]
    kept.sort(key=lambda a: (-a.depth, -sum(_depth(x) for x in fits[a]), a.curie))
    return tuple(a.curie for a in kept)


def _first_sentence(text: str | None) -> str:
    text = " ".join((text or "").split())
    end = text.find(". ")
    return text[: end + 1] if end > 0 else text


def _spec(slot_name: str, association: str) -> QualifierSpec | None:
    view = toolkit().view
    slot = view.induced_slot(slot_name, association)
    key = slot_name.replace(" ", "_")
    description = _first_sentence(slot.description)
    if slot_name == "qualified predicate":
        parent = slot.subproperty_of or DEFAULT_QUALIFIED_PREDICATE
        values = tuple(sorted(_descendants(_curie(parent))))
        return QualifierSpec(key, values, _curie(parent), description)
    if not slot.range:
        return None
    enum = view.get_enum(slot.range)
    if enum is not None:
        return QualifierSpec(key, tuple(enum.permissible_values), enum.name, description)
    if view.get_class(slot.range) is not None:
        return QualifierSpec(key, None, _curie(slot.range), description)
    # Free text ('string', 'uriorcurie', 'frequency value'): nothing could check a proposal.
    return None


def allowed_qualifiers(
    subject_categories: list[str], predicate: str, object_categories: list[str]
) -> dict[str, QualifierSpec]:
    """Qualifier slots (bare names, e.g. 'object_direction_qualifier') the edge's association
    classes allow, with their permissible values."""
    return dict(_allowed_qualifiers(tuple(subject_categories), predicate, tuple(object_categories)))


@lru_cache(maxsize=4096)
def _allowed_qualifiers(
    subject_categories: tuple[str, ...], predicate: str, object_categories: tuple[str, ...]
) -> tuple[tuple[str, QualifierSpec], ...]:
    tk = toolkit()
    specs: dict[str, QualifierSpec] = {}
    # Most specific class first: where two classes allow a slot, its slot_usage wins.
    for curie in _associations(subject_categories, predicate, object_categories):
        name = _name(curie)
        for slot in tk.view.class_slots(name):
            if slot in GENERIC_QUALIFIERS or slot.replace(" ", "_") in specs:
                continue
            if not tk.is_qualifier(slot):
                continue
            spec = _spec(slot, name)
            if spec is not None:
                specs[spec.slot] = spec
    return tuple(sorted(specs.items()))


@cache
def _qualifier_specs() -> tuple[QualifierSpec, ...]:
    """Every enum-valued qualifier slot of every association class, as that class allows it
    (`qualified_predicate`'s values are predicates). Slots take their range from the
    association, so this reads every class's."""
    tk = toolkit()
    out = []
    for association in _association_table():
        for slot in tk.view.class_slots(association.name):
            if slot in GENERIC_QUALIFIERS or not tk.is_qualifier(slot):
                continue
            spec = _spec(slot, association.name)
            if spec is not None and spec.values is not None:
                out.append(spec)
    return tuple(out)


def qualifier_values() -> dict[str, tuple[str, ...]]:
    """Every value each enum-valued qualifier slot takes in some association class (bare slot
    names): all that step 3 could offer any statement, before the check of its own."""
    values: dict[str, set[str]] = {}
    for spec in _qualifier_specs():
        values.setdefault(spec.slot, set()).update(spec.values or ())
    return {slot: tuple(sorted(v)) for slot, v in sorted(values.items())}


def qualifier_enums() -> dict[str, str]:
    """The enum of every enum-valued qualifier slot (e.g. 'object_direction_qualifier' ->
    'DirectionQualifierEnum'); `qualified_predicate` takes predicates, not an enum, and isn't
    here."""
    out: dict[str, str] = {}
    for spec in _qualifier_specs():
        if spec.slot != "qualified_predicate" and spec.range:
            out.setdefault(spec.slot, spec.range)
    return dict(sorted(out.items()))


def enum_parent(enum: str, value: str) -> str | None:
    """The value that `value` is below in Biolink's `enum` (`is_a`): 'upregulated' ->
    'increased' in DirectionQualifierEnum. None for a top value, or one the enum lacks."""
    return _enum_parent(enum, value)


@lru_cache(maxsize=4096)
def _enum_parent(enum: str, value: str) -> str | None:
    definition = toolkit().view.get_enum(enum)
    entry = definition.permissible_values.get(value) if definition is not None else None
    return entry.is_a if entry is not None and entry.is_a else None


@lru_cache(maxsize=1)
def _prefix_classes() -> dict[str, frozenset[str]]:
    """id prefix -> names of the classes that list it in `id_prefixes`."""
    out: dict[str, set[str]] = {}
    for name, cls in toolkit().view.all_classes().items():
        for prefix in cls.id_prefixes or ():
            out.setdefault(prefix, set()).add(name)
    return {p: frozenset(c) for p, c in out.items()}


def _check_value(slot: str, spec: QualifierSpec, value: str, assocs: list[str]) -> list[str]:
    tk = toolkit()
    if slot == "qualified_predicate":
        if value not in (spec.values or ()):
            return [f"{slot}: {value!r} is not allowed here; use one of {list(spec.values or ())}"]
        if _constrains_qp(assocs):
            # bmt compares the value against element names, not CURIEs, so pass the name.
            ok = tk.validate_qualifier(f"biolink:{slot}", _name(value), associations=assocs)
            if not ok:
                return [f"{slot}: bmt rejects {value!r} for {', '.join(assocs)}"]
        return []
    if spec.values is not None:
        if value not in spec.values or not tk.is_permissible_value_of_enum(spec.range, value):
            return [f"{slot}: {value!r} is not a permissible value of {spec.range}"]
        if not tk.validate_qualifier(f"biolink:{slot}", value, associations=assocs):
            return [f"{slot}: bmt rejects {value!r} for {', '.join(assocs)}"]
        return []
    # An open class range: the value must be an identifier, and if Biolink knows its prefix,
    # one of the prefix's classes must be the range or below it. (bmt's own check here wants the
    # range itself among the prefix's classes, which rejects e.g. CL cells as anatomical
    # entities.)
    if not _CURIE.match(value):
        return [f"{slot}: {value!r} is not a CURIE (expected an id of a {spec.range})"]
    classes = _prefix_classes().get(value.split(":", 1)[0])
    range_name = _name(spec.range or "")
    if classes and range_name and not any(_under(c, range_name) for c in classes):
        return [f"{slot}: {value!r} has a prefix for {sorted(classes)}, not a {spec.range}"]
    return []


def _constrains_qp(assocs: list[str]) -> bool:
    view = toolkit().view
    return any(view.induced_slot("qualified predicate", _name(a)).subproperty_of for a in assocs)


def _slot_key(key: str) -> str:
    return key.removeprefix("biolink:").replace(" ", "_")


def validate_qualifiers(
    qualifiers: dict[str, Any],
    subject_categories: list[str],
    predicate: str,
    object_categories: list[str],
) -> list[str]:
    """Problems with a proposed qualifier set for the edge, in words; [] if it is valid."""
    if not qualifiers:
        return []
    assocs = associations(subject_categories, predicate, object_categories)
    if not assocs:
        return [
            f"no Biolink association class fits {list(subject_categories)} {predicate} "
            f"{list(object_categories)}, so no qualifiers can be checked"
        ]
    allowed = allowed_qualifiers(subject_categories, predicate, object_categories)
    errors: list[str] = []
    slots = [_slot_key(k) for k in qualifiers]
    for key, value in qualifiers.items():
        slot = _slot_key(key)
        if not toolkit().is_qualifier(f"biolink:{slot}"):
            errors.append(f"{key}: not a Biolink qualifier")
            continue
        spec = allowed.get(slot)
        if spec is None:
            errors.append(f"{slot}: not allowed on {', '.join(assocs)}")
            continue
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{slot}: expected one non-empty string, got {value!r}")
            continue
        errors.extend(_check_value(slot, spec, value, assocs))
    # Biolink: a qualified predicate refines the reading of a statement whose subject or object
    # is qualified; alone it would just replace the predicate.
    if "qualified_predicate" in slots and not any(
        s.startswith(("subject_", "object_")) for s in slots
    ):
        errors.append("qualified_predicate: needs a subject_* or object_* qualifier to go with it")
    return errors
