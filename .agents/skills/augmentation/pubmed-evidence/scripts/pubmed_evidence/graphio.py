"""Reading and writing the KGX graph, and the identity of an edge's statement.

A *statement* is what an edge claims, without its provenance: subject, predicate, object,
qualifiers and `negated`. Sources often give the same statement (DisGeNET and JensenLab both
give `gene_associated_with_condition`), so select lists each statement once, and a statement
step 6 proposes is one edge, however many originals it refines. Select, review and step 6 key
on `statement_id`, so it must depend only on the statement.

A *pair* is the unordered pair of nodes an edge joins. One PubMed search and one label request
serve every edge between them, in both directions.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import IO, Any

from pydantic import BaseModel, ValidationError

from shared import schema
from shared.pubmed import pmid_of as shared_pmid_of
from shared.schema import Edge, Node

NODES = "nodes.jsonl"
EDGES = "edges.jsonl"
# The `id` prefixes of an input edge that came without one (`edge_id`), and of an edge step 6
# adds.
EDGE_ID_PREFIX = "kgbcia-edge:"
ID_PREFIX = "kgbcia-augmentation:"
# Where step 6 lists the PMIDs it appended to an input edge's `publications`.
ADDED_PUBLICATIONS = "lit_added_publications"


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


def write_jsonl(path: Path, records: Iterable[BaseModel | dict], compact: bool = False) -> int:
    """Write one JSON object per line; models drop None fields, as ORION's KGX does. With
    `compact`, dicts are written without spaces too, keys in their order, as pydantic and ORION
    write them."""
    path.parent.mkdir(parents=True, exist_ok=True)
    separators = (",", ":") if compact else None
    n = 0
    with _open(path, "w") as f:
        for r in records:
            if isinstance(r, BaseModel):
                f.write(r.model_dump_json(exclude_none=True))
            else:
                f.write(json.dumps(r, ensure_ascii=False, separators=separators))
            f.write("\n")
            n += 1
    return n


def read_nodes(directory: Path) -> Iterator[Node]:
    for d in read_jsonl(graph_file(directory, NODES)):
        yield Node.model_validate(d)


def read_edges(directory: Path) -> Iterator[Edge]:
    for d in read_jsonl(graph_file(directory, EDGES)):
        yield Edge.model_validate(d)


# How many invalid edges a report lists by line number; the rest are only counted.
INVALID_EXAMPLES = 10


class InvalidEdges:
    """Edges that aren't valid `Edge` records, counted by reason, with the first few lines.

    Decision (review of #22, Nov 2026): one bad edge never stops the stage, or the pipeline.
    Select and bundle skip it, apply writes it out exactly as read, and each step's report
    counts it and the run prints a warning. Dropping bad edges is the final cleaning pass's job,
    not augmentation's."""

    def __init__(self) -> None:
        self.by_reason: Counter[str] = Counter()
        self.examples: list[dict[str, Any]] = []

    def check(self, row: Any, line: int) -> Edge | None:
        """The row as an Edge, or None (counted) if it isn't a valid one."""
        try:
            return Edge.model_validate(row)
        except ValidationError as e:
            problem = e.errors()[0]
            where = ".".join(map(str, problem["loc"])) or "record"
            reason = f"{where}: {problem['msg']}"
            self.by_reason[reason] += 1
            if len(self.examples) < INVALID_EXAMPLES:
                self.examples.append({"line": line, "reason": reason})
            return None

    @property
    def count(self) -> int:
        return sum(self.by_reason.values())

    def report(self) -> dict[str, Any]:
        return {
            "edges": self.count,
            "by_reason": dict(self.by_reason.most_common()),
            "examples": self.examples,
        }


def invalid_warning(step: str, invalid: dict[str, Any], path: Path, done: str) -> str | None:
    """The warning a step prints for the invalid edges its report lists; None if none."""
    if not invalid["edges"]:
        return None
    reason, n = next(iter(invalid["by_reason"].items()))
    lines = ", ".join(str(x["line"]) for x in invalid["examples"])
    return (
        f"{step}: warning: {invalid['edges']:,} edges in {path} aren't valid Edge records and "
        f"were {done} (most common: {reason}, {n:,} edges; lines {lines}); see "
        f"invalid_edges in the report"
    )


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


def statement_id(
    subject: str,
    predicate: str,
    object: str,
    qualifiers: dict[str, Any] | None = None,
    negated: bool = False,
) -> str:
    """Stable id of a statement: `shared.schema.statement_id`."""
    return schema.statement_id(subject, predicate, object, qualifiers, negated)


def edge_id(record: dict[str, Any]) -> str:
    """The `id` an input edge goes by: its own, or for an edge without one `kgbcia-edge:` plus
    the first 16 hex digits of the SHA-256 of its canonical JSON (sorted keys, the record as
    read). The hash changes if the transformation's output for the edge does."""
    if record.get("id"):
        return record["id"]
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return EDGE_ID_PREFIX + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def as_cited(record: dict[str, Any]) -> dict[str, Any]:
    """The edge record with its `publications` as its source cited them: the PMIDs an earlier
    step 6 appended (its `lit_added_publications`) taken back out, keys in their order. The
    same record if it has none."""
    added = record.get(ADDED_PUBLICATIONS)
    publications = record.get("publications")
    if not isinstance(added, list) or not added or not isinstance(publications, list):
        return record
    appended = set(added)
    return {**record, "publications": [p for p in publications if p not in appended]}


def proposed_edge_id(statement_id: str) -> str:
    """The `id` of an edge step 6 proposes: `kgbcia-augmentation:` plus the first 16 hex digits
    of the SHA-256 of `statement_id`, the proposal's key (`refine.statement_key`, one for a
    symmetric statement either way round). So a statement has one id whichever originals it
    refines, in any graph."""
    return ID_PREFIX + hashlib.sha256(statement_id.encode("utf-8")).hexdigest()[:16]


def edge_statement(edge: Edge) -> schema.Statement:
    """What the edge says: its subject, predicate, object, qualifiers (values as strings) and
    `negated`, with a bare `qualified_predicate` given its `biolink:` prefix."""
    qualifiers = {k: str(v) for k, v in qualifiers_of(edge).items()}
    qp = qualifiers.pop("qualified_predicate", None)
    if qp is not None and not qp.startswith("biolink:"):
        qp = "biolink:" + qp
    return schema.Statement(
        subject=edge.subject,
        predicate=edge.predicate,
        object=edge.object,
        qualified_predicate=qp,
        qualifiers=qualifiers,
        negated=is_negated(edge),
    )


def edge_statement_id(edge: Edge) -> str:
    """The id of what the edge says: `edge_statement(edge).id`, so an edge and the statement
    refine reads from it always share one id."""
    return edge_statement(edge).id


def pair_id(a: str, b: str) -> str:
    """Unordered pair of node ids, e.g. 'CHEBI:45783|NCBIGene:25'. CURIEs never contain '|'."""
    return "|".join(sorted((a, b)))


def pair_nodes(pid: str) -> tuple[str, str]:
    a, b = pid.split("|")
    return a, b


def pmid_of(publication: str) -> str | None:
    """'PMID:12345' -> '12345'; anything that isn't a PubMed id -> None. The prefix is matched in
    any case ('pmid:', 'PmId:'), with spaces allowed around its parts, and a PubMed article URL
    gives its id: `shared.pubmed.pmid_of`, so this agent and the cleaning stage's evidence check
    read citations the same way."""
    return shared_pmid_of(publication)


def parse_pmid(value: Any) -> str | None:
    """The digits of a PMID however it is written by hand ('12345', 12345, 'PMID:12345',
    'PMID 12345'); None if there are none."""
    if value is None or isinstance(value, bool):
        return None
    m = re.search(r"\d+", str(value))
    return m.group(0) if m else None


def pmid_key(pmid: str) -> tuple[bool, int, str]:
    """Sort key for bare PMIDs: by number, anything that isn't digits last."""
    return (not pmid.isdigit(), int(pmid) if pmid.isdigit() else 0, pmid)


def publication_of(pmid: str) -> str:
    return f"PMID:{pmid}"


def chunks[T](items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    """`items` in consecutive slices of at most `size` (at least 1) each."""
    size = max(1, size)
    for i in range(0, len(items), size):
        yield items[i : i + size]
