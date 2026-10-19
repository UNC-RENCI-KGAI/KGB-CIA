"""Records passed between agents.

Every agent reads and writes these so the handoffs are stable. Change them in a PR with the
`integration` label and review from the teams on both sides of the handoff.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Provenance(BaseModel):
    """Where an edge came from. Required: explainability is 20% of the score."""

    source: str = Field(description="Dataset name, e.g. 'TCRD'")
    file: str = Field(description="File or table the edge was read from")
    columns: list[str] = Field(default_factory=list, description="Columns that produced it")
    evidence: list[str] = Field(default_factory=list, description="e.g. PubMed IDs added later")
    notes: str | None = None


class Node(BaseModel):
    id: str = Field(description="NodeNorm-preferred CURIE, e.g. 'NCBIGene:1017'")
    category: str = Field(description="Biolink class, e.g. 'biolink:Gene'")
    name: str | None = None
    original_ids: list[str] = Field(default_factory=list, description="IDs before NodeNorm")
    properties: dict[str, str | float | int | bool | None] = Field(default_factory=dict)


class Edge(BaseModel):
    subject: str = Field(description="Node.id")
    predicate: str = Field(description="Biolink predicate, e.g. 'biolink:binds'")
    object: str = Field(description="Node.id")
    properties: dict[str, str | float | int | bool | None] = Field(default_factory=dict)
    provenance: Provenance


class Graph(BaseModel):
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
