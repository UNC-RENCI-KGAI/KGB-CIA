import json

import pytest
from pydantic import ValidationError

from shared.pipeline import STAGES, main
from shared.schema import (
    AugmentedEdge,
    ClaimContext,
    ClaimRecord,
    ColumnRef,
    Edge,
    Graph,
    Mapping,
    Node,
    PublicationEvidence,
    Skipped,
    Statement,
    TransformReport,
)


def test_dry_run_lists_every_stage(capsys):
    assert main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    for name, _, _ in STAGES:
        assert f"[{name}]" in out


def test_stage_order():
    names = [name for name, _, _ in STAGES]
    assert names.index("cleaning") < names.index("filter") < names.index("augmentation")
    assert (
        names.index("augmentation") < names.index("final_cleaning") < names.index("mcp_interface")
    )
    # the MCP serves the final pass's output; on its own kgb-mcp would pick data/cleaning
    stage_args = {name: args for name, _, args in STAGES}
    assert "data/final" in stage_args["final_cleaning"]
    assert stage_args["mcp_interface"] == ["--input", "data/final"]


def _edge(**kw):
    fields = dict(
        subject="CHEBI:45783",
        predicate="biolink:directly_physically_interacts_with",
        object="NCBIGene:1017",
        primary_knowledge_source="infores:chembl",
        aggregator_knowledge_source=["infores:tcrd"],
        knowledge_level="knowledge_assertion",
        agent_type="manual_agent",
    )
    return Edge(**{**fields, **kw})


def test_schema_round_trip():
    g = Graph(
        nodes=[
            Node(
                id="NCBIGene:1017",
                category=["biolink:Gene"],
                name="CDK2",
                equivalent_identifiers=["NCBIGene:1017", "HGNC:1771"],
                in_taxon=["NCBITaxon:9606"],
            )
        ],
        edges=[_edge(publications=["PMID:12345"], affinity=7.2, affinity_parameter="pKi")],
    )
    assert Graph.model_validate_json(g.model_dump_json()) == g


STATEMENT = dict(
    subject="CHEBI:45783",
    predicate="biolink:affects",
    object="NCBIGene:1017",
    qualified_predicate="biolink:causes",
    qualifiers={"object_aspect_qualifier": "activity", "object_direction_qualifier": "decreased"},
)


def _claim(**kw):
    fields = dict(
        polarity="positive",
        statement=Statement(**STATEMENT),
        claim_role="primary_finding",
        hedged=False,
        evidence_sentence="Imatinib inhibited CDK2 activity.",
        evidence_sentence_valid=True,
        answered_by="gpt-6-luna",
    )
    return ClaimRecord(**{**fields, **kw})


def _evidence(**kw):
    fields = dict(
        publication="PMID:12345",
        publication_types=["Journal Article"],
        supporting_document_type="research",
        found_by=["search"],
        study_design="in_vitro",
        study_design_source="extractor",
        extractor_study_design="in_vitro",
    )
    return PublicationEvidence(**{**fields, **kw})


def test_augmented_edge_round_trip():
    binds = Statement(
        subject="CHEBI:45783",
        predicate="biolink:directly_physically_interacts_with",
        object="NCBIGene:1017",
    )
    negative = _claim(
        polarity="negative",
        statement=binds,
        also_supports=[binds.model_copy(update={"predicate": "biolink:interacts_with"})],
        claim_role="background",
        hedged=True,
        negative_finding_type="failed_to_detect",
        context=ClaimContext(
            species="NCBITaxon:9606",
            anatomy="UBERON:0002107",
            anatomy_name="hepatocytes",
            notes="at 10 uM",
        ),
        evidence_sentence_repaired=True,
        review="upheld",
        review_reason="The abstract says no binding was detected.",
        supports=False,
    )
    evidence = [
        _evidence(
            supporting_document_year=2001,
            claims=[_claim(supports=True), negative],
        ),
        _evidence(
            publication="PMID:67890",
            publication_types=["Journal Article", "Review"],
            supporting_document_type="review",
            study_design="narrative_review",
            study_design_source="pubmed",
            extractor_study_design="cohort",
            found_by=["source", "search"],
        ),
    ]
    edge = AugmentedEdge(
        **_edge(
            id="kgbcia-edge:0123456789abcdef",
            affinity=7.2,
            publications=["PMID:1", "PMID:12345"],
        ).model_dump(),
        original_edge_ids=["kgbcia-edge:0123456789abcdef"],
        augmentation="checked",
        lit_support_score=1,
        lit_supporting_publications=["PMID:12345"],
        lit_added_publications=["PMID:12345"],
        lit_refined_by=["kgbcia-augmentation:0123456789abcdef"],
        lit_context_notes=["at 10 uM"],
        lit_evidence=evidence,
    )
    line = edge.model_dump_json(exclude_none=True)
    assert AugmentedEdge.model_validate_json(line) == edge
    # Still one flat KGX line: a stage that reads Edge gets the evidence as extra slots.
    plain = Edge.model_validate_json(line)
    assert plain.affinity == 7.2 and plain.lit_support_score == 1
    assert plain.publications == ["PMID:1", "PMID:12345"]
    assert plain.lit_added_publications == ["PMID:12345"]
    assert plain.lit_refined_by == ["kgbcia-augmentation:0123456789abcdef"]
    claim = plain.lit_evidence[0]["claims"][1]
    assert (claim["polarity"], claim["supports"]) == ("negative", False)
    assert plain.lit_evidence[0]["claims"][0]["supports"] is True
    assert claim["statement"]["predicate"] == "biolink:directly_physically_interacts_with"
    assert claim["context"]["anatomy"] == "UBERON:0002107"
    assert plain.lit_evidence[1]["claims"] == []


def test_proposed_edge_names_its_originals():
    edge = AugmentedEdge(
        **_edge(
            id="kgbcia-augmentation:0123456789abcdef",
            predicate="biolink:affects",
            primary_knowledge_source="infores:kgbcia-augmentor",
            aggregator_knowledge_source=[],
            agent_type="text_mining_agent",
            publications=["PMID:12345"],
            qualified_predicate="biolink:causes",
            object_aspect_qualifier="activity",
        ).model_dump(),
        original_edge_ids=["kgbcia-edge:0123456789abcdef"],
        augmentation="proposed",
        supporting_data_source=["infores:pubmed"],
        lit_support_score=1,
        lit_supporting_publications=["PMID:12345"],
        lit_evidence=[_evidence(claims=[_claim(supports=True)])],
    )
    line = json.loads(edge.model_dump_json(exclude_none=True))
    assert line["supporting_data_source"] == ["infores:pubmed"]
    assert line["object_aspect_qualifier"] == "activity"
    assert line["lit_added_publications"] == line["lit_refined_by"] == []


def test_supporting_data_source_is_multivalued_as_in_biolink():
    """Biolink 4.4.6 makes `supporting_data_source` multivalued, so a source's edge may carry a
    list; a bare CURIE, as older inputs have it, is read as a list of one."""
    fields = dict(
        original_edge_ids=["kgbcia-edge:0123456789abcdef"],
        augmentation="checked",
        lit_support_score=0,
        lit_evidence=[],
    )
    sources = ["infores:drugcentral", "infores:pubmed"]
    edge = AugmentedEdge(**_edge().model_dump(), **fields, supporting_data_source=sources)
    assert edge.supporting_data_source == sources
    bare = AugmentedEdge(**_edge().model_dump(), **fields, supporting_data_source="infores:x")
    assert bare.supporting_data_source == ["infores:x"]


def test_statement_id_is_canonical():
    s = Statement(**STATEMENT)
    reordered = s.model_copy(update={"qualifiers": dict(reversed(s.qualifiers.items()))})
    assert s.id == reordered.id and len(s.id) == 16
    assert s.model_copy(update={"negated": True}).id != s.id
    assert s.model_copy(update={"qualified_predicate": None}).id != s.id


@pytest.mark.parametrize(
    "bad, claim",
    [
        ({"study_design": "anecdote"}, {}),
        ({"study_design_source": "model"}, {}),
        ({"supporting_document_type": "preprint"}, {}),
        ({"found_by": ["llm"]}, {}),
        ({}, {"polarity": "contradicted"}),
        ({}, {"claim_role": "experimental_result"}),
        ({}, {"review": "partial"}),
        ({}, {"supports": "partially"}),
        ({}, {"negative_finding_type": "failed_to_detect"}),  # on a positive claim
        ({}, {"statement": {**STATEMENT, "negated": True}}),
        ({}, {"statement": {**STATEMENT, "predicate": "affects"}}),
        ({}, {"statement": {**STATEMENT, "qualifiers": {"qualified_predicate": "causes"}}}),
        ({}, {"context": {"anatomy": "liver"}}),
        ({}, {"context": {"species": "human"}}),
    ],
)
def test_publication_evidence_rejects_unknown_values(bad, claim):
    claims = [{**_claim().model_dump(), **claim}]
    with pytest.raises(ValidationError):
        _evidence(claims=claims, **bad)


@pytest.mark.parametrize(
    "bad",
    [
        {"augmentation": "qualified"},
        {"lit_support_score": -1},
        {"lit_support_score": 0.5},
        {"supporting_data_source": "pubmed"},
        {"supporting_data_source": ["infores:pubmed", "pubmed"]},
    ],
)
def test_augmented_edge_rejects_unknown_values(bad):
    fields = dict(
        original_edge_ids=["kgbcia-edge:0123456789abcdef"],
        augmentation="checked",
        lit_support_score=0,
        lit_evidence=[],
    )
    AugmentedEdge(**_edge().model_dump(), **fields)
    with pytest.raises(ValidationError):
        AugmentedEdge(**_edge().model_dump(), **{**fields, **bad})


def test_edge_is_flat_kgx():
    line = json.loads(_edge(affinity=7.2).model_dump_json(exclude_none=True))
    assert line["primary_knowledge_source"] == "infores:chembl"
    assert line["affinity"] == 7.2
    assert "provenance" not in line


@pytest.mark.parametrize(
    "bad",
    [
        {"primary_knowledge_source": "ChEMBL"},
        {"aggregator_knowledge_source": ["TCRD"]},
        {"predicate": "binds"},
        {"knowledge_level": "curated"},
        {"agent_type": None},
    ],
)
def test_edge_rejects_non_biolink(bad):
    with pytest.raises(ValidationError):
        _edge(**bad)


def test_transform_report_round_trip():
    report = TransformReport(
        source="infores:tcrd",
        source_version="6.13.4",
        croissant="data/tcrd/croissant.json",
        semantic_croissant="data/tcrd/croissant.semantic.json",
        mappings=[
            Mapping(
                id="tcrd.cmpd_activity.interacts",
                file="cmpd_activity.tsv",
                record_set="record_cmpd_activity",
                plan_layout="compound_activity",
                produces="edge",
                subject_category="biolink:SmallMolecule",
                predicate="biolink:directly_physically_interacts_with",
                object_category="biolink:Protein",
                columns={
                    "subject": [ColumnRef(field="field_a1", name="cmpd_id_in_src")],
                    "object": [ColumnRef(field="field_b2", name="target_id")],
                    "publications": [ColumnRef(field="field_c3", name="pubmed_ids")],
                },
                constants={"knowledge_level": "knowledge_assertion"},
                rows_read=10,
                records_written=8,
            )
        ],
        skipped=[Skipped(ref="field_d4", name="nhash", reason="internal hash, no meaning")],
    )
    assert TransformReport.model_validate_json(report.model_dump_json()) == report
