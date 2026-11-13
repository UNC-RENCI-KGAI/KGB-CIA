"""Statements and claims as English sentences, in Biolink's reading order."""

from __future__ import annotations

import pytest
from pubmed_evidence import biolink, graphio, records, sentences
from toy import ABL1, CML, IMATINIB, LIVER, TP53

from shared import schema

NAMES = {IMATINIB: "imatinib", ABL1: "ABL1", CML: "chronic myeloid leukemia"}


def statement(subject: str, predicate: str, object: str, **qualifiers) -> records.SelectedStatement:
    return records.SelectedStatement(
        statement_id=graphio.statement_id(subject, predicate, object, qualifiers),
        pair_id=graphio.pair_id(subject, object),
        subject=subject,
        predicate=predicate,
        object=object,
        qualifiers=qualifiers,
        rule="test",
        edge_count=1,
    )


def test_readme_example():
    s = statement(
        IMATINIB,
        "biolink:affects",
        ABL1,
        qualified_predicate="biolink:causes",
        object_aspect_qualifier="expression",
        object_direction_qualifier="increased",
    )
    assert sentences.sentence(s, NAMES) == "imatinib causes increased expression of ABL1"


def test_treats():
    s = statement(IMATINIB, "biolink:treats", CML)
    assert sentences.sentence(s, NAMES) == "imatinib treats chronic myeloid leukemia"
    s = statement(IMATINIB, "biolink:treats_or_applied_or_studied_to_treat", CML)
    assert sentences.sentence(s, NAMES) == (
        "imatinib is used or studied to treat chronic myeloid leukemia"
    )


def test_every_in_scope_predicate_has_a_phrase():
    for name in (
        "treats_or_applied_or_studied_to_treat",
        "treats",
        "studied_to_treat",
        "in_clinical_trials_for",
        "in_preclinical_trials_for",
        "beneficial_in_models_for",
        "applied_to_treat",
        "ameliorates_condition",
        "preventative_for_condition",
        "contraindicated_in",
        "affects",
        "directly_physically_interacts_with",
        "binds",
        "physically_interacts_with",
        "interacts_with",
        "gene_associated_with_condition",
        "genetically_associated_with",
        "causes",
        "contributes_to",
        "has_phenotype",
    ):
        assert f"biolink:{name}" in sentences.PREDICATE_PHRASES


def test_predicate_fallback_uses_the_biolink_name(monkeypatch):
    monkeypatch.setattr(biolink, "element_name", lambda curie: "has adverse event")
    s = statement(IMATINIB, "biolink:has_adverse_event_made_up", CML)
    assert sentences.sentence(s, NAMES) == ("imatinib has adverse event chronic myeloid leukemia")


def test_subject_aspect_and_direction():
    # Biolink's example: "Methionine deficiency results in increased expression of ADRB2".
    s = statement(
        IMATINIB,
        "biolink:affects",
        ABL1,
        subject_aspect_qualifier="abundance",
        subject_direction_qualifier="decreased",
        qualified_predicate="biolink:causes",
        object_aspect_qualifier="expression",
        object_direction_qualifier="increased",
    )
    assert sentences.sentence(s, NAMES) == (
        "decreased abundance of imatinib causes increased expression of ABL1"
    )


def test_object_part_and_form_or_variant():
    # Biolink's example: "increased methylation of a mutant form of the CDKN2A promoter".
    s = statement(
        IMATINIB,
        "biolink:affects",
        ABL1,
        qualified_predicate="biolink:causes",
        object_part_qualifier="promoter",
        object_form_or_variant_qualifier="mutant_form",
        object_aspect_qualifier="methylation",
        object_direction_qualifier="increased",
    )
    assert sentences.sentence(s, NAMES) == (
        "imatinib causes increased methylation of a mutant form of the promoter of ABL1"
    )


def test_aspect_without_direction_and_enum_underscores():
    s = statement(
        IMATINIB, "biolink:affects", ABL1, object_aspect_qualifier="activity_or_abundance"
    )
    assert sentences.sentence(s, NAMES) == "imatinib affects the activity or abundance of ABL1"


def test_anatomical_and_species_context():
    s = statement(
        IMATINIB,
        "biolink:affects",
        ABL1,
        qualified_predicate="biolink:causes",
        object_aspect_qualifier="activity",
        object_direction_qualifier="decreased",
        causal_mechanism_qualifier="inhibition",
        anatomical_context_qualifier=LIVER,
        species_context_qualifier="NCBITaxon:10090",
    )
    assert sentences.sentence(s, NAMES) == (
        "imatinib causes decreased activity of ABL1 through inhibition "
        f"in {LIVER} in NCBITaxon:10090"
    )


def test_multivalued_and_unknown_qualifiers():
    s = statement(
        IMATINIB,
        "biolink:affects",
        ABL1,
        anatomical_context_qualifier=[LIVER, "UBERON:0000178"],
        catalyst_qualifier="NCBIGene:1",
    )
    assert sentences.sentence(s, NAMES) == (
        f"imatinib affects ABL1 in {LIVER} and UBERON:0000178 (catalyst qualifier: NCBIGene:1)"
    )


def test_names_fall_back_to_ids():
    s = statement(TP53, "biolink:has_phenotype", "HP:0001250")
    assert sentences.sentence(s, {}) == "NCBIGene:7157 has the phenotype HP:0001250"


def test_shared_schema_statements_read_their_qualified_predicate():
    s = schema.Statement(
        subject=IMATINIB,
        predicate="biolink:affects",
        object=ABL1,
        qualified_predicate="biolink:causes",
        qualifiers={
            "object_aspect_qualifier": "activity",
            "object_direction_qualifier": "decreased",
        },
    )
    assert sentences.sentence(s, NAMES) == "imatinib causes decreased activity of ABL1"
    assert sentences.sentence(s.model_copy(update={"negated": True}), NAMES) == (
        "imatinib does not cause decreased activity of ABL1"
    )


@pytest.mark.parametrize(
    "predicate, expected",
    [
        ("biolink:treats", "imatinib does not treat chronic myeloid leukemia"),
        (
            "biolink:treats_or_applied_or_studied_to_treat",
            "imatinib is not used or studied to treat chronic myeloid leukemia",
        ),
        (
            "biolink:in_clinical_trials_for",
            "imatinib has not been tested in clinical trials for chronic myeloid leukemia",
        ),
        (
            "biolink:directly_physically_interacts_with",
            "imatinib does not directly physically interact with chronic myeloid leukemia",
        ),
        ("biolink:has_phenotype", "imatinib does not have the phenotype chronic myeloid leukemia"),
        (
            "biolink:predisposes_to_condition",
            "imatinib does not predispose to chronic myeloid leukemia",
        ),
    ],
)
def test_negated_statements(predicate, expected):
    s = schema.Statement(subject=IMATINIB, predicate=predicate, object=CML, negated=True)
    assert sentences.sentence(s, NAMES) == expected


def test_a_negated_phrase_without_a_verb_negates_the_whole_reading(monkeypatch):
    monkeypatch.setattr(biolink, "element_name", lambda curie: "gene associated with condition")
    s = schema.Statement(subject=ABL1, predicate="biolink:made_up", object=CML, negated=True)
    assert sentences.sentence(s, NAMES) == (
        "it is not the case that ABL1 gene associated with condition chronic myeloid leukemia"
    )


def test_claim_sentence_negates_a_negative_claim():
    claim = {
        "polarity": "negative",
        "statement": {
            "subject": IMATINIB,
            "predicate": "biolink:treats",
            "object": CML,
            "qualified_predicate": None,
            "qualifiers": {},
        },
    }
    assert sentences.claim_sentence(claim, NAMES) == (
        "imatinib does not treat chronic myeloid leukemia"
    )
    claim["polarity"] = "positive"
    assert sentences.claim_sentence(claim, NAMES) == "imatinib treats chronic myeloid leukemia"
