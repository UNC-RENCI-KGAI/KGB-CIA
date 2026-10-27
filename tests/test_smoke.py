import json

import pytest
from pydantic import ValidationError

from shared.pipeline import STAGES, main
from shared.schema import ColumnRef, Edge, Graph, Mapping, Node, TransformReport


def test_dry_run_lists_every_stage(capsys):
    assert main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    for name, _ in STAGES:
        assert f"[{name}]" in out


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
        croissant="data/tcrd/croissant.semantic.json",
        mappings=[
            Mapping(
                id="tcrd.cmpd_activity.interacts",
                file="cmpd_activity.tsv",
                record_set="record_cmpd_activity",
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
        unused_columns=[ColumnRef(field="field_d4", name="nhash")],
    )
    assert TransformReport.model_validate_json(report.model_dump_json()) == report
