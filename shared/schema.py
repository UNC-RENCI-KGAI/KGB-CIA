"""Records passed between agents.

Every agent reads and writes these so the handoffs are stable. Change them in a PR with the
`integration` label and review from the teams on both sides of the handoff.

`Node` and `Edge` are KGX records shaped like the `nodes.jsonl` and `edges.jsonl` ORION writes
for ROBOKOP: one flat JSON object per line, Biolink slot names as keys. The named fields are the
ones every record needs; any other Biolink slot (`in_taxon`, `symbol`, `p_value`, qualifiers such
as `object_aspect_qualifier`, ...) goes on the record as an extra top-level key, as ORION does.
`model_dump_json(exclude_none=True)` gives one line of the file.

Provenance lives on the edge in Biolink terms: `primary_knowledge_source`,
`aggregator_knowledge_source`, `knowledge_level`, `agent_type` and `publications`. Which source
file and columns produced a node or edge is not Biolink, so it is kept out of the graph, in the
`TransformReport` the transformation agent writes next to it, which points into the source's
Croissant metadata.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Biolink 4.4.6 KnowledgeLevelEnum and AgentTypeEnum.
KnowledgeLevel = Literal[
    "knowledge_assertion",
    "logical_entailment",
    "prediction",
    "statistical_association",
    "text_co_occurrence",
    "observation",
    "not_provided",
]
AgentType = Literal[
    "manual_agent",
    "automated_agent",
    "data_analysis_pipeline",
    "computational_model",
    "text_mining_agent",
    "image_processing_agent",
    "manual_validation_of_automated_agent",
    "not_provided",
]


def _require_prefix(value: str, prefix: str) -> str:
    if not value.startswith(prefix):
        raise ValueError(f"expected a {prefix!r} CURIE, got {value!r}")
    return value


class Node(BaseModel):
    """One line of nodes.jsonl. Extra Biolink node slots are allowed as top-level keys."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(description="NodeNorm-preferred CURIE, e.g. 'NCBIGene:1017'")
    category: list[str] = Field(
        min_length=1,
        description="Biolink classes, most specific first, e.g. ['biolink:Gene', ...]",
    )
    name: str | None = None
    description: str | None = None
    equivalent_identifiers: list[str] = Field(
        default_factory=list,
        description="All CURIEs NodeNorm merged into this node (ORION/KGX convention)",
    )

    @field_validator("category")
    @classmethod
    def _biolink_categories(cls, v: list[str]) -> list[str]:
        return [_require_prefix(c, "biolink:") for c in v]


class Edge(BaseModel):
    """One line of edges.jsonl. Extra Biolink edge slots and qualifiers are allowed.

    The knowledge source, knowledge level and agent type are required: explainability is 20%
    of the score, and the MCP interface answers "why does A connect to B?" from them.
    """

    model_config = ConfigDict(extra="allow")

    id: str | None = None
    subject: str = Field(description="Node.id")
    predicate: str = Field(description="Biolink predicate, e.g. 'biolink:affects'")
    object: str = Field(description="Node.id")
    primary_knowledge_source: str = Field(
        description="infores CURIE of the resource that first asserted this, e.g. 'infores:chembl'"
    )
    aggregator_knowledge_source: list[str] = Field(
        default_factory=list,
        description="infores CURIEs that passed it along, e.g. ['infores:tcrd']",
    )
    knowledge_level: KnowledgeLevel
    agent_type: AgentType
    publications: list[str] = Field(default_factory=list, description="e.g. 'PMID:12345'")
    original_subject: str | None = Field(default=None, description="Subject ID before NodeNorm")
    original_object: str | None = Field(default=None, description="Object ID before NodeNorm")
    original_predicate: str | None = None

    @field_validator("predicate")
    @classmethod
    def _biolink_predicate(cls, v: str) -> str:
        return _require_prefix(v, "biolink:")

    @field_validator("primary_knowledge_source")
    @classmethod
    def _infores_primary(cls, v: str) -> str:
        return _require_prefix(v, "infores:")

    @field_validator("aggregator_knowledge_source")
    @classmethod
    def _infores_aggregators(cls, v: list[str]) -> list[str]:
        return [_require_prefix(s, "infores:") for s in v]


class Graph(BaseModel):
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)


class ColumnRef(BaseModel):
    """A column in a source file, as its Croissant metadata names it."""

    field: str = Field(description="The cr:Field @id in the croissant")
    name: str = Field(description="Column name in the file, e.g. 'lig'")


class Mapping(BaseModel):
    """One rule the transformation agent applied: these columns of this file became these
    nodes or edges."""

    id: str = Field(description="Unique within the report, e.g. 'tcrd.cmpd_activity.interacts'")
    file: str = Field(description="The cr:FileObject or cr:FileSet @id the rows came from")
    record_set: str | None = Field(default=None, description="The cr:RecordSet @id, if any")
    produces: Literal["node", "edge"]
    category: str | None = Field(default=None, description="Biolink class, for node mappings")
    subject_category: str | None = Field(default=None, description="For edge mappings")
    predicate: str | None = Field(default=None, description="For edge mappings")
    object_category: str | None = Field(default=None, description="For edge mappings")
    columns: dict[str, list[ColumnRef]] = Field(
        default_factory=dict,
        description="Biolink slot filled -> columns read for it, e.g. "
        "{'subject': [...], 'object': [...], 'publications': [...]}",
    )
    constants: dict[str, str | float | int | bool] = Field(
        default_factory=dict,
        description="Slots set to a fixed value, e.g. {'knowledge_level': 'knowledge_assertion'}",
    )
    conditions: list[str] = Field(
        default_factory=list, description="Row filters applied, in words, e.g. 'act_type == Ki'"
    )
    code: str | None = Field(default=None, description="Path of the transform code that ran it")
    rows_read: int | None = None
    records_written: int | None = None


class TransformReport(BaseModel):
    """What the transformation agent read to build its graph: which columns of which files
    became which nodes and edges. Written next to the graph, one per source."""

    source: str = Field(description="infores CURIE of the source, e.g. 'infores:tcrd'")
    source_version: str | None = None
    croissant: str = Field(description="Path or URL of the Croissant metadata used")
    mappings: list[Mapping] = Field(default_factory=list)
    unused_columns: list[ColumnRef] = Field(
        default_factory=list, description="Columns looked at and deliberately not mapped"
    )
