"""A node's most specific Biolink categories, from the Biolink model the graph was built with.

ORION writes every ancestor category onto a node, and the Neo4j dump turns each into a label,
so a gene carries `Gene`, `BiologicalEntity`, `OntologyClass` and more. Counting edges once per
label counts the same edge several times. This module keeps only the categories no other
category on the node is under.
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import cache
from importlib.resources import files


@cache
def _toolkit():
    from bmt import Toolkit

    schema = files("biolink_model") / "schema" / "biolink_model.yaml"
    # the empty predicate map stops bmt fetching one from GitHub at import time
    return Toolkit(str(schema), predicate_map={"predicate mappings": []})


@cache
def _under(category: str) -> frozenset[str]:
    """Every category under a Biolink class or mixin, itself included."""

    try:
        return frozenset(_toolkit().get_descendants(category, formatted=True)) | {category}
    except ValueError:  # not a Biolink element
        return frozenset({category})


def most_specific(categories: Iterable[str]) -> tuple[str, ...]:
    """The categories in the list that no other category in the list is under, sorted.

    There can be more than one: NodeNorm gives many drugs both `Drug` and `SmallMolecule`, and
    neither is under the other, so never take the first category. A category Biolink doesn't
    know counts as specific.
    """

    present = set(categories)
    return tuple(sorted(c for c in present if not (_under(c) - {c}) & present))
