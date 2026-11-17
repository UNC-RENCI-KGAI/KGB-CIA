"""The records of the files the steps hand each other in the work directory, and their readers.

reference/work-files.md, "Work directory", lists each file, who writes it and its fields.
`Entity` and `Pair` are `shared.pubmed`'s, with the fields step 1 adds; step 2's records are
`shared.pubmed`'s own.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

import shared.pubmed
from pubmed_evidence.graphio import read_jsonl

ENTITIES = "entities.jsonl"
STATEMENTS = "statements.jsonl"
PAIRS = "pairs.jsonl"
SCOPE_REPORT = "scope_report.json"
PUBMED = "pubmed.jsonl"
LABEL_RUN = "label_run.json"
REFINEMENTS = "refinements.jsonl"
APPLY_REPORT = "apply_report.json"


class Entity(shared.pubmed.Entity):
    """A node in a kept pair, with what step 2 needs to search for it and step 3 to name it.
    Its `synonyms` are the node's own; step 2 adds NodeNorm's labels and records the final list
    in `PubmedRecord.synonyms`."""

    category: list[str] = Field(default_factory=list)


class SelectedStatement(BaseModel):
    """One distinct statement (subject, predicate, object, qualifiers, `negated`) among the kept
    edges."""

    statement_id: str = Field(description="graphio.statement_id of the statement")
    pair_id: str = Field(description="graphio.pair_id of subject and object")
    subject: str
    predicate: str
    object: str
    qualifiers: dict[str, Any] = Field(default_factory=dict)
    negated: bool = Field(
        default=False, description="The edges deny it: a negated source edge, judged as one"
    )
    rule: str = Field(
        description="Id of the statement rule that kept it, e.g. 'chemical-treats-disease', "
        "whose predicates its pair's claims may use"
    )
    provenance_rule: str | None = Field(
        default=None,
        description="Id of the provenance rule that set its tier and cap instead, e.g. "
        "'text-co-occurrence', when one lowered every kept edge making it; None otherwise",
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


class Pair(shared.pubmed.Pair):
    """A kept node pair, with its statements; its `source_pmids` are the union of theirs, and
    its `rules` their statement rules (never a provenance rule)."""

    statement_ids: list[str]
    rules: list[str] = Field(default_factory=list)
    source_pmid_pairs: dict[str, int] = Field(
        default_factory=dict,
        description="Each of `source_pmids` -> the number of pairs select kept that cite it, "
        "this one included. Counted over the whole selection and carried by each pair, so "
        "pairs.jsonl split into parts keeps them. Step 3's source screen reads them "
        "(config.SOURCE_SCREEN_MIN_PAIRS); a pairs.jsonl from before them screens nothing",
    )


class LabelRun(BaseModel):
    """The run that steps 4 to 6 and the full run use: the last label run, the quote repairs
    that count, the review that followed it, if any, and the UBERON release the ground step
    pinned (reference/work-files.md, "`label_run.json` (`LabelRun`)")."""

    model: str
    prompt_version: str
    prompt: str | None = Field(default=None, description="Path of the prompt file used")
    reasoning_effort: str | None = Field(
        default=None,
        description="Effort the run sent ('omit' when it sent none; 'none' is a level); the "
        "full run continues with it. None in a file written before it was recorded",
    )
    reviewer: str | None = None
    review_prompt_version: str | None = None
    review_reasoning_effort: str | None = Field(
        default=None,
        description="Effort the review sent ('omit' when it sent none; 'none' is a level); "
        "the full run continues with it while its reviewer reviews",
    )
    repairer: str | None = Field(
        default=None,
        description="The last of `repairers`, the repair model when its pass ran: the one "
        "repairer a file from before `repairers` names, read only when `repairers` is absent",
    )
    repair_prompt_version: str | None = Field(
        default=None, description="`repairer`'s repair prompt (or code) version"
    )
    repairers: list[tuple[str, str]] | None = Field(
        default=None,
        description="(repairer, repair prompt version) of the quote repairs that count, in "
        "priority order: a claim's sentence is the first of their repairs that was found, "
        "e.g. [('code', 'align-2'), ('gpt-5.6-terra', 'repair@...')]. None in a file written "
        "before code repairs",
    )
    uberon_version: str | None = Field(
        default=None,
        description="data-version of the uberon-basic.obo in the cache directory, whose "
        "is_a/part_of closure decides which anatomy is at or below which; the ground step "
        "records it",
    )

    def repair_order(self) -> list[tuple[str, str]]:
        """(repairer, repair prompt version) of the repairs that count, in priority order:
        `repairers`, or in a file without it, the one `repairer` it names; [] for none."""
        if self.repairers is not None:
            return [(r, v) for r, v in self.repairers]
        if self.repairer and self.repair_prompt_version:
            return [(self.repairer, self.repair_prompt_version)]
        return []

    def set_repairs(self, order: list[tuple[str, str]]) -> None:
        """Name the repairs that count, in priority order, with the last of them in `repairer`
        and `repair_prompt_version` too, for a reader of those two alone."""
        self.repairers = [(r, v) for r, v in order] or None
        self.repairer, self.repair_prompt_version = order[-1] if order else (None, None)


def read_records[M: BaseModel](path: Path, model: type[M]) -> Iterator[M]:
    for d in read_jsonl(path):
        yield model.model_validate(d)


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
