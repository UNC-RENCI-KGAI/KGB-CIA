"""A toy filtered graph for the skill's tests, built in code (`*.jsonl` is gitignored).

It has one edge or more in each tier of the step 1 scope table: query (drug treats disease,
chemical-gene, gene-disease), capped (gene-gene, gene-phenotype), skipped (participates_in,
expressed_in, orthologous_to); a statement two sources both make; a qualified edge; and a
negated edge. And a toy UBERON release, as the ground step fetches it.
"""

from __future__ import annotations

from shared.schema import Edge, Node

IMATINIB = "CHEBI:45783"
ABL1 = "NCBIGene:25"
CML = "MONDO:0011996"
TP53 = "NCBIGene:7157"
MDM2 = "NCBIGene:4193"
SEIZURE = "HP:0001250"
APOPTOSIS = "GO:0006915"
LIVER = "UBERON:0002107"
MOUSE_TP53 = "NCBIGene:22059"
ASPIRIN = "CHEBI:15365"

NODES = [
    Node(
        id=IMATINIB,
        category=["biolink:SmallMolecule", "biolink:MolecularEntity", "biolink:ChemicalEntity"],
        name="imatinib",
        equivalent_identifiers=[IMATINIB, "MESH:D000068877", "DRUGBANK:DB00619"],
        synonym=["Gleevec", "STI571", "STI-571"],
    ),
    Node(
        id=ABL1,
        category=["biolink:Gene", "biolink:GeneOrGeneProduct"],
        name="ABL1",
        equivalent_identifiers=[ABL1, "HGNC:76", "UniProtKB:P00519"],
        symbol="ABL1",
        full_name="ABL proto-oncogene 1, non-receptor tyrosine kinase",
    ),
    Node(
        id=CML,
        category=["biolink:Disease"],
        name="chronic myelogenous leukemia, BCR-ABL1 positive",
        equivalent_identifiers=[CML, "MESH:D015464", "DOID:8552"],
        synonym=["chronic myeloid leukemia", "CML"],
    ),
    Node(id=TP53, category=["biolink:Gene"], name="TP53", symbol="TP53"),
    Node(id=MDM2, category=["biolink:Gene"], name="MDM2", symbol="MDM2"),
    Node(id=SEIZURE, category=["biolink:PhenotypicFeature"], name="Seizure"),
    Node(id=APOPTOSIS, category=["biolink:BiologicalProcess"], name="apoptotic process"),
    Node(id=LIVER, category=["biolink:AnatomicalEntity"], name="liver"),
    Node(id=MOUSE_TP53, category=["biolink:Gene"], name="Trp53"),
    Node(id=ASPIRIN, category=["biolink:SmallMolecule"], name="aspirin"),
]


def _edge(subject, predicate, object, source, **kw) -> Edge:
    fields = dict(
        subject=subject,
        predicate=predicate,
        object=object,
        primary_knowledge_source=source,
        aggregator_knowledge_source=["infores:tcrd"],
        knowledge_level="knowledge_assertion",
        agent_type="manual_agent",
        kgbcia_source_records=[f"tcrd.{source.split(':')[1]}#{subject}-{object}"],
    )
    return Edge(**{**fields, **kw})


EDGES = [
    # Query tier. The same statement from two sources.
    _edge(IMATINIB, "biolink:treats", CML, "infores:drugcentral", publications=["PMID:11287972"]),
    _edge(IMATINIB, "biolink:treats", CML, "infores:chembl"),
    _edge(
        IMATINIB,
        "biolink:directly_physically_interacts_with",
        ABL1,
        "infores:chembl",
        publications=["PMID:10592173"],
        affinity=8.1,
        affinity_parameter="pIC50",
    ),
    # A qualified edge, as a DrugCentral action type might map.
    _edge(
        IMATINIB,
        "biolink:affects",
        ABL1,
        "infores:drugcentral",
        qualified_predicate="biolink:causes",
        object_aspect_qualifier="activity",
        object_direction_qualifier="decreased",
    ),
    _edge(
        ABL1,
        "biolink:gene_associated_with_condition",
        CML,
        "infores:disgenet",
        knowledge_level="statistical_association",
        agent_type="automated_agent",
    ),
    # Capped tier.
    _edge(TP53, "biolink:physically_interacts_with", MDM2, "infores:string", score=0.999),
    _edge(MDM2, "biolink:physically_interacts_with", TP53, "infores:bioplex", score=0.62),
    _edge(TP53, "biolink:has_phenotype", SEIZURE, "infores:monarchinitiative"),
    # Skipped tier.
    _edge(TP53, "biolink:participates_in", APOPTOSIS, "infores:reactome"),
    _edge(TP53, "biolink:expressed_in", LIVER, "infores:gtex"),
    _edge(TP53, "biolink:orthologous_to", MOUSE_TP53, "infores:hgnc"),
    # A negated source edge: checked like any other, and never supported.
    _edge(ASPIRIN, "biolink:treats", CML, "infores:drugcentral", negated=True),
]

# A few terms of uberon-basic.obo, with its header, qualifiers, comments, a relation that isn't
# is_a or part_of (develops_from), an obsolete term and a relation's own stanza. Liver parenchyma
# is part of the liver, an organ in the digestive system; the kidney is an organ in the renal
# system. Neither of liver and kidney is below the other.
UBERON_VERSION = "uberon/releases/2026-11-05/uberon-basic.owl"
UBERON_OBO = f"""format-version: 1.2
data-version: {UBERON_VERSION}
subsetdef: organ_slim "organs, excluding individual muscles and skeletal elements"
ontology: uberon

[Term]
id: UBERON:0000467
name: anatomical system

[Term]
id: UBERON:0000062
name: organ

[Term]
id: UBERON:0001007
name: digestive system
is_a: UBERON:0000467 {{source="FMA"}} ! anatomical system

[Term]
id: UBERON:0001008
name: renal system
is_a: UBERON:0000467 ! anatomical system

[Term]
id: UBERON:0002107
name: liver
synonym: "hepar" RELATED []
is_a: UBERON:0000062 {{source="cjm"}} ! organ
relationship: part_of UBERON:0001007 {{source="FMA", source="MA"}} ! digestive system
relationship: develops_from UBERON:0003894 ! liver primordium

[Term]
id: UBERON:0001280
name: liver parenchyma
relationship: part_of UBERON:0002107 ! liver

[Term]
id: UBERON:0003894
name: liver primordium

[Term]
id: UBERON:0002113
name: kidney
is_a: UBERON:0000062 ! organ
relationship: part_of UBERON:0001008 ! renal system

[Term]
id: UBERON:0009999
name: obsolete hepatic lobe
is_obsolete: true
replaced_by: UBERON:0002107

[Typedef]
id: part_of
name: part of
is_a: overlaps
is_transitive: true
"""
