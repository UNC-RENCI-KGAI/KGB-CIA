"""UBERON's is_a/part_of closure, which decides whether one anatomical context is at or below
another.

A claim's anatomy is grounded to an UBERON term (the ground step). One term is at or below
another when they're equal or the other is its ancestor by is_a or part_of, in any mix: the
liver is at or below the digestive system, but not below the kidney (`refine.Lattice`, which
takes `Uberon.ancestors`). The closure is built at load from `uberon-basic.obo` in the cache
directory, the release config.UBERON_URL pins, fetched once; its `data-version` names the
release a run used. No dependency: the OBO file is read line by line, and its 15,000 live terms
and 453,000 ancestor pairs take about 0.1 s.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from pubmed_evidence import config

FETCH_TIMEOUT = 120.0
PARENT_RELATIONS = ("part_of",)  # besides is_a


@dataclass(frozen=True)
class Uberon:
    """A release's live terms, each mapped to its ancestors by is_a and part_of (itself not
    among them), and the release's `data-version`, None if the file has none."""

    version: str | None
    ancestors: Mapping[str, frozenset[str]] = field(default_factory=dict)


def _target(value: str) -> str | None:
    """The id a tag's value starts with, without the trailing {qualifiers} and ! comment."""
    words = value.split("!", 1)[0].split("{", 1)[0].split()
    return words[0] if words else None


def _closure(parents: Mapping[str, set[str]]) -> dict[str, frozenset[str]]:
    done: dict[str, frozenset[str]] = {}
    visiting: set[str] = set()

    def visit(term: str) -> frozenset[str]:
        if term in done:
            return done[term]
        visiting.add(term)
        out: set[str] = set()
        for parent in parents.get(term, ()):
            out.add(parent)
            if parent not in visiting:  # a cycle, which UBERON doesn't have, stops here
                out |= visit(parent)
        visiting.discard(term)
        done[term] = frozenset(out - {term})
        return done[term]

    for term in parents:
        visit(term)
    return done


def parse(lines: Iterable[str]) -> Uberon:
    """The closure of an OBO file's lines. Only `[Term]` stanzas count, and obsolete terms are
    left out. A term's parents are its `is_a` lines and its `relationship: part_of` lines."""
    version: str | None = None
    parents: dict[str, set[str]] = {}
    obsolete: set[str] = set()
    term: str | None = None
    in_term = False
    for raw in lines:
        line = raw.strip()
        if line.startswith("["):
            in_term, term = line == "[Term]", None
            continue
        tag, _, value = line.partition(":")
        value = value.strip()
        if not in_term:
            if tag == "data-version" and version is None:
                version = value or None
        elif tag == "id":
            term = value
            parents.setdefault(term, set())
        elif term is None:
            continue
        elif tag == "is_a" and (parent := _target(value)):
            parents[term].add(parent)
        elif tag == "relationship":
            relation, _, rest = value.partition(" ")
            if relation in PARENT_RELATIONS and (parent := _target(rest)):
                parents[term].add(parent)
        elif tag == "is_obsolete" and value == "true":
            obsolete.add(term)
    closure = _closure(parents)
    return Uberon(version, {t: a for t, a in closure.items() if t not in obsolete})


def load(path: Path) -> Uberon:
    with path.open(encoding="utf-8") as f:
        return parse(f)


def fetch(path: Path, url: str = config.UBERON_URL, client: httpx.Client | None = None) -> None:
    """Download the release at `url` to `path`, following redirects. It's written to a
    temporary file first and moved into place only if it reads as an OBO file with terms, so a
    failed or cut-off download leaves nothing behind. Raises httpx.HTTPError or ValueError."""
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    http = client if client is not None else httpx.Client(timeout=FETCH_TIMEOUT)
    try:
        with http.stream("GET", url, follow_redirects=True) as response:
            response.raise_for_status()
            with part.open("wb") as f:
                for chunk in response.iter_bytes():
                    f.write(chunk)
        if not load(part).ancestors:
            raise ValueError(f"{url} has no OBO terms")
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)
        if client is None:
            http.close()


def ensure(path: Path, url: str = config.UBERON_URL, client: httpx.Client | None = None) -> Uberon:
    """The closure of the release at `path`, fetched from `url` first if it isn't there."""
    if not path.exists():
        fetch(path, url, client)
    return load(path)
