"""The files the steps hand each other, in the work directory (`<output>/work` by default).

Each step reads the files of the steps before it and writes its own, so any step can be rerun
alone. The caches (`cache.py`) hold what costs requests or tokens; these files are cheap to
rebuild.

| File | Written by | One line per |
| --- | --- | --- |
| `entities.jsonl` | select (step 2) | node in a kept pair (`Entity`) |
| `statements.jsonl` | select (step 2) | distinct statement of a kept edge (`Statement`) |
| `pairs.jsonl` | select (step 2) | kept node pair (`Pair`) |
| `scope_report.json` | select (step 2) | counts per rule and per skipped edge type |
| `pubmed.jsonl` | pubmed (step 3) | pair searched (`PubmedRecord`) |
| `sentences.jsonl` | sentences (step 4) | statement (`SentenceRecord`) |
| `label_run.json` | label (step 5), review | the run step 6 applies (`LabelRun`) |
| `apply_report.json` | apply (step 6) | counts of what step 6 added and skipped |

PMIDs are bare digits ('12345') everywhere in the work files and caches, and 'PMID:12345' in
the graph's `publications`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, Field

from pubmed_evidence.graphio import read_jsonl, write_jsonl

ENTITIES = "entities.jsonl"
STATEMENTS = "statements.jsonl"
PAIRS = "pairs.jsonl"
SCOPE_REPORT = "scope_report.json"
PUBMED = "pubmed.jsonl"
SENTENCES = "sentences.jsonl"
LABEL_RUN = "label_run.json"
APPLY_REPORT = "apply_report.json"


class Entity(BaseModel):
    """A node in a kept pair, with what step 3 needs to search for it and step 4 to name it."""

    id: str
    name: str | None = None
    category: list[str] = Field(default_factory=list)
    equivalent_identifiers: list[str] = Field(default_factory=list)
    synonyms: list[str] = Field(
        default_factory=list,
        description="From the node's own `symbol`, `synonym` and `full_name` slots. Step 3 adds "
        "NodeNorm's labels and records the final list in PubmedRecord.synonyms",
    )


class Statement(BaseModel):
    """One distinct statement (subject, predicate, object, qualifiers) among the kept edges."""

    statement_id: str = Field(description="graphio.statement_id of the statement")
    pair_id: str = Field(description="graphio.pair_id of subject and object")
    subject: str
    predicate: str
    object: str
    qualifiers: dict[str, Any] = Field(default_factory=dict)
    rule: str = Field(
        description="Id of the scope rule that kept it, e.g. 'chemical-treats-disease'"
    )
    edge_count: int = Field(description="Edges in the input graph that make this statement")
    sources: list[str] = Field(
        default_factory=list, description="primary_knowledge_source of those edges"
    )
    source_pmids: list[str] = Field(
        default_factory=list, description="PMIDs in those edges' publications, bare digits"
    )
    score: float | None = Field(
        default=None, description="Best confidence score among those edges, for capped rules"
    )


class Pair(BaseModel):
    pair_id: str
    nodes: tuple[str, str] = Field(description="The pair's node ids, sorted")
    statement_ids: list[str]
    source_pmids: list[str] = Field(
        default_factory=list, description="Union of its statements' source PMIDs"
    )
    rules: list[str] = Field(default_factory=list)


class FetchedPmid(BaseModel):
    pmid: str
    origin: Literal["search", "source", "source+search"]
    rank: int | None = Field(default=None, description="1-based relevance rank in the search")


class PubmedRecord(BaseModel):
    """What step 3 found for one pair. Titles and abstracts are in the PubMed cache."""

    pair_id: str
    query: str
    retrieved: str = Field(description="ISO date of the search, e.g. '2026-11-02'")
    total: int = Field(description="ESearch's total hit count")
    synonyms: dict[str, list[str]] = Field(
        default_factory=dict, description="Node id -> the [tiab] terms used in the query"
    )
    mesh: dict[str, list[str]] = Field(
        default_factory=dict, description="Node id -> the [mh] headings used in the query"
    )
    pmids: list[FetchedPmid] = Field(
        default_factory=list, description="Fetched PMIDs that have an abstract, search ones first"
    )
    no_abstract: list[str] = Field(
        default_factory=list, description="Fetched PMIDs PubMed has no abstract for"
    )


class SentenceRecord(BaseModel):
    statement_id: str
    sentence: str


class LabelRun(BaseModel):
    """Which labels step 6 applies: the model and prompt of the last label run, and of the
    review that followed it, if any."""

    model: str
    prompt_version: str
    prompt: str | None = Field(default=None, description="Path of the prompt file used")
    reviewer: str | None = None
    review_prompt_version: str | None = None


M = TypeVar("M", bound=BaseModel)


def read_records(path: Path, model: type[M]) -> Iterator[M]:
    for d in read_jsonl(path):
        yield model.model_validate(d)


def write_records(path: Path, records) -> int:
    return write_jsonl(path, records)


def read_label_run(work: Path) -> LabelRun | None:
    path = work / LABEL_RUN
    if not path.exists():
        return None
    return LabelRun.model_validate_json(path.read_text(encoding="utf-8"))


def write_label_run(work: Path, run: LabelRun) -> None:
    work.mkdir(parents=True, exist_ok=True)
    (work / LABEL_RUN).write_text(run.model_dump_json(indent=2) + "\n", encoding="utf-8")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
