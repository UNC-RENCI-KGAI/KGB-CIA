"""Biolink's domain and range for each predicate, as sets of node categories.

ORION normalizes nodes and predicates separately and never checks that a predicate fits the
categories at its two ends. The metadata check validates the plan's declared classes, but
NodeNorm assigns the final category later: a DrugCentral indication the plan called a Disease
can come back a Procedure. This is where those edges are caught.

A predicate's domain and range come from the Biolink model (its own, or inherited from its
nearest ancestor that sets them). Each is expanded to every category under it, mixins
included, so an edge fits when any of the subject's categories is in the domain set and any of
the object's is in the range set. ORION writes a node's ancestors into `category`, so the test
is a set intersection.

A domain or range is enforced through the categories under it that are also under `named
thing`, since those are the only ones a node can carry. `subclass_of` (domain `ontology class`)
has none and is never enforced. Violations are reported by predicate and stay flags until a
predicate is known to be a source of wrong edges.

`NOT_ENFORCED` lists the sides Biolink gets wrong for this graph. The participation predicates
put `occurrent` on the participant's side, so every gene, protein, complex or chemical that takes
part in a Reactome pathway or catalyzes a reaction would be flagged: 262K edges on the Nov 9
graph, none of them wrong. The process side is still enforced.
"""

from __future__ import annotations

from functools import cache
from importlib.resources import files


@cache
def toolkit():
    from bmt import Toolkit

    schema = files("biolink_model") / "schema" / "biolink_model.yaml"
    # the empty predicate map stops bmt fetching one from GitHub at import time
    return Toolkit(str(schema), predicate_map={"predicate mappings": []})


@cache
def categories_under(name: str) -> frozenset[str]:
    """Every category CURIE under a Biolink class or mixin, itself included."""
    return frozenset(toolkit().get_descendants(name, formatted=True))


def _under(name: str) -> frozenset[str]:
    try:
        return categories_under(name)
    except ValueError:  # not a Biolink element
        return frozenset({name})


@cache
def most_specific(categories: tuple[str, ...]) -> tuple[str, ...]:
    """The categories in a node's list that no other category in the list is under, sorted.

    There can be more than one: NodeNorm gives many drugs both `Drug` and `SmallMolecule`, and
    neither is under the other. So never take the first category as the most specific one. A
    category Biolink doesn't know counts as specific.
    """
    present = set(categories)
    return tuple(sorted(c for c in present if not (_under(c) - {c}) & present))


@cache
def predicates_under(predicate: str) -> frozenset[str]:
    """A predicate CURIE and every predicate under it: `biolink:treats` includes
    `biolink:ameliorates_condition` and `biolink:preventative_for_condition`."""
    return frozenset(toolkit().get_descendants(predicate, formatted=True)) | {predicate}


@cache
def _named_things() -> frozenset[str]:
    return categories_under("named thing")


def _inherited(predicate: str, slot: str) -> str | None:
    """The predicate's own domain/range, or the nearest ancestor's."""
    tk = toolkit()
    for name in tk.get_ancestors(predicate):
        element = tk.get_element(name)
        value = getattr(element, slot, None) if element else None
        if value:
            return value
    return None


# Predicate -> the side not enforced, with why. Inverses are listed too, so a source that writes
# the other direction isn't flagged either.
NOT_ENFORCED: dict[str, str] = {
    "biolink:has_participant": "range",  # pathway has_participant gene: range is `occurrent`
    "biolink:participates_in": "domain",  # gene participates_in pathway: domain is `occurrent`
    "biolink:has_catalyst": "range",  # reaction has_catalyst protein: range is `occurrent`
    "biolink:catalyzes": "domain",  # protein catalyzes reaction: domain is `occurrent`
}


@cache
def constraint(predicate: str) -> tuple[frozenset[str] | None, frozenset[str] | None]:
    """(domain categories, range categories) for a predicate; None where nothing is enforced.

    Unknown predicates and domains or ranges outside `named thing` give None, as does
    `named thing` itself, which every category satisfies, and the sides in `NOT_ENFORCED`.
    """
    tk = toolkit()
    if not tk.is_predicate(predicate):
        return None, None
    sets = []
    for slot in ("domain", "range"):
        name = _inherited(predicate, slot)
        if not name or name == "named thing" or NOT_ENFORCED.get(predicate) == slot:
            sets.append(None)
            continue
        under = categories_under(name) & _named_things()
        sets.append(under or None)
    return sets[0], sets[1]


def expected(predicate: str, side: str) -> str | None:
    """The Biolink class a predicate's domain or range names ('disease or phenotypic feature'),
    its own or the nearest ancestor's; `side` is 'domain' or 'range'."""
    if not toolkit().is_predicate(predicate):
        return None
    return _inherited(predicate, side)


def violation(predicate: str, subject_categories, object_categories) -> str | None:
    """'domain', 'range', or None when the edge fits (or nothing is enforced)."""
    domain, range_ = constraint(predicate)
    if domain is not None and subject_categories and domain.isdisjoint(subject_categories):
        return "domain"
    if range_ is not None and object_categories and range_.isdisjoint(object_categories):
        return "range"
    return None
