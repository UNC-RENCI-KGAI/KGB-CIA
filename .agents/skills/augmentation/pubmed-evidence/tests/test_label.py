"""Step 3 (label.py), per pair, with a fake model client: no network. Biolink lookups are real
(the bundled model, offline)."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from conftest import FakeClient, Sent
from pubmed_evidence import biolink, config, label, llm, records, repair, scope
from pubmed_evidence.cache import (
    AbstractClaimsRow,
    ClaimCheckRow,
    LabelCache,
    PubMedCache,
    claim_id,
)
from pubmed_evidence.graphio import pair_id, read_jsonl, write_jsonl
from pubmed_evidence.records import Entity, Pair

from shared.pubmed import Abstract, FetchedPmid, PubmedRecord

IMATINIB = "CHEBI:45783"
ABL1 = "NCBIGene:25"
CML = "MONDO:0011996"
MODEL = "gpt-5.6-luna"  # in config.MODELS, so it has costs
GENE_PAIR = pair_id(IMATINIB, ABL1)
CML_PAIR = pair_id(IMATINIB, CML)
CHEMICAL = ["biolink:SmallMolecule", "biolink:ChemicalEntity"]
GENE = ["biolink:Gene"]
DISEASE = ["biolink:Disease"]

ABSTRACTS = {
    "1001": Abstract(
        "1001",
        "Imatinib is a potent ABL1 inhibitor",
        "Imatinib bound the ABL1 kinase domain and inhibited its catalytic activity in vitro.",
        2001,
        publication_types=["Journal Article"],
    ),
    "1002": Abstract(
        "1002",
        "Binding studies of STI571",
        "In our assays STI571 did not bind the active conformation of c-Abl at any dose.",
        2002,
    ),
    "1003": Abstract(
        "1003",
        "Cardiovascular events on imatinib",
        "ABL1 transcript levels were measured at baseline in all 300 patients on imatinib.",
        2019,
    ),
    "2001": Abstract(
        "2001",
        "Imatinib in CML",
        "Imatinib induced complete cytogenetic responses in most patients with chronic myeloid "
        "leukemia.",
        2004,
        publication_types=["Randomized Controlled Trial", "Journal Article"],
    ),
}
BOUND = "Imatinib bound the ABL1 kinase domain and inhibited its catalytic activity"
NOT_BOUND = "In our assays STI571 did not bind the active conformation of c-Abl"
RESPONSE = "Imatinib induced complete cytogenetic responses in most patients with chronic"


def pubmed_record(pid: str, pmids: list[str]) -> PubmedRecord:
    return PubmedRecord(
        pair_id=pid,
        query="q",
        retrieved="2026-11-02",
        total=len(pmids),
        synonyms={IMATINIB: ["imatinib", "Gleevec", "STI571"], ABL1: ["ABL1", "c-Abl"]},
        mesh={IMATINIB: ["Imatinib Mesylate"]},
        pmids=[FetchedPmid(pmid=p, origin="search", rank=i) for i, p in enumerate(pmids, 1)],
    )


@dataclass
class Work:
    work: Path
    cache_dir: Path

    def write_pubmed(self, gene_pmids: list[str], cml_pmids: list[str] | None = None) -> None:
        recs = [pubmed_record(GENE_PAIR, gene_pmids)]
        if cml_pmids is not None:
            recs.append(pubmed_record(CML_PAIR, cml_pmids))
        write_jsonl(self.work / records.PUBMED, recs)

    def add_abstracts(self, abstracts) -> None:
        with PubMedCache() as cache:
            cache.put_abstracts(abstracts)

    def answers(self, version: str | None = None, model: str = MODEL) -> dict:
        version = version or label.label_prompt()[1]
        with LabelCache(self.cache_dir / config.LABEL_CACHE) as cache:
            return {(r.pair_id, r.pmid): r for r in cache.abstract_claims(version, model)}

    def checks(self, version: str | None = None) -> dict:
        version = version or label.label_prompt()[1]
        with LabelCache(self.cache_dir / config.LABEL_CACHE) as cache:
            return {(c.pair_id, c.pmid, c.claim_id): c for c in cache.claim_checks(version, MODEL)}


@pytest.fixture
def w(tmp_path: Path) -> Work:
    work = Work(tmp_path / "work", tmp_path / "cache")
    write_jsonl(
        work.work / records.ENTITIES,
        [
            Entity(id=IMATINIB, name="imatinib", category=CHEMICAL, synonyms=["Gleevec"]),
            Entity(id=ABL1, name="ABL1", category=GENE, synonyms=["ABL"]),
            Entity(id=CML, name="chronic myeloid leukemia", category=DISEASE),
        ],
    )
    write_jsonl(
        work.work / records.PAIRS,
        [
            Pair(
                pair_id=GENE_PAIR,
                nodes=tuple(sorted((IMATINIB, ABL1))),
                statement_ids=["s1"],
                rules=["chemical-affects-gene"],
            ),
            Pair(
                pair_id=CML_PAIR,
                nodes=tuple(sorted((IMATINIB, CML))),
                statement_ids=["s2"],
                rules=["chemical-treats-disease"],
            ),
        ],
    )
    work.write_pubmed(["1001", "1002", "1003"], ["2001"])
    work.add_abstracts(ABSTRACTS.values())
    return work


# --- The fake model -------------------------------------------------------------------------


def abstract_ids(sent: Sent) -> dict[str, str]:
    """PMID -> its abstract's position (A1, ...) in a label request."""
    return {pmid: a for a, pmid in re.findall(r"^## (A\d+) \(PMID:(\d+)", sent.user, re.M)}


def pmids_of(sent: Sent) -> list[str]:
    return list(abstract_ids(sent))


def side_of(sent: Sent, node: str) -> str:
    """'X' or 'Y': how a label request names a node."""
    return re.search(rf"^([XY]): .* \({re.escape(node)}\)$", sent.user, re.M).group(1)


def stmt(subject: str, predicate: str, qualified=None, **qualifiers) -> dict:
    """A statement as the reply schema has it (the slots it leaves out would be null)."""
    return {
        "subject": subject,
        "predicate": predicate,
        "qualified_predicate": qualified,
        "qualifiers": qualifiers,
    }


def claim(
    statement: dict,
    sentence: str,
    polarity: str = "positive",
    *,
    also=(),
    role="primary_finding",
    hedged=False,
    finding=None,
    species=None,
    anatomy=None,
    notes=None,
) -> dict:
    """A claim as the reply schema has it."""
    return {
        "polarity": polarity,
        "statement": statement,
        "also_supports": list(also),
        "claim_role": role,
        "hedged": hedged,
        "negative_finding_type": finding,
        "context": {"species": species, "anatomy": anatomy, "notes": notes},
        "evidence_sentence": sentence,
    }


def reply(sent: Sent, claims: dict[str, list[dict]] | None = None, design="in_vitro") -> dict:
    """A reply answering every abstract of the request, with `claims` by PMID."""
    claims = claims or {}
    return {
        "abstracts": [
            {"abstract": a, "study_design": design, "claims": claims.get(pmid, [])}
            for pmid, a in abstract_ids(sent).items()
        ]
    }


def labeler(answer=None) -> FakeClient:
    """A fake model for label requests; by default, no abstract claims anything."""
    usage = llm.Usage([llm.Attempt(MODEL, 800, 200, 0, 100)])
    return FakeClient(answer or (lambda sent: reply(sent)), usage)


def gene_claims(sent: Sent) -> dict:
    """A1001 shows imatinib inhibiting ABL1's activity; A1002 that STI571 didn't bind c-Abl."""
    x, y = side_of(sent, IMATINIB), side_of(sent, ABL1)
    inhibits = stmt(
        x,
        "affects",
        "causes",
        object_aspect_qualifier="activity",
        object_direction_qualifier="decreased",
    )
    return {
        "1001": [claim(inhibits, BOUND, species="human", anatomy=" Liver ")],
        "1002": [claim(stmt(y, "binds"), NOT_BOUND, "negative", finding="failed_to_detect")],
    }


def run_label(w: Work, client: FakeClient, **kw) -> dict:
    return label.label_pairs(w.work, w.cache_dir, model=MODEL, client=client, workers=1, **kw)


def args_for(w: Work, *argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    label.add_arguments(parser)
    args = parser.parse_args(["--model", MODEL, *argv])
    args.input, args.output, args.work, args.cache_dir = None, None, w.work, w.cache_dir
    return args


# --- Claims ---------------------------------------------------------------------------------


def test_claims_are_stored_with_curies_and_an_explicit_object(w):
    client = labeler(
        lambda sent: reply(sent, gene_claims(sent) if "1001" in pmids_of(sent) else {})
    )
    summary = run_label(w, client)
    assert summary["calls"] == 2 and summary["abstracts_answered"] == 4
    answers = w.answers()
    assert set(answers) == {(GENE_PAIR, p) for p in ("1001", "1002", "1003")} | {(CML_PAIR, "2001")}
    first = answers[(GENE_PAIR, "1001")]
    assert (first.study_design, first.answered_by) == ("in_vitro", MODEL)
    [stored] = first.claims
    assert stored == {
        "polarity": "positive",
        "statement": {
            "subject": IMATINIB,
            "predicate": "biolink:affects",
            "object": ABL1,
            "qualified_predicate": "biolink:causes",
            "qualifiers": {
                "object_aspect_qualifier": "activity",
                "object_direction_qualifier": "decreased",
            },
        },
        "also_supports": [],
        "claim_role": "primary_finding",
        "hedged": False,
        "negative_finding_type": None,
        # The species as its NCBITaxon term; the anatomy as written, for the ground step.
        "context": {"species": config.HUMAN, "anatomy": "Liver", "notes": None},
        "evidence_sentence": BOUND,
    }
    # The subject is whichever node the model named: ABL1 here, the object made explicit.
    [negative] = answers[(GENE_PAIR, "1002")].claims
    assert (negative["statement"]["subject"], negative["statement"]["object"]) == (ABL1, IMATINIB)
    # An abstract with no claims is stored with its design.
    assert answers[(GENE_PAIR, "1003")].claims == []
    # Stored claims are what claim_id hashes; a reread hashes the same.
    assert claim_id(stored) == claim_id(json.loads(json.dumps(stored)))
    # Every stored claim reads as the shared schema's records.
    assert label.statement_of(stored["statement"]).id


def test_species_are_ncbitaxon_terms_and_other_is_none(w):
    def answer(sent):
        if "2001" not in pmids_of(sent):
            return reply(sent)
        x = side_of(sent, IMATINIB)
        return reply(
            sent,
            {
                "2001": [
                    claim(stmt(x, "treats"), RESPONSE, species="mouse"),
                    claim(stmt(x, "treats"), RESPONSE, species="other", notes="in  cats"),
                    claim(stmt(x, "treats"), RESPONSE, species=None, notes="  "),
                ]
            },
        )

    run_label(w, labeler(answer))
    contexts = [c["context"] for c in w.answers()[(CML_PAIR, "2001")].claims]
    assert contexts == [
        {"species": "NCBITaxon:10090", "anatomy": None, "notes": None},
        {"species": None, "anatomy": None, "notes": "in cats"},
        {"species": None, "anatomy": None, "notes": None},
    ]
    assert list(config.SPECIES_TAXA) == list(label.SPECIES)
    assert all(t is None or t.startswith("NCBITaxon:") for t in config.SPECIES_TAXA.values())


def test_a_claim_given_twice_is_kept_once(w):
    def answer(sent):
        if "2001" not in pmids_of(sent):
            return reply(sent)
        x = side_of(sent, IMATINIB)
        twice = claim(stmt(x, "treats"), RESPONSE)
        also = {**twice, "also_supports": [stmt(x, "in_clinical_trials_for")] * 2}
        return reply(sent, {pmids_of(sent)[0]: [twice, dict(twice), also]})

    summary = run_label(w, labeler(answer))
    claims = w.answers()[(CML_PAIR, "2001")].claims
    assert len(claims) == 2 and len(claims[1]["also_supports"]) == 1
    assert summary["problems"] == {"claims given twice": 1}


def test_message_names_the_pair_and_never_its_edges(w):
    client = labeler()
    run_label(w, client)
    gene = next(s for s in client.sent if "1001" in pmids_of(s))
    assert "X: imatinib (CHEBI:45783)" in gene.user and "Y: ABL1 (NCBIGene:25)" in gene.user
    assert "  synonyms: Gleevec; STI571" in gene.user
    assert "  MeSH headings: Imatinib Mesylate" in gene.user
    predicates = gene.user.split("# Predicates\n\n", 1)[1].split("\n", 1)[0].split(", ")
    assert "affects" in predicates and "binds" in predicates and "treats" not in predicates
    assert "Q1, with X as the subject of affects" in gene.user
    assert "- object_direction_qualifier: one of DirectionQualifierEnum" in gene.user
    assert "- subject_derivative_qualifier: metabolite" in gene.user
    values = gene.user.split("# Qualifier values\n\n", 1)[1]
    assert values.count("- DirectionQualifierEnum: increased, upregulated, decreased") == 1
    assert "## A1 (PMID:1001, 2001)\nPublication types: Journal Article\nTitle:" in gene.user
    assert gene.user.endswith("of every abstract (A1 to A3).")
    assert "statement" not in gene.user.lower()
    # A chemical and a disease take no qualifiers, and each pair has its own schema.
    cml = next(s for s in client.sent if "2001" in pmids_of(s))
    assert "None of the predicates take qualifiers for this pair." in cml.user
    enums = [
        s.schema.body["$defs"]["statement"]["properties"]["predicate"]["enum"] for s in (gene, cml)
    ]
    assert "treats" in enums[1] and "treats" not in enums[0]


# --- Schemas and versions -------------------------------------------------------------------


def test_predicates_come_from_the_rules_biolink_and_opposites():
    rules = scope.DEFAULT_RULES
    treats = label.predicates_of(rules, ["chemical-treats-disease"])
    # The rule's own, those below them, and their opposites.
    assert {"biolink:treats", "biolink:contraindicated_in"} <= set(treats)
    assert {"biolink:in_clinical_trials_for", "biolink:beneficial_in_models_for"} <= set(treats)
    assert {"biolink:exacerbates_condition", "biolink:promotes_condition"} <= set(treats)
    assert "biolink:affects" not in treats
    # Literature co-occurrence is under associated_with, and a skip rule always drops it.
    genes = label.predicates_of(rules, ["gene-disease"])
    assert "biolink:causes" in genes and "biolink:gene_associated_with_condition" in genes
    assert "biolink:occurs_together_in_literature_with" not in genes
    # A pair matching two rules gets both rules' predicates.
    both = label.predicates_of(rules, ["chemical-treats-disease", "chemical-affects-gene"])
    assert set(both) == set(treats) | set(label.predicates_of(rules, ["chemical-affects-gene"]))
    assert label.predicates_of(rules, ["literature-co-occurrence"]) == ()
    # A skip rule on the edge's provenance drops nothing: a text-mined `treats` edge is skipped,
    # but a paper can still claim `treats`.
    mined = scope.Rule(
        id="mined", tier="skip", predicates=("biolink:treats",), agent_type=("text_mining_agent",)
    )
    assert set(label.predicates_of((mined, *rules), ["chemical-treats-disease"])) == set(treats)


def test_predicates_read_from_the_rules_subject_to_its_object():
    """Biolink's domain and range set the subject and object order, so a reply schema never
    offers a predicate the wrong way round. One defined the other way round becomes its
    inverse, and one that fits neither way is left out."""
    rules = scope.DEFAULT_RULES
    chemical_gene = label.predicates_of(rules, ["chemical-affects-gene"])
    assert {"biolink:affects", "biolink:disrupts", "biolink:binds"} <= set(chemical_gene)
    wrong = {"has_adverse_event", "has_side_effect", "enables", "ameliorates_condition"}
    assert {f"biolink:{p}" for p in wrong}.isdisjoint(chemical_gene)
    schema = label.rule_schemas(rules)["chemical-affects-gene"]
    enum = schema.body["$defs"]["statement"]["properties"]["predicate"]["enum"]
    assert "affects" in enum and wrong.isdisjoint(enum)
    genes = label.predicates_of(rules, ["gene-disease"])
    assert {"biolink:gene_associated_with_condition", "biolink:biomarker_for"} <= set(genes)
    inverses = {
        "condition_associated_with_gene",
        "has_biomarker",
        "increased_likelihood_associated_with",
        "coexpressed_with",
    }
    assert {f"biolink:{p}" for p in inverses}.isdisjoint(genes)
    for rule in rules:
        if rule.tier != "skip":
            for predicate in label.predicates_of(rules, [rule.id]):
                assert biolink.oriented(rule.subject, predicate, rule.object) == (predicate, False)


def test_reply_schema_vocabulary():
    schema = label.reply_schema(("biolink:treats", "biolink:in_clinical_trials_for"))
    statement = schema.body["$defs"]["statement"]["properties"]
    assert statement["subject"]["enum"] == ["X", "Y"]
    assert statement["predicate"]["enum"] == ["treats", "in_clinical_trials_for"]
    assert statement["qualified_predicate"]["anyOf"][0]["enum"] == ["causes"]
    slots = statement["qualifiers"]["properties"]
    assert "qualified_predicate" not in slots and "object_direction_qualifier" in slots
    # A slot takes `none`, not null, so that the slots aren't unions (test_llm counts them).
    direction = slots["object_direction_qualifier"]
    assert direction == {
        "type": "string",
        "enum": ["decreased", "downregulated", "increased"] + ["upregulated", label.UNSET],
    }
    unset = dict.fromkeys(slots, label.UNSET) | {"object_direction_qualifier": "increased"}
    stored = label.stored_statement(stmt("X", "affects", **unset), NODES)
    assert stored["qualifiers"] == {"object_direction_qualifier": "increased"}
    abstract = schema.body["properties"]["abstracts"]["items"]["properties"]
    assert abstract["abstract"]["enum"][-1] == f"A{config.ABSTRACTS_PER_CALL}"
    assert abstract["study_design"]["enum"] == list(label.DESIGNS)
    claim = abstract["claims"]["items"]["properties"]
    assert claim["statement"] == claim["also_supports"]["items"] == {"$ref": "#/$defs/statement"}
    species = claim["context"]["properties"]["species"]["anyOf"][0]["enum"]
    assert species == list(config.SPECIES_TAXA)
    assert label.reply_schema(("biolink:treats",)).text() != schema.text()


def test_prompt_version_covers_rules_effort_and_the_included_rules(tmp_path, monkeypatch):
    low = label.label_prompt(model=MODEL)[1]
    assert label.label_prompt(model=MODEL, reasoning_effort="low")[1] == low
    assert label.label_prompt(model=MODEL, reasoning_effort="medium")[1] != low
    # A model that takes no effort sends none, as 'none' does.
    none = label.label_prompt(model=MODEL, reasoning_effort="none")[1]
    assert label.label_prompt(model="gpt-oss-120b")[1] == none
    # Another set of scope rules means other schemas.
    fewer = tuple(r for r in scope.DEFAULT_RULES if r.id != "gene-gene")
    assert label.label_prompt(model=MODEL, rules=fewer)[1] != low
    # The shared claim rules are part of the prompt.
    prompts = tmp_path / "prompts"
    shutil.copytree(config.PROMPTS_DIR, prompts)
    copy = prompts / "label.md"
    assert label.label_prompt(copy, MODEL)[1] == low
    with (prompts / "claim-rules.md").open("a", encoding="utf-8") as f:
        f.write("\nOne more rule.\n")
    text, version = label.label_prompt(copy, MODEL)
    assert version != low and "One more rule." in text


def test_provenance_rules_have_no_schema_and_leave_the_version_alone():
    """A provenance rule names no predicates: it has no reply schema (its predicate enum would
    be empty), a pair never names it, and adding one, in any tier, leaves the prompt version
    and so the cached claims as they were. Only the statement rules that aren't skips make
    the version, the same five as before provenance rules existed."""
    statement_rules = tuple(r for r in scope.DEFAULT_RULES if not r.is_provenance)
    more = (
        scope.Rule(id="mined", tier="capped", agent_type=("text_mining_agent",)),
        *scope.DEFAULT_RULES,
        scope.Rule(id="co-mentioned", tier="query", knowledge_level=("text_co_occurrence",)),
    )
    schemas = label.rule_schemas(more)
    assert set(schemas) == {
        "chemical-treats-disease",
        "chemical-affects-gene",
        "gene-disease",
        "gene-gene",
        "gene-phenotype",
    }
    for schema in schemas.values():
        assert schema.body["$defs"]["statement"]["properties"]["predicate"]["enum"]
    assert label.predicates_of(more, ["mined"]) == ()
    version = label.label_prompt(model=MODEL, rules=statement_rules)[1]
    assert label.label_prompt(model=MODEL)[1] == version
    assert label.label_prompt(model=MODEL, rules=more)[1] == version


# --- Checks ---------------------------------------------------------------------------------

NODES = {"X": IMATINIB, "Y": ABL1}
CML_NODES = {"X": IMATINIB, "Y": CML}
GENE_CML_NODES = {"X": ABL1, "Y": CML}
CATEGORIES = {IMATINIB: CHEMICAL, ABL1: GENE, CML: DISEASE}
GOF = {"subject_form_or_variant_qualifier": "gain_of_function_variant_form"}
OGOF = {"object_form_or_variant_qualifier": "gain_of_function_variant_form"}
INHIBITS = stmt(
    "X",
    "affects",
    "causes",
    object_aspect_qualifier="activity",
    object_direction_qualifier="decreased",
)


@pytest.mark.parametrize(
    "nodes, item, expected",
    [
        (NODES, claim(INHIBITS, BOUND), []),
        (
            NODES,
            claim(stmt("X", "affects", "causes", causal_mechanism_qualifier="inhibition"), BOUND),
            ["qualified_predicate causes needs an aspect or a direction"],
        ),
        (
            CML_NODES,
            claim(stmt("X", "treats", object_direction_qualifier="decreased"), RESPONSE),
            ["object_direction_qualifier doesn't apply to treats between these entities"],
        ),
        (
            NODES,
            claim(stmt("X", "affects", object_aspect_qualifier="made_up"), BOUND),
            ["'made_up' is not an allowed value of object_aspect_qualifier for affects"],
        ),
        (
            CML_NODES,
            claim(stmt("X", "treats"), RESPONSE, finding="demonstrated_absence"),
            ["negative_finding_type is for negative claims only"],
        ),
        (
            CML_NODES,
            claim(
                stmt("X", "treats"),
                RESPONSE,
                also=[stmt("X", "treats_or_applied_or_studied_to_treat")],
            ),
            ["also_supports[0] is already supported by the claim's statement"],
        ),
        (
            CML_NODES,
            claim(
                stmt("X", "treats"),
                RESPONSE,
                "negative",
                also=[stmt("X", "ameliorates_condition")],
            ),
            ["also_supports[0] contradicts the negative claim"],
        ),
        (
            CML_NODES,
            claim(
                stmt("X", "treats"),
                RESPONSE,
                "negative",
                also=[stmt("X", "in_clinical_trials_for", object_direction_qualifier="decreased")],
                finding="demonstrated_absence",
            ),
            [
                "also_supports[0]: object_direction_qualifier doesn't apply to "
                "in_clinical_trials_for between these entities"
            ],
        ),
        # A null trial: a negative treats that also supports in_clinical_trials_for. Fine, and
        # so is a negative claim without a type.
        (
            CML_NODES,
            claim(
                stmt("X", "treats"),
                RESPONSE,
                "negative",
                also=[stmt("X", "in_clinical_trials_for")],
            ),
            [],
        ),
        # Biolink's domain and range set the order. A predicate defined the other way round is
        # checked as its inverse, and one from the disease is checked turned round. A symmetric
        # one is fine either way.
        (
            GENE_CML_NODES,
            claim(stmt("X", "condition_associated_with_gene", **GOF), RESPONSE),
            [],
        ),
        (GENE_CML_NODES, claim(stmt("Y", "gene_associated_with_condition", **OGOF), RESPONSE), []),
        (NODES, claim(stmt("Y", "binds"), BOUND), []),
        # One that fits neither way fails the claim, in its own statement or an also_supports.
        (
            NODES,
            claim(stmt("X", "has_adverse_event"), BOUND),
            [
                "has_adverse_event fits neither way between SmallMolecule and Gene by its "
                "Biolink domain and range"
            ],
        ),
        (
            NODES,
            claim(INHIBITS, BOUND, also=[stmt("X", "enables")]),
            [
                "also_supports[0]: enables fits neither way between SmallMolecule and Gene by "
                "its Biolink domain and range"
            ],
        ),
    ],
)
def test_check_claim(nodes, item, expected):
    stored = label.stored_claim(item, nodes)
    assert label.check_claim(stored, CATEGORIES, label.Qualifiers()) == expected


def test_a_misdirected_claim_is_checked_turned_and_stored_as_given():
    """The check reads a claim Biolink's way round, where its qualifiers fit (the gene's form
    on the gene, now the subject), but doesn't rewrite it: `claim_id` hashes it as stored."""
    item = claim(stmt("Y", "gene_associated_with_condition", **OGOF), RESPONSE)
    stored = label.stored_claim(item, GENE_CML_NODES)
    before = claim_id(stored)
    assert label.check_claim(stored, CATEGORIES, label.Qualifiers()) == []
    as_given = label.Qualifiers().errors(stored["statement"], CATEGORIES)
    assert as_given == [
        "object_form_or_variant_qualifier doesn't apply to gene_associated_with_condition "
        "between these entities"
    ]
    assert stored["statement"]["subject"] == CML and claim_id(stored) == before


@pytest.mark.parametrize(
    "sentence, found",
    [
        (BOUND, True),
        ("imatinib BOUND the abl1   kinase domain", True),
        ("Imatinib bound the ABL1 kinase domain ... in vitro", False),  # a part too short
        ("Imatinib is a potent ABL1 inhibitor ... inhibited its catalytic activity", False),
        ("Imatinib bound the ABL1 kinase domain ... inhibited its catalytic activity", True),
        ("Imatinib bound ABL1 and inhibited it", False),
        ("", False),
    ],
)
def test_sentence_found(sentence, found):
    assert label.sentence_found({"evidence_sentence": sentence}, ABSTRACTS["1001"]) is found
    assert label.sentence_found({"evidence_sentence": sentence}, None) is False


def test_checks_are_stored_counted_and_recomputed(w):
    def answer(sent):
        if "2001" in pmids_of(sent):
            x = side_of(sent, IMATINIB)
            return reply(
                sent,
                {
                    "2001": [
                        claim(stmt(x, "treats"), "Imatinib cured everyone", "negative"),
                        claim(stmt(x, "treats", object_direction_qualifier="increased"), RESPONSE),
                    ]
                },
            )
        return reply(sent, gene_claims(sent))

    summary = run_label(w, labeler(answer))
    checked = summary["checked"]
    assert (checked["abstracts"], checked["no_claim"], checked["claims"]) == (4, 1, 4)
    assert (checked["valid"], checked["invalid"]) == (3, 1)
    # Of the valid claims, the made-up sentence; and a negative without a finding type.
    assert (checked["sentences_not_found"], checked["negatives_without_type"]) == (1, 1)
    error = "object_direction_qualifier doesn't apply to treats between these entities"
    assert checked["errors"] == {error: 1}
    checks = w.checks()
    rows = {(k[1], c.valid, c.sentence_valid) for k, c in checks.items()}
    assert rows == {
        ("1001", True, True),
        ("1002", True, True),
        ("2001", True, False),
        ("2001", False, True),
    }
    # Nothing versions the checks, so every run recomputes them over the run's claims.
    key, wrong = next((k, c) for k, c in checks.items() if not c.valid)
    with LabelCache(w.cache_dir / config.LABEL_CACHE) as cache:
        cache.put_claim_checks([replace(wrong, valid=True, errors=[])])
    again = labeler()
    run_label(w, again)
    assert again.sent == [] and w.checks()[key] == wrong


# --- Replies --------------------------------------------------------------------------------


def request_for(pmids: list[str]) -> label.Request:
    job = label.PairJob(
        GENE_PAIR,
        [Entity(id=IMATINIB, category=CHEMICAL), Entity(id=ABL1, category=GENE)],
        {},
        {},
        ("chemical-affects-gene",),
        ("biolink:affects",),
        pmids,
    )
    return label.Request(job, pmids)


def parse(body: dict, pmids: list[str]) -> label.Parsed:
    return label.parse_reply(body, request_for(pmids), prompt_version="v", model=MODEL)


def entry(position: str, claims=()) -> dict:
    return {"abstract": position, "study_design": "animal", "claims": list(claims)}


def test_parse_maps_positions_back():
    parsed = parse({"abstracts": [entry("A2", [claim(INHIBITS, BOUND)]), entry("A1")]}, ["1", "2"])
    assert [(r.pmid, r.study_design, len(r.claims)) for r in parsed.rows] == [
        ("1", "animal", 0),
        ("2", "animal", 1),
    ]
    assert parsed.problems == {}


@pytest.mark.parametrize(
    "body, message",
    [
        ({"abstracts": [entry("A1"), entry("A2")]}, "positions beyond the request: A2"),
        ({"abstracts": [entry("A1"), entry("A1")]}, "answered twice: A1"),
    ],
)
def test_a_position_beyond_the_request_or_twice_fails_the_call(body, message):
    with pytest.raises(ValueError, match=message):
        parse(body, ["1"])


def test_an_abstract_left_out_is_asked_again(w, capsys, monkeypatch):
    def answer(sent):
        body = reply(sent)
        body["abstracts"] = [a for a in body["abstracts"] if a["abstract"] != "A2"]
        return body

    monkeypatch.setattr(llm, "client_for", lambda model, **kw: labeler(answer))
    assert label.run(args_for(w)) == 0
    assert "1 abstracts were left out of the replies" in capsys.readouterr().err
    assert (GENE_PAIR, "1002") not in w.answers() and len(w.answers()) == 3
    again = labeler()
    run_label(w, again)
    assert [pmids_of(s) for s in again.sent] == [["1002"]]


def test_unreadable_reply_is_a_failed_call_and_retried(w):
    first = labeler(lambda sent: {"abstracts": [entry("A9")]})
    summary = run_label(w, first)
    assert summary["calls_failed"] == 2 and w.answers() == {}
    again = labeler()
    run_label(w, again)
    assert len(again.sent) == 2


def test_malformed_reply_counts_as_failed(w):
    summary = run_label(w, labeler(lambda sent: {"verdict": "nothing here"}))
    assert summary["calls_failed"] == 2
    assert w.answers() == {}


# --- Reruns and chunks ----------------------------------------------------------------------


def test_rerun_asks_only_unanswered_abstracts(w):
    run_label(w, labeler())
    w.write_pubmed(["1001", "1002", "1003", "1004"], ["2001"])
    w.add_abstracts([Abstract("1004", "New", "Imatinib and ABL1 in a new study of many cells.")])
    again = labeler()
    run_label(w, again)
    assert [pmids_of(s) for s in again.sent] == [["1004"]]


def test_a_new_model_or_prompt_starts_fresh(w, tmp_path):
    run_label(w, labeler())
    other = labeler()
    label.label_pairs(w.work, w.cache_dir, model="gpt-oss-120b", client=other, workers=1)
    assert len(other.sent) == 2
    prompt = tmp_path / "label.md"
    prompt.write_text(config.LABEL_PROMPT.read_text() + "\nBe strict.\n")
    tuned = labeler()
    summary = run_label(w, tuned, prompt=prompt)
    assert len(tuned.sent) == 2 and summary["prompt_version"] != label.label_prompt()[1]
    # The tuned copy still includes the shared rules from the skill's prompts directory.
    assert "## Which entities count" in tuned.sent[0].system


def test_another_effort_reads_afresh(w):
    run_label(w, labeler())
    assert records.read_label_run(w.work).reasoning_effort == "low"
    client = labeler()
    summary = run_label(w, client, reasoning_effort="medium")
    assert client.sent and summary["reasoning_effort"] == "medium"
    run = records.read_label_run(w.work)
    assert (run.reasoning_effort, run.prompt_version) == ("medium", summary["prompt_version"])
    again = labeler()
    run_label(w, again, reasoning_effort="medium")
    assert again.sent == []


def test_chunking(w):
    client = labeler()
    run_label(w, client, abstracts_per_call=2)
    assert sorted(pmids_of(s) for s in client.sent) == [["1001", "1002"], ["1003"], ["2001"]]
    # Each chunk names its abstracts from A1.
    assert all(re.search(r"^## A1 \(PMID:", s.user, re.M) for s in client.sent)


def test_abstracts_per_call_is_at_most_20(w, capsys):
    many = [str(3000 + i) for i in range(25)]
    w.write_pubmed(many)
    w.add_abstracts([Abstract(p, f"Title {p}", f"Abstract text number {p}.") for p in many])
    client = labeler()
    run_label(w, client, abstracts_per_call=50)
    assert sorted(len(pmids_of(s)) for s in client.sent) == [5, 20]
    assert "at most 20 abstracts per call" in capsys.readouterr().err


# A provenance rule names no predicates, so a pair naming one (select never writes one) has
# none to offer from it.
@pytest.mark.parametrize("unknown", ["no-such-rule", "text-mined"])
def test_pairs_without_pubmed_rules_or_abstracts_are_skipped(w, unknown):
    pairs = list(records.read_records(w.work / records.PAIRS, Pair))
    pairs[1] = pairs[1].model_copy(update={"rules": [unknown]})
    third = Pair(pair_id="A:1|B:1", nodes=("A:1", "B:1"), statement_ids=["s3"], rules=["gene-gene"])
    write_jsonl(w.work / records.PAIRS, [*pairs, third])
    w.write_pubmed(["1001", "9999"], ["2001"])
    client = labeler()
    summary = run_label(w, client)
    assert summary["skipped"] == {
        "pairs whose scope rules aren't known": 1,
        "pairs without a PubMed record": 1,
        "abstracts missing from the PubMed cache": 1,
    }
    assert [pmids_of(s) for s in client.sent] == [["1001"]]


def test_the_rules_select_used_are_read_from_its_report(w):
    rules = [r.model_dump() for r in scope.DEFAULT_RULES if r.id != "gene-gene"]
    rules_file = w.work / "rules.json"
    rules_file.write_text(json.dumps(rules))
    records.write_json(w.work / records.SCOPE_REPORT, {"rules_file": str(rules_file)})
    summary = run_label(w, labeler())
    assert label.rules_of(w.work) == tuple(scope.Rule.model_validate(r) for r in rules)
    assert summary["prompt_version"] != label.label_prompt(model=MODEL)[1]
    rules_file.write_text("not json")
    with pytest.raises(scope.RulesError):
        run_label(w, labeler())


# --- Failures -------------------------------------------------------------------------------


def test_failed_calls_logged_and_fail_the_run(w, monkeypatch, capsys):
    def answer(sent: Sent):
        if "2001" in pmids_of(sent):
            usage = llm.Usage([llm.Attempt(MODEL, 500, 0, 0, 16000)])
            return llm.LLMError("incomplete response (max_output_tokens)", usage)
        return reply(sent)

    client = labeler(answer)
    monkeypatch.setattr(llm, "client_for", lambda model, **kw: client)
    assert label.run(args_for(w, "--workers", "2")) == 1
    err = capsys.readouterr().err
    assert "max_output_tokens" in err and "1 of the 2 calls these pairs need failed" in err
    assert not (w.work / records.LABEL_RUN).exists()
    with LabelCache(w.cache_dir / config.LABEL_CACHE) as cache:
        rows = cache.db.execute("SELECT pair_id, error, output_tokens FROM calls").fetchall()
    failed = [r for r in rows if r["error"]]
    assert [(r["pair_id"], r["output_tokens"]) for r in failed] == [(CML_PAIR, 16000)]
    # The other pair's answers are kept, and a rerun asks again only for the failed pair.
    assert len(w.answers()) == 3
    retry = labeler()
    run_label(w, retry)
    assert [pmids_of(s) for s in retry.sent] == [["2001"]]


def test_a_failed_chunk_leaves_the_pairs_other_answers(w):
    def answer(sent):
        return llm.LLMError("boom") if pmids_of(sent) == ["1002"] else reply(sent)

    run_label(w, labeler(answer), abstracts_per_call=1)
    assert {p for (pair, p) in w.answers() if pair == GENE_PAIR} == {"1001", "1003"}


def test_few_failures_only_warn(w, monkeypatch, capsys):
    many = [str(3000 + i) for i in range(21)]
    w.write_pubmed(many, ["2001"])
    w.add_abstracts([Abstract(p, f"Title {p}", f"Abstract text number {p}.") for p in many])

    def answer(sent: Sent):
        return llm.LLMError("boom") if pmids_of(sent) == ["3005"] else reply(sent)

    monkeypatch.setattr(llm, "client_for", lambda model, **kw: labeler(answer))
    assert label.run(args_for(w, "--abstracts-per-call", "1")) == 0
    captured = capsys.readouterr()
    assert "calls: 22 (21 ok, 1 failed)" in captured.out
    assert "rerun to retry; answers already cached are kept" in captured.err
    assert records.read_label_run(w.work) is not None


@pytest.mark.parametrize("stuck, rc", [({"3005"}, 0), ({"3005", "3006"}, 1)])
def test_failures_are_a_share_of_every_call_the_pairs_need(w, monkeypatch, capsys, stuck, rc):
    """A rerun that sends only the calls that keep failing is judged on every call its pairs
    need, those answered before included: one stuck call in 22 never fails a later run, while
    two (more than 5% of the work) fail every run until they're answered."""
    many = [str(3000 + i) for i in range(21)]
    w.write_pubmed(many, ["2001"])
    w.add_abstracts([Abstract(p, f"Title {p}", f"Abstract text number {p}.") for p in many])

    def answer(sent: Sent):
        return llm.LLMError("boom") if set(pmids_of(sent)) & stuck else reply(sent)

    monkeypatch.setattr(llm, "client_for", lambda model, **kw: labeler(answer))
    assert label.run(args_for(w, "--abstracts-per-call", "1")) == rc
    capsys.readouterr()
    (w.work / records.LABEL_RUN).unlink(missing_ok=True)
    assert label.run(args_for(w, "--abstracts-per-call", "1")) == rc
    captured = capsys.readouterr()
    assert f"calls: {len(stuck)} (0 ok, {len(stuck)} failed)" in captured.out
    assert (records.read_label_run(w.work) is not None) == (rc == 0)
    if rc:
        assert f"{len(stuck)} of the 22 calls these pairs need failed" in captured.err


def test_missing_input_names_the_step(w, capsys):
    (w.work / records.PUBMED).unlink()
    assert label.run(args_for(w)) == 1
    assert "kgb-augment pubmed" in capsys.readouterr().err
    (w.work / records.PAIRS).unlink()
    assert label.run(args_for(w)) == 1
    assert "kgb-augment select" in capsys.readouterr().err


def test_missing_key(w, monkeypatch, capsys):
    monkeypatch.delenv(config.AZURE_KEY_VAR, raising=False)
    assert label.run(args_for(w)) == 1
    assert config.AZURE_KEY_VAR in capsys.readouterr().err


def test_unknown_model(w, capsys):
    assert label.run(args_for(w, "--model", "not-a-model")) == 1
    assert "not-a-model is not in config.MODELS" in capsys.readouterr().err


def test_answers_a_fallback_model_gave_are_its_and_counted(w):
    usage = llm.Usage([llm.Attempt(MODEL, 100, 0, 0, 0), llm.Attempt("claude-opus-5", 900)])
    summary = run_label(w, FakeClient(lambda sent: reply(sent), usage))
    assert summary["fallback_served"] == 2
    assert {a.answered_by for a in w.answers().values()} == {"claude-opus-5"}


# --- Quote repair ---------------------------------------------------------------------------

MADE_UP = "Imatinib was found to bind ABL1 tightly in every assay we ran"


def misquoted(sent: Sent):
    """The gene pair's first claim misquotes A1001; the CML pair's claim is invalid and
    misquoted."""
    if "2001" in pmids_of(sent):
        x = side_of(sent, IMATINIB)
        bad = claim(stmt(x, "treats", object_direction_qualifier="increased"), "Not a quote at all")
        return reply(sent, {"2001": [bad]})
    claims = gene_claims(sent)
    claims["1001"] = [{**claims["1001"][0], "evidence_sentence": MADE_UP}]
    return reply(sent, claims)


def repairer(answer=None) -> FakeClient:
    usage = llm.Usage([llm.Attempt(config.REPAIR_MODEL, 300, 0, 0, 20)])
    return FakeClient(answer or (lambda sent: {"quote": BOUND}), usage)


def repairs_of(w: Work, summary: dict) -> dict:
    with LabelCache(w.cache_dir / config.LABEL_CACHE) as cache:
        rows = cache.claim_repairs(summary["prompt_version"], MODEL, *repair.current())
        return {(r.pair_id, r.pmid): r for r in rows}


def test_only_valid_claims_whose_sentence_wasnt_found_are_repaired(w):
    fixer = repairer()
    summary = run_label(w, labeler(misquoted), repair_client=fixer)
    [sent] = fixer.sent
    assert sent.system == repair.repair_prompt()[0] and sent.schema == repair.REPLY_SCHEMA
    assert "Positive claim: imatinib causes decreased activity of ABL1" in sent.user
    assert f"Evidence sentence not found in the abstract: {json.dumps(MADE_UP)}" in sent.user
    assert "## Abstract (PMID:1001, 2001)" in sent.user and '"context"' in sent.user
    [row] = repairs_of(w, summary).values()
    assert (row.pair_id, row.pmid, row.evidence_sentence, row.sentence_valid) == (
        GENE_PAIR,
        "1001",
        BOUND,
        True,
    )
    assert row.answered_by == config.REPAIR_MODEL
    assert summary["repairs"]["found"] == 1 and summary["repairs"]["pending"] == 1
    # The stored claim is never rewritten: the repair sits beside it.
    [stored] = w.answers()[(GENE_PAIR, "1001")].claims
    assert stored["evidence_sentence"] == MADE_UP and row.claim_id == claim_id(stored)
    with LabelCache(w.cache_dir / config.LABEL_CACHE) as cache:
        found = repair.sentence_repairs(cache, summary["prompt_version"], MODEL, *repair.current())
    assert found == {(GENE_PAIR, "1001", row.claim_id): BOUND}


def test_a_repair_answered_is_not_asked_again(w):
    fixer = repairer(lambda sent: {"quote": "Still not in the abstract, said differently"})
    summary = run_label(w, labeler(misquoted), repair_client=fixer)
    assert summary["repairs"]["not_found"] == 1
    [row] = repairs_of(w, summary).values()
    assert row.sentence_valid is False
    again = repairer()
    run_label(w, labeler(), repair_client=again)
    assert again.sent == []
    with LabelCache(w.cache_dir / config.LABEL_CACHE) as cache:
        assert (
            repair.sentence_repairs(cache, summary["prompt_version"], MODEL, *repair.current())
            == {}
        )


def test_a_failed_repair_call_is_retried(w):
    summary = run_label(w, labeler(misquoted), repair_client=repairer(lambda s: llm.LLMError("x")))
    assert summary["repairs"]["failed_calls"] == 1 and repairs_of(w, summary) == {}
    again = repairer(lambda sent: {"quote": None})
    summary = run_label(w, labeler(), repair_client=again)
    assert len(again.sent) == 1 and summary["repairs"]["none"] == 1
    [row] = repairs_of(w, summary).values()
    assert (row.evidence_sentence, row.sentence_valid) == (None, None)


def test_label_run_names_the_repairs_that_count(w):
    run_label(w, labeler(misquoted), repair_client=repairer())
    run = records.read_label_run(w.work)
    assert (run.repairer, run.repair_prompt_version) == repair.current()


def test_no_repair_and_no_credentials(w, capsys):
    summary = run_label(w, labeler(misquoted), repair_quotes=False)
    assert "repairs" not in summary
    summary = run_label(w, labeler())
    assert summary["repairs"]["skipped"] and summary["repairs"]["pending"] == 1
    label.print_summary(summary)
    assert "quote repair skipped" in capsys.readouterr().err


# --- Modes ----------------------------------------------------------------------------------


def test_sample_files(w):
    summary = run_label(w, labeler(misquoted), sample=1, seed=3, repair_client=repairer())
    md, jsonl = (Path(p) for p in summary["sample_files"])
    text = md.read_text()
    assert "# Label sample" in text and "seed 3" in text
    rows = list(read_jsonl(jsonl))
    if rows[0]["pair_id"] == GENE_PAIR:
        assert "**positive**: imatinib causes decreased activity of ABL1" in text
        assert "(NOT FOUND)" in text and f'repair: "{BOUND}" (found)' in text
        assert "**negative**: ABL1 does not bind imatinib" in text and "- no claims" in text
        assert [len(r["claims"]) for r in rows] == [1, 1, 0]
    else:
        assert "INVALID: object_direction_qualifier doesn't apply" in text
        assert rows[0]["study_design"] == "in_vitro" and rows[0]["claims"][0]["valid"] is False
    first = rows[0]
    assert {"pair_id", "pmid", "title", "abstract", "publication_types", "claims"} <= set(first)
    assert not (w.work / records.LABEL_RUN).exists()


def test_a_pairs_file_labels_exactly_its_pairs(w, capsys):
    """A gold-set tool: `--pairs FILE` labels the pairs a file names, as `evaluate --draw`
    writes it, either way round; a pair select didn't keep, or with nothing to read, is
    reported."""
    run_label(w, labeler())
    before = records.read_label_run(w.work)
    pairs = w.work / "gold_pairs.jsonl"
    cml = "|".join(reversed(CML_PAIR.split("|")))
    lines = [
        {"pair_id": cml, "outcome": "refined", "family": "chemical-treats-disease", "edges": {}},
        {"pair_id": "A:1|B:1"},
        {"pair_id": CML_PAIR},
    ]
    write_jsonl(pairs, lines)
    client = labeler()
    summary = label.label_pairs(
        w.work, w.cache_dir, model="gpt-oss-120b", client=client, workers=1, pairs=pairs
    )
    assert [pmids_of(s) for s in client.sent] == [["2001"]]
    assert summary["pairs"] == 1
    assert summary["pairs_not_found"] == {"not_selected": ["A:1|B:1"], "skipped": []}
    md = Path(summary["sample_files"][0]).read_text()
    assert f"1 pairs from {pairs}" in md
    assert [r["pair_id"] for r in read_jsonl(w.work / label.SAMPLE_JSONL)] == [CML_PAIR]
    assert records.read_label_run(w.work) == before

    # The scope rules still apply: a pair select kept with nothing to read is reported too.
    w.write_pubmed(["1001", "1002", "1003"])  # the CML pair is no longer searched
    write_jsonl(pairs, [{"pair_id": CML_PAIR}, {"pair_id": "A:1|B:1"}])
    assert label.run(args_for(w, "--pairs", str(pairs))) == 0
    err = capsys.readouterr().err
    assert "1 pairs in" in err and "weren't kept by select, so they aren't labeled: A:1|B:1" in err
    assert f"have nothing to read (see skipped): {CML_PAIR}" in err

    pairs.write_text('{"pair": "x"}\n')
    assert label.run(args_for(w, "--pairs", str(pairs))) == 1
    assert "a line needs a pair_id" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        args_for(w, "--pairs", str(pairs), "--sample", "1")


def test_dry_run_sends_nothing(w, monkeypatch):
    def no_client(*a, **kw):
        raise AssertionError("a dry run must not build a client")

    monkeypatch.setattr(llm, "client_for", no_client)
    monkeypatch.delenv(config.AZURE_KEY_VAR, raising=False)
    summary = label.label_pairs(w.work, w.cache_dir, model=MODEL, dry_run=True)
    assert summary["requests"] == 2 and summary["calls"] == 0
    assert summary["abstracts_to_answer"] == 4
    assert summary["estimated_input_tokens"] > 1000
    # Two pairs, two schemas: neither call reads the other's prefix from the cache.
    assert summary["estimated_cached_tokens"] == 0
    assert summary["estimated_cost"] > 0
    example = (w.work / label.REQUEST_EXAMPLE).read_text()
    assert "=== instructions (system) ===" in example and "## A1 (PMID:" in example
    assert "## Which entities count" in example
    assert not (w.cache_dir / config.LABEL_CACHE).exists()
    assert not (w.work / records.LABEL_RUN).exists()


def test_dry_run_caches_a_schemas_prefix_after_its_first_call(w):
    summary = label.label_pairs(
        w.work, w.cache_dir, model=MODEL, dry_run=True, abstracts_per_call=1
    )
    assert summary["requests"] == 4 and summary["estimated_cached_tokens"] > 0


def test_dry_run_prices_each_call_by_its_own_prompt(w, monkeypatch):
    haiku = config.MODELS["claude-haiku-5-5"]
    first = label.label_pairs(w.work, w.cache_dir, model=haiku.id, dry_run=True)
    # A longer-prompt price below the run's tokens but above every call's: it never applies.
    tier = (first["estimated_input_tokens"] - 1, config.Price(9, 9, 9, 9))
    monkeypatch.setitem(config.MODELS, haiku.id, replace(haiku, long_prompt=tier))
    again = label.label_pairs(w.work, w.cache_dir, model=haiku.id, dry_run=True)
    assert first["requests"] == 2
    assert again["estimated_cost"] == pytest.approx(first["estimated_cost"])


def test_label_run_written(w):
    run_label(w, labeler())
    run = records.read_label_run(w.work)
    version = label.label_prompt()[1]
    assert (run.model, run.prompt_version, run.prompt) == (MODEL, version, str(config.LABEL_PROMPT))
    # A review of this run is kept; another model's run clears it. The UBERON release the
    # ground step pinned is kept either way.
    update = {"reviewer": "gpt-5.6-terra", "review_prompt_version": "r@1", "uberon_version": "u"}
    records.write_label_run(w.work, run.model_copy(update=update))
    run_label(w, labeler())
    run = records.read_label_run(w.work)
    assert (run.reviewer, run.uberon_version) == ("gpt-5.6-terra", "u")
    label.label_pairs(w.work, w.cache_dir, model="gpt-oss-120b", client=labeler(), workers=1)
    run = records.read_label_run(w.work)
    assert (run.model, run.reviewer, run.review_prompt_version) == ("gpt-oss-120b", None, None)
    assert run.uberon_version == "u"


def test_sample_runs_leave_label_run_alone(w):
    run_label(w, labeler())
    before = records.read_label_run(w.work)
    label.label_pairs(
        w.work, w.cache_dir, model="gpt-oss-120b", client=labeler(), workers=1, sample=1
    )
    assert records.read_label_run(w.work) == before


def test_run_prints_a_summary(w, monkeypatch, capsys):
    monkeypatch.setattr(
        llm, "client_for", lambda model, **kw: labeler(lambda s: reply(s, gene_claims(s)))
    )
    assert label.run(args_for(w, "--reasoning-effort", "none", "--limit", "1")) == 0
    out = capsys.readouterr().out
    assert "label: calls: 1 (1 ok, 0 failed)" in out
    assert "label: answered 3 abstracts with 2 claims" in out
    assert "3 abstracts (1 with no claim), 2 claims: 2 valid, 0 invalid" in out
    assert all(line.startswith("label: ") for line in out.splitlines())


def test_cached_answers_are_never_asked_again(w):
    version = label.label_prompt()[1]
    with LabelCache(w.cache_dir / config.LABEL_CACHE) as cache:
        cache.put_abstract_claims([AbstractClaimsRow(CML_PAIR, "2001", version, MODEL, "rct")])
    client = labeler()
    run_label(w, client)
    assert len(client.sent) == 1 and "2001" not in pmids_of(client.sent[0])
    assert w.answers()[(CML_PAIR, "2001")].study_design == "rct"


# --- Qualifiers -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subject, predicate, object, expected",
    [
        # Chemical and disease: nothing.
        (["biolink:SmallMolecule"], "biolink:treats", DISEASE, set()),
        (["biolink:Drug"], "biolink:contraindicated_in", DISEASE, set()),
        # Gene and disease: subject qualifiers only, with qualified_predicate.
        (
            GENE,
            "biolink:gene_associated_with_condition",
            DISEASE,
            {
                "qualified_predicate",
                "subject_aspect_qualifier",
                "subject_direction_qualifier",
                "subject_form_or_variant_qualifier",
            },
        ),
    ],
)
def test_offered_qualifiers_fit_the_entities(subject, predicate, object, expected):
    offered = set(label.Qualifiers().offered(subject, predicate, object))
    assert offered == expected
    assert not any(k.startswith("object_") for k in offered)


def test_chemical_gene_offers_both_sides():
    offered = label.Qualifiers().offered(["biolink:SmallMolecule"], "biolink:affects", GENE)
    assert {
        "object_aspect_qualifier",
        "subject_derivative_qualifier",
        "qualified_predicate",
    } <= set(offered)
    assert "anatomical_context_qualifier" not in offered
    assert biolink.qualifier_values()["object_direction_qualifier"]


def test_check_rows_carry_claim_ids(w):
    run_label(w, labeler(lambda s: reply(s, gene_claims(s))))
    stored = w.answers()[(GENE_PAIR, "1001")].claims[0]
    check = w.checks()[(GENE_PAIR, "1001", claim_id(stored))]
    assert isinstance(check, ClaimCheckRow) and check.valid and check.sentence_valid
