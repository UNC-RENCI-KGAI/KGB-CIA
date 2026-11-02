"""Reading and writing the KGX graph, and the identity of an edge's statement.

A *statement* is what an edge claims, without its provenance: subject, predicate, object,
qualifiers and `negated`. Sources often give the same statement (DisGeNET and JensenLab both
give `gene_associated_with_condition`), so step 5 labels each statement once and step 6 applies
the result to every edge that makes it. Steps 2, 5 and 6 all key on `statement_id`, so it must
depend only on the statement.

A *pair* is the unordered pair of nodes an edge joins. One PubMed search serves every edge
between them, in both directions.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import IO, Any

from pydantic import BaseModel

from shared.schema import Edge, Node

NODES = "nodes.jsonl"
EDGES = "edges.jsonl"


def _open(path: Path, mode: str) -> IO[str]:
    if path.suffix == ".gz":
        return gzip.open(path, mode + "t", encoding="utf-8")
    return path.open(mode, encoding="utf-8")


def graph_file(directory: Path, name: str) -> Path:
    """`directory/name` if it exists, else the first that does of its `.gz` and ORION's own
    names for it (`normalized_nodes.jsonl`, `normalized_edges.jsonl`, and their `.gz`), else
    `directory/name`."""
    for candidate in (name, name + ".gz", "normalized_" + name, "normalized_" + name + ".gz"):
        if (directory / candidate).exists():
            return directory / candidate
    return directory / name


def read_jsonl(path: Path) -> Iterator[dict]:
    with _open(path, "r") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def write_jsonl(path: Path, records: Iterable[BaseModel | dict]) -> int:
    """Write one JSON object per line; models drop None fields, as ORION's KGX does."""
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with _open(path, "w") as f:
        for r in records:
            if isinstance(r, BaseModel):
                f.write(r.model_dump_json(exclude_none=True))
            else:
                f.write(json.dumps(r, ensure_ascii=False))
            f.write("\n")
            n += 1
    return n


def read_nodes(directory: Path) -> Iterator[Node]:
    for d in read_jsonl(graph_file(directory, NODES)):
        yield Node.model_validate(d)


def read_edges(directory: Path) -> Iterator[Edge]:
    for d in read_jsonl(graph_file(directory, EDGES)):
        yield Edge.model_validate(d)


def extras(record: BaseModel) -> dict[str, Any]:
    """The extra Biolink slots on a Node or Edge (pydantic's `model_extra`)."""
    return dict(record.model_extra or {})


def is_qualifier_slot(slot: str) -> bool:
    return slot == "qualified_predicate" or slot.endswith("_qualifier")


def qualifiers_of(edge: Edge) -> dict[str, Any]:
    """The edge's qualifier slots: `qualified_predicate` and every `*_qualifier` key."""
    return {k: v for k, v in sorted(extras(edge).items()) if is_qualifier_slot(k) and v}


def is_negated(edge: Edge) -> bool:
    value = extras(edge).get("negated")
    return value is True or (isinstance(value, str) and value.lower() == "true")


def statement_key(
    subject: str,
    predicate: str,
    object: str,
    qualifiers: dict[str, Any] | None = None,
    negated: bool = False,
) -> dict[str, Any]:
    return {
        "subject": subject,
        "predicate": predicate,
        "object": object,
        "qualifiers": dict(sorted((qualifiers or {}).items())),
        "negated": bool(negated),
    }


def statement_id(
    subject: str,
    predicate: str,
    object: str,
    qualifiers: dict[str, Any] | None = None,
    negated: bool = False,
) -> str:
    """Stable id of a statement: the first 16 hex digits of the SHA-256 of its canonical JSON."""
    key = statement_key(subject, predicate, object, qualifiers, negated)
    canonical = json.dumps(key, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def edge_statement_id(edge: Edge) -> str:
    return statement_id(
        edge.subject, edge.predicate, edge.object, qualifiers_of(edge), is_negated(edge)
    )


def pair_id(a: str, b: str) -> str:
    """Unordered pair of node ids, e.g. 'CHEBI:45783|NCBIGene:25'. CURIEs never contain '|'."""
    return "|".join(sorted((a, b)))


def pair_nodes(pid: str) -> tuple[str, str]:
    a, b = pid.split("|")
    return a, b


def pmid_of(publication: str) -> str | None:
    """'PMID:12345' -> '12345'; anything that isn't a PubMed id -> None."""
    prefix, _, local = publication.partition(":")
    if prefix.upper() == "PMID" and local.strip().isdigit():
        return local.strip()
    return None


def publication_of(pmid: str) -> str:
    return f"PMID:{pmid}"
