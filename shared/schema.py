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

`AugmentedEdge` is an `Edge` the augmentation agent checked against the PubMed literature: an
input edge, or a more specific edge the literature states, proposed from the originals it names.
Its `lit_*` fields carry the support score (how many PubMed abstracts support the edge), their
PMIDs, the more specific edges that refine it and, in `lit_evidence`, one `PublicationEvidence`
per abstract read for the edge's node pair, with the `ClaimRecord`s found in it. An input edge's
`publications` gain the supporting PMIDs its source didn't cite, appended after the source's own
and listed again in `lit_added_publications`; a proposed edge's `publications` are the PMIDs that
support it. Its fields are top-level keys like any other slot, so a stage that reads `Edge` still
reads it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


# The augmentation agent's literature vocabulary.
ArticleType = Literal["research", "review", "opinion", "guideline", "case_report", "other"]
Origin = Literal["source", "search", "pubtator"]
StudyDesign = Literal[
    "meta_analysis",
    "systematic_review",
    "rct",
    "cohort",
    "case_control",
    "cross_sectional",
    "animal",
    "in_vitro",
    "case_report",
    "narrative_review",
    "computational",
]
Polarity = Literal["positive", "negative"]
ClaimRole = Literal["primary_finding", "background", "hypothesis"]
NegativeFindingType = Literal["demonstrated_absence", "failed_to_detect"]
ReviewVerdict = Literal["upheld", "reject"]


class Statement(BaseModel):
    """What an edge or a literature claim says, without provenance: subject, predicate, object,
    qualifiers and `negated`, by their Biolink names. A `qualified_predicate` replaces the
    predicate in the full reading, and `negated` negates the full statement."""

    subject: str = Field(description="Node.id")
    predicate: str = Field(description="Biolink predicate, e.g. 'biolink:affects'")
    object: str = Field(description="Node.id")
    qualified_predicate: str | None = Field(default=None, description="e.g. 'biolink:causes'")
    qualifiers: dict[str, str] = Field(
        default_factory=dict,
        description="Every other qualifier slot set, e.g. "
        "{'object_aspect_qualifier': 'expression'}",
    )
    negated: bool = False

    @field_validator("predicate")
    @classmethod
    def _biolink_predicate(cls, v: str) -> str:
        return _require_prefix(v, "biolink:")

    @field_validator("qualified_predicate")
    @classmethod
    def _biolink_qualified_predicate(cls, v: str | None) -> str | None:
        return None if v is None else _require_prefix(v, "biolink:")

    @field_validator("qualifiers")
    @classmethod
    def _qualifier_slots(cls, v: dict[str, str]) -> dict[str, str]:
        for slot in v:
            if not slot.endswith("_qualifier"):
                raise ValueError(f"expected a '*_qualifier' slot, got {slot!r}")
        return v

    @property
    def id(self) -> str:
        """The same for every edge that makes the statement: the first 16 hex digits of the
        SHA-256 of its canonical JSON, with `qualified_predicate` among the qualifiers, as the
        augmentation agent's `graphio.statement_id` computes it."""
        qualifiers = dict(self.qualifiers)
        if self.qualified_predicate is not None:
            qualifiers["qualified_predicate"] = self.qualified_predicate
        key = {
            "subject": self.subject,
            "predicate": self.predicate,
            "object": self.object,
            "qualifiers": qualifiers,
            "negated": self.negated,
        }
        canonical = json.dumps(key, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


class ClaimContext(BaseModel):
    """Where a claim holds. Species and anatomy are grounded to CURIEs and become the claim's
    context qualifiers; the notes are free text and never do."""

    species: str | None = Field(
        default=None,
        description="NCBITaxon CURIE of the organism studied: the claim's organism and its "
        "species_context_qualifier",
    )
    anatomy: str | None = Field(
        default=None, description="UBERON CURIE: the claim's anatomical_context_qualifier"
    )
    anatomy_name: str | None = Field(
        default=None, description="The anatomy as the abstract names it, grounded or not"
    )
    notes: str | None = Field(
        default=None,
        description="Context Biolink can't express: population, cell line, dose, formulation, "
        "endpoint, stage, comparator, time point",
    )

    @field_validator("species")
    @classmethod
    def _ncbitaxon(cls, v: str | None) -> str | None:
        return None if v is None else _require_prefix(v, "NCBITaxon:")

    @field_validator("anatomy")
    @classmethod
    def _uberon(cls, v: str | None) -> str | None:
        return None if v is None else _require_prefix(v, "UBERON:")


class ClaimRecord(BaseModel):
    """One finding an abstract states about a node pair, with its checks and review. A
    negative claim denies its `statement`, which is itself never negated. A pair's claims are
    judged against every edge between the pair; `supports` is set where a claim is listed on an
    edge."""

    polarity: Polarity
    statement: Statement
    also_supports: list[Statement] = Field(
        default_factory=list,
        description="Further positive statements the same finding shows, e.g. "
        "in_clinical_trials_for from a null trial",
    )
    claim_role: ClaimRole
    hedged: bool = Field(description="Worded as a possibility: 'may', 'suggests', 'could'")
    negative_finding_type: NegativeFindingType | None = Field(
        default=None,
        description="Negative claims only: whether the study showed there is no effect or "
        "failed to detect one",
    )
    context: ClaimContext = Field(default_factory=ClaimContext)
    evidence_sentence: str = Field(description="Verbatim from the abstract")
    evidence_sentence_valid: bool = Field(
        description="Whether the sentence was found in the abstract (after repair, if it "
        "needed one)"
    )
    evidence_sentence_repaired: bool = Field(
        default=False, description="The sentence is the quote repair's, not the extractor's"
    )
    review: ReviewVerdict | None = Field(default=None, description="None if not reviewed")
    review_reason: str | None = None
    answered_by: str | None = Field(
        default=None, description="The model that extracted it, a fallback's name if one did"
    )
    supports: bool | None = Field(
        default=None,
        description="Whether it counts as support for the edge it's listed on. On an original, it "
        "supports the edge. On a proposed edge, it makes the edge's statement. None off an edge",
    )

    @model_validator(mode="after")
    def _polarity_rules(self) -> ClaimRecord:
        if any(s.negated for s in (self.statement, *self.also_supports)):
            raise ValueError("a claim's statements aren't negated; its polarity is")
        if self.polarity == "positive" and self.negative_finding_type is not None:
            raise ValueError("negative_finding_type is for negative claims only")
        return self


class PublicationEvidence(BaseModel):
    """One abstract read for an edge's node pair, with the claims found in it; none if it
    states nothing about the pair. Every abstract read is listed, with what PubMed says about
    the paper, so it can be judged again without fetching it.
    `supporting_document_type` and `supporting_document_year` take their names from Biolink's
    `TextMiningStudyResult`."""

    publication: str = Field(description="e.g. 'PMID:12345'")
    publication_types: list[str] = Field(
        description="PubMed's own, verbatim, e.g. ['Journal Article', 'Review']"
    )
    supporting_document_type: ArticleType = Field(
        description="The article type, from PubMed's publication types"
    )
    supporting_document_year: int | None = Field(default=None, description="Publication year")
    retracted: bool = Field(
        default=False,
        description="A Retracted Publication in PubMed; its claims count for nothing",
    )
    found_by: list[Origin] = Field(
        description="How the agent found it: in this edge's publications as its source cited "
        "them (`source`; for a proposed edge, its originals'), by its PubMed search (`search`) or "
        "by a PubTator search (`pubtator`); empty if only another edge of the pair cites it"
    )
    study_design: StudyDesign = Field(
        description="PubMed's design where its types name one, else the extractor's"
    )
    study_design_source: Literal["pubmed", "extractor"] = Field(
        description="pubmed where its publication types name a design, else extractor"
    )
    extractor_study_design: StudyDesign | None = Field(
        default=None, description="The extractor's own answer, kept when PubMed's overrode it"
    )
    claims: list[ClaimRecord] = Field(default_factory=list)


class AugmentedEdge(Edge):
    """An edge with the augmentation agent's findings from the literature: an input edge it
    checked (`checked`), with every slot it had, or an edge it proposes (`proposed`) from the
    originals in `original_edge_ids`, whose statement the literature makes more specific than
    theirs. Originals stay in the graph, and name the edges that refine them in
    `lit_refined_by`. The `lit_` prefix keeps the agent's fields clear of Biolink slots."""

    original_edge_ids: list[str] = Field(
        description="A checked edge's own id; on a proposed edge, the originals it refines"
    )
    augmentation: Literal["checked", "proposed"]
    supporting_data_source: list[str] | None = Field(
        default=None,
        description="infores CURIEs of the data its evidence came from, multivalued as in "
        "Biolink: ['infores:pubmed'] on a proposed edge. A bare CURIE is read as a list of one",
    )
    lit_support_score: int = Field(
        ge=0,
        description="The number of distinct PMIDs whose counting claims support it (an "
        "original) or make it (a proposed edge). 0 for an unsupported original",
    )
    lit_supporting_publications: list[str] = Field(
        default_factory=list, description="PMIDs of the claims that support it, e.g. 'PMID:1'"
    )
    lit_added_publications: list[str] = Field(
        default_factory=list,
        description="On an original: the supporting PMIDs it didn't cite, now appended to its "
        "`publications`",
    )
    lit_refined_by: list[str] = Field(
        default_factory=list,
        description="On an original: the edges more specific than it that its supporting claims "
        "make. These are proposed edges, and edges of the graph that already make such a "
        "statement",
    )
    lit_context_notes: list[str] = Field(
        default_factory=list,
        description="Contexts Biolink can't express, such as cell line or dose",
    )
    lit_evidence: list[PublicationEvidence] = Field(
        description="Every abstract read for its node pair, each claim with whether it supports "
        "this edge"
    )

    @field_validator("supporting_data_source", mode="before")
    @classmethod
    def _one_supporting(cls, v: object) -> object:
        return [v] if isinstance(v, str) else v

    @field_validator("supporting_data_source")
    @classmethod
    def _infores_supporting(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else [_require_prefix(s, "infores:") for s in v]


class Graph(BaseModel):
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)


class ColumnRef(BaseModel):
    """A column in a source file, as its Croissant metadata names it. Field `@id`s are the same
    in the original and the semantic croissant."""

    field: str = Field(description="The cr:Field @id")
    name: str = Field(description="Column name in the file: the field's source.extract.column")


class Mapping(BaseModel):
    """One rule the transformation agent applied: these columns of this file became these
    nodes or edges."""

    id: str = Field(description="Unique within the report, e.g. 'tcrd.cmpd_activity.interacts'")
    file: str = Field(description="The cr:FileObject or cr:FileSet @id the rows came from")
    record_set: str | None = Field(default=None, description="The cr:RecordSet @id, if any")
    plan_layout: str | None = Field(
        default=None, description="The record set's rcv:planLayout in the semantic croissant"
    )
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
        default_factory=list,
        description="Rows dropped because they can't make a valid record, in words, "
        "e.g. 'target_id is empty'",
    )
    flags: list[str] = Field(
        default_factory=list,
        description="Possible improvements to the semantic croissant, for the metadata team. "
        "The mapping still follows the semantic croissant as written.",
    )
    code: str | None = Field(default=None, description="Path of the transform code that ran it")
    rows_read: int | None = None
    records_written: int | None = None


class Skipped(BaseModel):
    """A file, record set or column deliberately not used."""

    ref: str = Field(description="@id of the cr:FileObject, cr:FileSet, cr:RecordSet or cr:Field")
    name: str | None = None
    reason: str


class TransformReport(BaseModel):
    """What the transformation agent read to build its graph: which columns of which files
    became which nodes and edges. Written next to the graph, one per source."""

    source: str = Field(description="infores CURIE of the source, e.g. 'infores:tcrd'")
    source_version: str | None = None
    croissant: str = Field(description="Path or URL of the source's RoboCroissant metadata")
    semantic_croissant: str | None = Field(
        default=None, description="Path or URL of its croissant.semantic.json"
    )
    mappings: list[Mapping] = Field(default_factory=list)
    skipped: list[Skipped] = Field(default_factory=list)
