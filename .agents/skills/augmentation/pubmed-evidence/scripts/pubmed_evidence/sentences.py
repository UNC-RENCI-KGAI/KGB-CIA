"""Step 4: each statement as an English sentence, for step 5 to label abstracts against.

The sentence follows Biolink's reading order for qualifier-based statements
(https://biolink.github.io/biolink-model/reading-a-qualifier-based-statement/): the subject
composed with its qualifiers, then `qualified_predicate` (or `predicate` when there is none),
then the object composed with its qualifiers, then the statement's context. Each side is built
from the core concept outwards, each layer applying to everything before it: derivative, part,
form or variant, specialization, aspect, direction, then process and context. So `affects`
with `qualified_predicate: causes`, `object_aspect_qualifier: expression` and
`object_direction_qualifier: increased` reads "A causes increased expression of B".

Predicate phrases are written by hand, since Biolink's slot names don't always read well
(`treats_or_applied_or_studied_to_treat`); any other predicate falls back to its Biolink name.
Qualifier values are either enum values, read with underscores as spaces, or CURIEs (contexts),
kept as they are: step 5 also gets the structured statement, so the LLM can resolve them.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

from pubmed_evidence import biolink, records

# One phrase per in-scope predicate (step 2's table) and the descendants the transform may
# pick, plus the predicates a `qualified_predicate` usually names. The subject comes before
# the phrase and the object after it.
PREDICATE_PHRASES: dict[str, str] = {
    # Chemical or drug -> disease. The first groups the treats, applied and studied senses.
    "biolink:treats_or_applied_or_studied_to_treat": "is used or studied to treat",
    "biolink:treats": "treats",
    "biolink:applied_to_treat": "is used to treat",
    "biolink:studied_to_treat": "has been studied as a treatment for",
    "biolink:in_clinical_trials_for": "has been tested in clinical trials for",
    "biolink:in_preclinical_trials_for": "has been tested in preclinical studies for",
    "biolink:beneficial_in_models_for": "is beneficial in models of",
    "biolink:ameliorates_condition": "ameliorates",
    "biolink:preventative_for_condition": "prevents",
    "biolink:contraindicated_in": "is contraindicated in",
    # Chemical -> gene, gene <-> gene.
    "biolink:affects": "affects",
    "biolink:regulates": "regulates",
    "biolink:disrupts": "disrupts",
    "biolink:interacts_with": "interacts with",
    "biolink:physically_interacts_with": "physically interacts with",
    "biolink:directly_physically_interacts_with": "directly physically interacts with",
    "biolink:indirectly_physically_interacts_with": "indirectly physically interacts with",
    "biolink:binds": "binds",
    "biolink:genetically_interacts_with": "genetically interacts with",
    "biolink:pharmacologically_interacts_with": "pharmacologically interacts with",
    # Gene -> disease. "is associated with" still reads well after subject qualifiers
    # ("increased expression of A is associated with B"), unlike "is a gene associated with".
    "biolink:gene_associated_with_condition": "is associated with",
    "biolink:condition_associated_with_gene": "is associated with",
    "biolink:genetically_associated_with": "is genetically associated with",
    "biolink:associated_with": "is associated with",
    "biolink:genetic_association": "has a genetic association with",
    "biolink:associated_with_likelihood_of": "is associated with the likelihood of",
    "biolink:associated_with_increased_likelihood_of": (
        "is associated with an increased likelihood of"
    ),
    "biolink:associated_with_decreased_likelihood_of": (
        "is associated with a decreased likelihood of"
    ),
    "biolink:correlated_with": "is correlated with",
    "biolink:positively_correlated_with": "is positively correlated with",
    "biolink:negatively_correlated_with": "is negatively correlated with",
    "biolink:biomarker_for": "is a biomarker for",
    "biolink:contributes_to": "contributes to",
    "biolink:causes": "causes",
    # Gene -> phenotype.
    "biolink:has_phenotype": "has the phenotype",
    # Other descendants of affects, and the likelihood predicates beside them.
    "biolink:exacerbates_condition": "exacerbates",
    "biolink:has_adverse_event": "has the adverse event",
    "biolink:has_side_effect": "has the side effect",
    "biolink:affects_likelihood_of": "affects the likelihood of",
    "biolink:predisposes_to_condition": "predisposes to",
    "biolink:promotes_condition": "promotes",
    "biolink:related_to": "is related to",
}

# Statement qualifiers, read after the object in this order: the mechanism qualifies the
# predicate, so it comes first, then where, in whom and when. Any other qualifier is appended
# as "(<slot name>: <value>)".
CONTEXT_PHRASES: dict[str, str] = {
    "causal_mechanism_qualifier": "through {}",
    "anatomical_context_qualifier": "in {}",
    "species_context_qualifier": "in {}",
    "population_context_qualifier": "in {}",
    "sex_qualifier": "in {}",
    "disease_context_qualifier": "in the context of {}",
    "stage_qualifier": "during {}",
    "temporal_context_qualifier": "during {}",
    "temporal_interval_qualifier": "within {}",
    "onset_qualifier": "with onset {}",
    "frequency_qualifier": "with frequency {}",
    "severity_qualifier": "with severity {}",
    "association_basis_qualifier": "on a {} basis",
    "statistical_significance_qualifier": "({})",
}

_CURIE = re.compile(r"^[A-Za-z][\w.\-]*:\S+$")


def _present(value: Any) -> bool:
    return value is not None and value != "" and value != []


def qualifier_value(value: Any) -> str:
    """A qualifier value in words: an enum value with underscores as spaces ('activity_or_
    abundance' -> 'activity or abundance'), a CURIE as it is, a list joined with 'and'."""
    if isinstance(value, list | tuple):
        return " and ".join(qualifier_value(v) for v in value)
    text = str(value)
    return text if _CURIE.match(text) else text.replace("_", " ")


def predicate_phrase(predicate: str) -> str:
    """The hand-written phrase for a predicate, or its Biolink name ('has adverse event')."""
    curie = predicate if ":" in predicate else f"biolink:{predicate}"
    if curie in PREDICATE_PHRASES:
        return PREDICATE_PHRASES[curie]
    try:
        name = biolink.element_name(curie)
    except (LookupError, ValueError):
        name = None
    return name or curie.removeprefix("biolink:").replace("_", " ")


def _article(noun: str) -> str:
    return "an" if noun[:1].lower() in "aeiou" else "a"


def _compose(name: str, qualifiers: dict[str, Any], side: str, used: set[str]) -> str:
    """The subject or object with its qualifiers, from the core concept outwards."""

    def take(kind: str) -> str | None:
        slot = f"{side}_{kind}_qualifier"
        if slot not in qualifiers:
            return None
        used.add(slot)
        return qualifier_value(qualifiers[slot])

    text = name
    if derivative := take("derivative"):
        text = f"{_article(derivative)} {derivative} of {text}"
    if part := take("part"):
        text = f"the {part} of {text}"
    if form := take("form_or_variant"):
        # The enum's values already end in 'form' ('mutant_form'); free text may not.
        noun = form if form.endswith("form") else f"{form} form"
        text = f"{_article(noun)} {noun} of {text}"
    if specialization := take("specialization"):
        text = f"{text} (specifically {specialization})"
    aspect, direction = take("aspect"), take("direction")
    if aspect and direction:
        text = f"{direction} {aspect} of {text}"
    elif aspect:
        text = f"the {aspect} of {text}"
    elif direction:
        text = f"{direction} {text}"
    for kind in ("activity", "process", "context"):
        if value := take(kind):
            text = f"{text} in {value}"
    return text


def sentence(statement: records.Statement, names: dict[str, str]) -> str:
    """The statement in English, e.g. 'imatinib causes decreased activity of ABL1'.

    `names` maps node ids to names; a node without one is called by its id. No final period,
    so the sentence can be quoted or embedded as it is.
    """
    qualifiers = {k: v for k, v in statement.qualifiers.items() if _present(v)}
    used = {"qualified_predicate"}
    subject = _compose(
        names.get(statement.subject) or statement.subject, qualifiers, "subject", used
    )
    predicate = predicate_phrase(qualifiers.get("qualified_predicate") or statement.predicate)
    object_ = _compose(names.get(statement.object) or statement.object, qualifiers, "object", used)
    words = [subject, predicate, object_]
    for slot, template in CONTEXT_PHRASES.items():
        if slot in qualifiers:
            used.add(slot)
            words.append(template.format(qualifier_value(qualifiers[slot])))
    for slot in sorted(set(qualifiers) - used):
        words.append(f"({slot.replace('_', ' ')}: {qualifier_value(qualifiers[slot])})")
    return " ".join(words)


def write_sentences(work: Path) -> int:
    """Write `sentences.jsonl` from `statements.jsonl` and `entities.jsonl`, by statement id.
    Returns the number of sentences."""
    statements: dict[str, records.Statement] = {}
    for statement in records.read_records(work / records.STATEMENTS, records.Statement):
        statements.setdefault(statement.statement_id, statement)
    names = {
        entity.id: entity.name
        for entity in records.read_records(work / records.ENTITIES, records.Entity)
        if entity.name
    }
    rows = (
        records.SentenceRecord(statement_id=sid, sentence=sentence(statements[sid], names))
        for sid in sorted(statements)
    )
    return records.write_records(work / records.SENTENCES, rows)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Step 4 has no options of its own."""


def run(args: argparse.Namespace) -> int:
    work = Path(args.work)
    for name in (records.STATEMENTS, records.ENTITIES):
        if not (work / name).exists():
            print(
                f"sentences: {work / name} not found; run `kgb-augment select` first",
                file=sys.stderr,
            )
            return 1
    n = write_sentences(work)
    print(f"sentences: wrote {n} sentences to {work / records.SENTENCES}")
    return 0
