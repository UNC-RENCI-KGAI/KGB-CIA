"""Biolink lookups: ancestry, association classes and qualifiers, all offline."""

from __future__ import annotations

import urllib.request

import pytest
import requests
from pubmed_evidence import biolink

CHEMICAL = ["biolink:SmallMolecule", "biolink:MolecularEntity", "biolink:ChemicalEntity"]
GENE = ["biolink:Gene"]
PROTEIN = ["biolink:Protein"]
DISEASE = ["biolink:Disease"]
PHENOTYPE = ["biolink:PhenotypicFeature"]

# Classes bmt's get_associations returns for these edges although they don't fit them.
WRONG = {
    "biolink:Association",
    "biolink:FunctionalAssociation",
    "biolink:GeneToGoTermAssociation",
    "biolink:ReactionToCatalystAssociation",
    "biolink:NamedThingAssociatedWithLikelihoodOfNamedThingAssociation",
    "biolink:EntityToDiseaseAssociation",
}


def test_toolkit_loads_bundled_model_without_network(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("bmt tried to use the network")

    monkeypatch.setattr(requests.Session, "request", no_network)
    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    fresh = biolink._toolkit.__wrapped__()
    assert fresh.get_model_version() == biolink.BIOLINK_VERSION
    assert biolink.toolkit() is biolink.toolkit()


def test_descendants_include_mixins_and_self():
    genes = biolink.descendants("biolink:GeneOrGeneProduct")
    assert {"biolink:GeneOrGeneProduct", "biolink:Gene", "biolink:Protein"} <= genes
    assert "biolink:Drug" in biolink.descendants("biolink:ChemicalEntity")
    treats = biolink.descendants("biolink:treats_or_applied_or_studied_to_treat")
    assert {"biolink:treats", "biolink:in_clinical_trials_for"} <= treats
    assert "biolink:Disease" not in genes


def test_is_a():
    assert biolink.is_a("biolink:Protein", "biolink:GeneOrGeneProduct")
    assert biolink.is_a(["biolink:Disease", "biolink:Drug"], ["biolink:ChemicalEntity"])
    assert biolink.is_a("biolink:causes", ["biolink:affects", "biolink:contributes_to"])
    assert biolink.is_a("biolink:binds", "biolink:directly_physically_interacts_with")
    assert not biolink.is_a("biolink:Disease", "biolink:GeneOrGeneProduct")
    assert not biolink.is_a("biolink:interacts_with", "biolink:physically_interacts_with")
    assert biolink.is_a("biolink:NotInBiolink", "biolink:NotInBiolink")
    assert not biolink.is_a("biolink:NotInBiolink", "biolink:NamedThing")


def test_is_symmetric():
    assert biolink.is_symmetric("biolink:interacts_with")
    assert biolink.is_symmetric("biolink:binds")
    assert not biolink.is_symmetric("biolink:affects")
    assert not biolink.is_symmetric("biolink:gene_associated_with_condition")


def test_inverse_either_side_and_symmetric():
    assert biolink.inverse("biolink:treats") == "biolink:treated_by"
    assert biolink.inverse("biolink:treated_by") == "biolink:treats"
    assert biolink.inverse("biolink:binds") == "biolink:binds"  # symmetric: itself
    assert biolink.inverse("biolink:interacts_with") == "biolink:interacts_with"
    assert biolink.inverse("biolink:not_in_biolink") is None
    assert biolink.is_canonical("biolink:gene_associated_with_condition")
    assert not biolink.is_canonical("biolink:condition_associated_with_gene")


def test_opposite_predicates_come_from_biolink_both_ways():
    opposites = biolink.opposites()
    in_scope = [
        ("contraindicated_in", "treats"),
        ("ameliorates_condition", "exacerbates_condition"),
        ("preventative_for_condition", "promotes_condition"),
        ("positively_correlated_with", "negatively_correlated_with"),
        ("has_increased_amount", "has_decreased_amount"),
        ("increases_sensitivity_to", "decreases_sensitivity_to"),
        ("disrupts", "enables"),
    ]
    for a, b in in_scope:
        assert (f"biolink:{a}", f"biolink:{b}") in opposites
        assert (f"biolink:{b}", f"biolink:{a}") in opposites
    assert all((b, a) in opposites for a, b in opposites)
    # Only predicates: interbase coordinates and `prevented by` are slots, not predicates.
    assert not any("interbase" in a or "prevented_by" in a for pair in opposites for a in pair)


def test_fits_by_domain_and_range_with_mixins():
    # treats' domain is the mixin `chemical or drug or treatment`, which a small molecule has.
    assert biolink.fits(CHEMICAL, "biolink:treats", DISEASE)
    assert not biolink.fits(DISEASE, "biolink:treats", CHEMICAL)
    # A broad mixin fits a narrower class it overlaps: a rule's GeneOrGeneProduct, gene's domain.
    rule_gene = ["biolink:GeneOrGeneProduct"]
    assert biolink.fits(rule_gene, "biolink:gene_associated_with_condition", DISEASE)
    assert not biolink.fits(rule_gene, "biolink:has_adverse_event", DISEASE)
    assert not biolink.fits(CHEMICAL, "biolink:has_adverse_event", rule_gene)
    # An ancestor in a node's category list doesn't make it fit (named thing would fit all).
    assert not biolink.fits(["biolink:NamedThing", *DISEASE], "biolink:biomarker_for", GENE)
    assert biolink.most_specific(["biolink:NamedThing", *PHENOTYPE]) == (
        "biolink:PhenotypicFeature",
    )
    # No domain or range, or no known categories, constrains nothing.
    assert biolink.fits(DISEASE, "biolink:affects", GENE)
    assert biolink.fits([], "biolink:treats", ["biolink:NotInBiolink"])


@pytest.mark.parametrize(
    "subject, predicate, object, expected",
    [
        (CHEMICAL, "treats", DISEASE, ("treats", False)),
        # Defined the other way round: its inverse, the same way round.
        (
            GENE,
            "condition_associated_with_gene",
            DISEASE,
            ("gene_associated_with_condition", False),
        ),
        (GENE, "has_biomarker", DISEASE, ("biomarker_for", False)),
        # From the other node: turned round to the canonical predicate.
        (DISEASE, "gene_associated_with_condition", GENE, ("gene_associated_with_condition", True)),
        (DISEASE, "condition_associated_with_gene", GENE, ("gene_associated_with_condition", True)),
        (DISEASE, "treats", CHEMICAL, ("treats", True)),
        (PHENOTYPE, "has_phenotype", GENE, ("has_phenotype", True)),
        # No domain or range either way: the canonical one of the pair.
        (
            GENE,
            "increased_likelihood_associated_with",
            DISEASE,
            ("associated_with_increased_likelihood_of", True),
        ),
        (GENE, "affects", CHEMICAL, ("affects", False)),
        # Neither way.
        (CHEMICAL, "has_adverse_event", GENE, None),
        (CHEMICAL, "enables", GENE, None),
        # Symmetric: its own inverse, either way round as written, or neither way.
        (GENE, "binds", CHEMICAL, ("binds", False)),
        (CHEMICAL, "binds", GENE, ("binds", False)),
        (GENE, "genetically_interacts_with", GENE, ("genetically_interacts_with", False)),
        (CHEMICAL, "genetically_interacts_with", GENE, None),
    ],
)
def test_oriented(subject, predicate, object, expected):
    read = biolink.oriented(subject, f"biolink:{predicate}", object)
    assert read == (None if expected is None else (f"biolink:{expected[0]}", expected[1]))


def test_element_name():
    name = biolink.element_name("biolink:treats_or_applied_or_studied_to_treat")
    assert name == "treats or applied or studied to treat"
    assert biolink.element_name("biolink:SmallMolecule") == "small molecule"
    assert biolink.element_name("biolink:not_in_biolink") == "not in biolink"


# Every in-scope triple of DESIGN.md's step 1 table, with the classes that fit it.
CHEM_TO_DISEASE = "biolink:ChemicalOrDrugOrTreatmentToDiseaseOrPhenotypicFeatureAssociation"
CHEM_AFFECTS = [
    "biolink:ChemicalAffectsGeneAssociation",
    "biolink:ChemicalAffectsBiologicalEntityAssociation",
]
CHEM_GENE = ["biolink:ChemicalGeneInteractionAssociation"]
GENE_DISEASE = ["biolink:GeneToDiseaseAssociation"]
GENE_CAUSES = ["biolink:GeneHasVariantThatContributesToDiseaseAssociation", *GENE_DISEASE]
GENE_GENE = ["biolink:PairwiseGeneToGeneInteraction"]
IN_SCOPE = [
    (CHEMICAL, "treats", DISEASE, [CHEM_TO_DISEASE]),
    (["biolink:Drug"], "treats_or_applied_or_studied_to_treat", DISEASE, [CHEM_TO_DISEASE]),
    (CHEMICAL, "affects", GENE, CHEM_AFFECTS),
    (CHEMICAL, "affects", PROTEIN, CHEM_AFFECTS),
    (CHEMICAL, "directly_physically_interacts_with", GENE, CHEM_GENE),
    (CHEMICAL, "binds", PROTEIN, CHEM_GENE),
    (GENE, "gene_associated_with_condition", DISEASE, GENE_DISEASE),
    (GENE, "genetically_associated_with", DISEASE, GENE_DISEASE),
    (PROTEIN, "gene_associated_with_condition", DISEASE, GENE_DISEASE),
    (GENE, "causes", DISEASE, GENE_CAUSES),
    (GENE, "contributes_to", DISEASE, GENE_CAUSES),
    (GENE, "physically_interacts_with", GENE, GENE_GENE),
    (PROTEIN, "interacts_with", GENE, GENE_GENE),
    (GENE, "has_phenotype", PHENOTYPE, ["biolink:GeneToPhenotypicFeatureAssociation"]),
]


@pytest.mark.parametrize(("subject", "predicate", "object", "expected"), IN_SCOPE)
def test_associations_fit_in_scope_triples(subject, predicate, object, expected):
    found = biolink.associations(subject, f"biolink:{predicate}", object)
    assert found == expected
    assert not WRONG & set(found)


def test_associations_with_full_category_lists():
    # KGX nodes often list every ancestor category; the answer must not change.
    tk = biolink.toolkit()
    chemical = tk.get_ancestors("biolink:SmallMolecule", formatted=True)
    gene = [*tk.get_ancestors("biolink:Gene", formatted=True), "biolink:Protein"]
    disease = tk.get_ancestors("biolink:Disease", formatted=True)
    assert biolink.associations(chemical, "biolink:affects", gene) == CHEM_AFFECTS
    assert biolink.associations(gene, "biolink:causes", disease) == GENE_CAUSES


def test_associations_without_a_constraining_class_fall_back_to_the_ranges():
    found = biolink.associations(CHEMICAL, "biolink:contraindicated_in", DISEASE)
    assert found == ["biolink:EntityToDiseaseAssociation"]


def test_associations_unknown_terms():
    assert biolink.associations(["biolink:NotAClass"], "biolink:affects", GENE) == []
    assert biolink.associations(CHEMICAL, "biolink:not_a_predicate", GENE) == []


def test_allowed_qualifiers_chemical_affects_gene():
    allowed = biolink.allowed_qualifiers(CHEMICAL, "biolink:affects", GENE)
    aspect = allowed["object_aspect_qualifier"]
    assert aspect.range == "GeneOrGeneProductOrChemicalEntityAspectEnum"
    assert {"activity", "expression", "abundance"} <= set(aspect.values)
    direction = allowed["object_direction_qualifier"]
    assert set(direction.values) == {"increased", "upregulated", "decreased", "downregulated"}
    assert allowed["qualified_predicate"].values == ("biolink:causes",)
    assert "inhibition" in allowed["causal_mechanism_qualifier"].values
    # Open ranges: an ontology class, no value list.
    assert allowed["species_context_qualifier"].values is None
    assert allowed["species_context_qualifier"].range == "biolink:OrganismTaxon"
    assert allowed["anatomical_context_qualifier"].range == "biolink:AnatomicalEntity"
    # Generic statement slots and free-text ranges stay out of the prompt.
    assert not {"qualifier", "statistical_significance_qualifier"} & set(allowed)
    assert all(spec.slot == slot for slot, spec in allowed.items())


def test_allowed_qualifiers_gene_disease():
    allowed = biolink.allowed_qualifiers(GENE, "biolink:gene_associated_with_condition", DISEASE)
    assert {
        "subject_aspect_qualifier",
        "subject_direction_qualifier",
        "subject_form_or_variant_qualifier",
        "object_direction_qualifier",
        "qualified_predicate",
        "disease_context_qualifier",
    } <= set(allowed)
    # GeneToDiseaseAssociation has no mechanism or part qualifiers, and its
    # anatomical_context_qualifier is free text.
    assert not {
        "causal_mechanism_qualifier",
        "object_part_qualifier",
        "anatomical_context_qualifier",
        "subject_specialization_qualifier",
        "association_basis_qualifier",
    } & set(allowed)
    assert allowed["qualified_predicate"].values == ("biolink:causes",)
    assert "loss_of_function_variant_form" in allowed["subject_form_or_variant_qualifier"].values


def test_validate_qualifiers_accepts_good_sets():
    good = {
        "qualified_predicate": "biolink:causes",
        "object_aspect_qualifier": "activity",
        "object_direction_qualifier": "decreased",
        "causal_mechanism_qualifier": "inhibition",
        "species_context_qualifier": "NCBITaxon:9606",
        "object_context_qualifier": "CL:0000236",
    }
    assert biolink.validate_qualifiers(good, CHEMICAL, "biolink:affects", GENE) == []
    assert biolink.validate_qualifiers({}, CHEMICAL, "biolink:affects", GENE) == []
    gene_disease = {
        "subject_aspect_qualifier": "expression",
        "subject_direction_qualifier": "increased",
        "qualified_predicate": "biolink:causes",
    }
    errors = biolink.validate_qualifiers(
        gene_disease, GENE, "biolink:gene_associated_with_condition", DISEASE
    )
    assert errors == []
    prefixed = {"biolink:object_direction_qualifier": "increased"}
    assert biolink.validate_qualifiers(prefixed, CHEMICAL, "biolink:affects", GENE) == []


@pytest.mark.parametrize(
    ("qualifiers", "problem"),
    [
        ({"object_direction_qualifier": "sideways"}, "not a permissible value"),
        ({"object_aspect_qualifier": "happiness"}, "not a permissible value"),
        ({"sex_qualifier": "PATO:0000383"}, "not allowed on"),
        ({"made_up_qualifier": "x"}, "not a Biolink qualifier"),
        (
            {"qualified_predicate": "biolink:treats", "object_aspect_qualifier": "activity"},
            "not allowed here",
        ),
        ({"qualified_predicate": "biolink:causes"}, "needs a subject_* or object_*"),
        ({"species_context_qualifier": "human"}, "not a CURIE"),
        ({"species_context_qualifier": "MONDO:0005148"}, "not a biolink:OrganismTaxon"),
        ({"object_aspect_qualifier": ["activity"]}, "one non-empty string"),
    ],
)
def test_validate_qualifiers_rejects_bad_sets(qualifiers, problem):
    errors = biolink.validate_qualifiers(qualifiers, CHEMICAL, "biolink:affects", GENE)
    assert len(errors) == 1
    assert problem in errors[0]


def test_validate_qualifiers_slot_not_on_gene_disease():
    errors = biolink.validate_qualifiers(
        {"causal_mechanism_qualifier": "inhibition"},
        GENE,
        "biolink:gene_associated_with_condition",
        DISEASE,
    )
    assert errors and "not allowed on biolink:GeneToDiseaseAssociation" in errors[0]


def test_validate_qualifiers_without_an_association_class():
    errors = biolink.validate_qualifiers(
        {"object_direction_qualifier": "increased"}, ["biolink:NotAClass"], "biolink:affects", GENE
    )
    assert errors and "no Biolink association class fits" in errors[0]


def test_every_listed_enum_value_validates():
    # The prompt lists these values, so bmt must accept each one the code accepts.
    for subject, predicate, object, _ in IN_SCOPE:
        predicate = f"biolink:{predicate}"
        for slot, spec in biolink.allowed_qualifiers(subject, predicate, object).items():
            if spec.values is None or slot == "qualified_predicate":
                continue
            for value in spec.values:
                assert biolink.validate_qualifiers({slot: value}, subject, predicate, object) == []


def test_enum_parent():
    assert biolink.enum_parent("DirectionQualifierEnum", "upregulated") == "increased"
    assert biolink.enum_parent("DirectionQualifierEnum", "increased") is None
    assert biolink.enum_parent("DirectionQualifierEnum", "sideways") is None
    assert biolink.enum_parent("NotAnEnum", "increased") is None
    aspects = "GeneOrGeneProductOrChemicalEntityAspectEnum"
    assert biolink.enum_parent(aspects, "expression") == "abundance"


def test_qualifier_enums():
    enums = biolink.qualifier_enums()
    assert enums["object_direction_qualifier"] == "DirectionQualifierEnum"
    assert enums["subject_form_or_variant_qualifier"] == (
        "ChemicalOrGeneOrGeneProductFormOrVariantEnum"
    )
    assert "qualified_predicate" not in enums and "species_context_qualifier" not in enums
