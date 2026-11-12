"""Step 3: an LLM reads each pair's abstracts and writes down the claims they make about it.

One request per unordered node pair, with its abstracts in chunks of `--abstracts-per-call` (at
most config.ABSTRACTS_PER_CALL): the pair's nodes go as X and Y, with names, synonyms and
categories, and the graph's edges are never sent. So a pair's claims serve every edge between
the pair, from any graph, now or later; step 6 refines each edge from them (`refine.py`).

For each abstract the model gives a study design and zero or more claims: an abstract with no
claims states nothing about the pair. A claim's statement names X or Y as its subject, a
predicate and its qualifiers, and code stores it with the pair's CURIEs in their place and its
object made explicit (`claim_id` hashes the claim exactly as stored, so a stored claim is never
rewritten). Species come from an enum and are stored as NCBITaxon terms (config.SPECIES_TAXA);
anatomy is stored as the abstract names it, for the ground step to resolve.

The model replies in strict structured output. Each pair's schema allows the predicates of the
scope rules it matches (`predicates_of`): theirs, the ones Biolink puts below them, and their
`opposite_of` partners, each the way round its domain and range read from the rule's subject to
its object: one that fits only the other way round is offered as its inverse, and one that fits
neither way is left out, so none is offered the wrong way round. Code then checks what a schema
can't say (`check_claim`), on each statement turned the way round Biolink
reads it (`refine.directed`; a claim whose predicate fits neither way fails): the qualifier
slots and values the predicate's association allows, `causes` only with an aspect or a
direction, `negative_finding_type` on negative claims only, and `also_supports` entries that
add something; and that the evidence sentence is in the abstract (`quotes.quote_found`). A claim
failing a check is kept in the cache but counts for nothing: the checks go to the
`claim_checks` table, recomputed on every run for the run's pairs, since nothing versions them.
Last, the quote repair pass (`repair.py`) asks a stronger model for the evidence sentences of
claims that pass every other check but whose sentence wasn't found.

A rerun asks only for the abstracts with no answer yet, and a new prompt, model or effort starts
fresh: the prompt version hashes the prompt file (with the claim rules it includes) together
with FORMAT_VERSION, the version of the code that writes the user message, every scope rule's
reply schema and the reasoning effort. A pair with a failed call keeps the answers of its other
calls; its edges stay unchanged until a run has answered all of its abstracts.

The calls run in a thread pool; everything else, including every cache write and every
Biolink lookup, stays on the main thread, since neither SQLite connections nor the toolkit are
shared safely between threads.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, get_args

from pubmed_evidence import biolink, config, llm, records, refine, repair, scope, sentences
from pubmed_evidence.biolink import QualifierSpec
from pubmed_evidence.cache import (
    AbstractClaimsRow,
    ClaimCheckRow,
    LabelCache,
    PubMedCache,
    canonical,
    claim_id,
)
from pubmed_evidence.graphio import chunks, pair_id, pair_nodes, read_jsonl, write_jsonl
from pubmed_evidence.quotes import quote_found
from pubmed_evidence.records import Entity, Pair
from pubmed_evidence.scope import Rule
from shared import schema
from shared.pubmed import Abstract, PubmedRecord, default_cache_path
from shared.schema import ClaimRole, NegativeFindingType, Polarity, StudyDesign

# Bump when the user message changes shape: it is part of the prompt version.
FORMAT_VERSION = "label-message-5"

MAX_SYNONYMS_SHOWN = 12
# The dry run's cost estimate assumes this many output tokens per call, reasoning included. A
# first guess for a chunk of abstracts with a few claims; the tuning sample measures it.
ESTIMATED_OUTPUT_TOKENS = 2000
# Azure OpenAI caches a prompt prefix of at least this many tokens.
CACHED_PREFIX_TOKENS = 1024
# Pairs whose claims are checked together, so a large run never holds every abstract at once.
CHECK_CHUNK = 100

SAMPLE_MD = "label_sample.md"
SAMPLE_JSONL = "label_sample.jsonl"
REQUEST_EXAMPLE = "label_request_example.txt"

# The reply's vocabularies, in the shared schema's order.
POLARITIES: tuple[str, ...] = get_args(Polarity)
ROLES: tuple[str, ...] = get_args(ClaimRole)
NEGATIVE_FINDINGS: tuple[str, ...] = get_args(NegativeFindingType)
DESIGNS: tuple[str, ...] = get_args(StudyDesign)
SPECIES: tuple[str, ...] = tuple(config.SPECIES_TAXA)
# The two nodes of a pair, as the request names them: X is the first in `pair_id` order.
SIDES = ("X", "Y")
# The one qualified predicate a claim may give: `causes`, under `affects`.
CAUSES = "causes"
# A qualifier slot the statement doesn't set. The slots take it rather than null: each nullable
# slot would be a union, and Anthropic's strict output allows only llm.MAX_UNION_PARAMETERS.
UNSET = "none"

# The step that writes each input file, for the error when one is missing.
_INPUTS = {
    records.PAIRS: "select",
    records.ENTITIES: "select",
    records.PUBMED: "pubmed",
}


def _warn(message: str) -> None:
    print(f"label: warning: {message}", file=sys.stderr)


def _bare(curie: str) -> str:
    return curie.removeprefix("biolink:")


# --- Scope rules and reply schemas ----------------------------------------------------------


def rules_of(work: Path) -> tuple[Rule, ...]:
    """The scope rules select used: the `--rules` file its report names, else the built-in
    ones. Raises scope.RulesError (a ValueError) for a rules file that can't be read."""
    path = Path(work) / records.SCOPE_REPORT
    report = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    rules_file = report.get("rules_file")
    return scope.load_rules(Path(rules_file)) if rules_file else scope.DEFAULT_RULES


def predicates_of(rules: Sequence[Rule], ids: Iterable[str]) -> tuple[str, ...]:
    """The predicates a claim may use for a pair the rules `ids` matched, as CURIEs, sorted:
    each rule's predicates, every predicate Biolink puts below them (mixins included), and their
    `opposite_of` partners, each as Biolink reads it from the rule's subject to its object
    (`rule_predicate`). Predicates a skip rule drops whatever the categories (literature
    co-occurrence, which is under `associated_with`) are left out: they never make a claim."""
    wanted = set(ids)
    found: set[str] = set()
    for rule in rules:
        if rule.id in wanted and rule.tier != "skip":
            named: set[str] = set()
            for predicate in rule.predicates:
                named |= biolink.descendants(predicate)
            named |= {b for a, b in biolink.opposites() if a in named}
            found |= {p for n in named if (p := rule_predicate(rule, n))}
    dropped: set[str] = set()
    for rule in rules:
        if rule.tier == "skip" and not rule.subject and not rule.object:
            for predicate in rule.predicates:
                dropped |= biolink.descendants(predicate)
    return tuple(sorted(found - dropped))


def rule_predicate(rule: Rule, predicate: str) -> str | None:
    """The predicate a claim uses for `predicate` between the rule's subject and object
    categories (`biolink.oriented`), so that a reply schema never offers one the wrong way
    round: one defined the other way round becomes its inverse (`condition_associated_with_gene`
    is `gene_associated_with_condition` for gene-disease), one Biolink doesn't take as
    canonical becomes its canonical inverse, and one that fits neither way (`has_adverse_event`
    or `enables` for chemical-gene) is None. A symmetric rule reads either way round."""
    sides = [(rule.subject, rule.object)]
    if rule.symmetric:
        sides.append((rule.object, rule.subject))
    for subject, object in sides:
        read = biolink.oriented(subject, predicate, object)
        if read is not None:
            return read[0]
    return None


def _enum(values: Iterable[str]) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)}


@cache
def reply_schema(predicates: tuple[str, ...]) -> llm.Schema:
    """The label reply's schema for a pair whose claims may use `predicates` (CURIEs; the
    schema names them without 'biolink:'). Abstracts are named by position, up to the most a
    call sends. A statement's qualifiers are one object with every enum-valued qualifier slot
    Biolink has, each its values or UNSET (not nullable, which would make 13 unions, and
    twice that with `also_supports`), built once from the pinned Biolink; which apply is checked
    in code. Statements are defined once (`$defs`), for a claim's own and its `also_supports`."""
    qualifiers = llm.strict_object(
        {
            slot: _enum([*values, UNSET])
            for slot, values in biolink.qualifier_values().items()
            if slot != "qualified_predicate"
        }
    )
    statement = llm.strict_object(
        {
            "subject": _enum(SIDES),
            "predicate": _enum(_bare(p) for p in predicates),
            "qualified_predicate": llm.nullable(_enum([CAUSES])),
            "qualifiers": qualifiers,
        }
    )
    reference = {"$ref": "#/$defs/statement"}
    context = llm.strict_object(
        {
            "species": llm.nullable(_enum(SPECIES)),
            "anatomy": llm.nullable({"type": "string"}),
            "notes": llm.nullable({"type": "string"}),
        }
    )
    claim = llm.strict_object(
        {
            "polarity": _enum(POLARITIES),
            "statement": reference,
            "also_supports": {"type": "array", "items": reference},
            "claim_role": _enum(ROLES),
            "hedged": {"type": "boolean"},
            "negative_finding_type": llm.nullable(_enum(NEGATIVE_FINDINGS)),
            "context": context,
            "evidence_sentence": {"type": "string"},
        }
    )
    abstract = llm.strict_object(
        {
            "abstract": llm.positions("A", config.ABSTRACTS_PER_CALL),
            "study_design": _enum(DESIGNS),
            "claims": {"type": "array", "items": claim},
        }
    )
    body = llm.strict_object({"abstracts": {"type": "array", "items": abstract}})
    body["$defs"] = {"statement": statement}
    return llm.Schema("label_reply", body)


def rule_schemas(rules: Sequence[Rule]) -> dict[str, llm.Schema]:
    """Each scope rule's reply schema, by rule id (skip rules have none). A pair matching
    several rules gets the schema of their predicates together, which these determine."""
    return {
        rule.id: reply_schema(predicates_of(rules, [rule.id]))
        for rule in rules
        if rule.tier != "skip"
    }


def label_prompt(
    prompt: Path = config.LABEL_PROMPT,
    model: str = config.BULK_MODEL,
    reasoning_effort: str | None = None,
    rules: Sequence[Rule] = scope.DEFAULT_RULES,
) -> tuple[str, str]:
    """(system prompt, prompt version) of a label run with this prompt file, model and scope
    rules. The version covers the prompt with its includes, FORMAT_VERSION, every rule's reply
    schema and the run's effort (`llm.run_effort`), so claims made at one effort are never
    reused at another. Anything that needs the version of a label run can call this."""
    effort = llm.run_effort(model, reasoning_effort)
    schemas = "\0".join(s.text() for _, s in sorted(rule_schemas(rules).items()))
    extra = f"{FORMAT_VERSION}\0{schemas}\0effort={effort}"
    return llm.load_prompt(prompt, extra=extra)


# --- Inputs and jobs ------------------------------------------------------------------------


@dataclass
class Inputs:
    pairs: list[Pair]
    entities: dict[str, Entity]
    pubmed: dict[str, PubmedRecord]
    rules: tuple[Rule, ...]


def read_inputs(work: Path) -> Inputs:
    """The work files of steps 1 and 2, and the scope rules select used. Raises
    FileNotFoundError naming the step to run first."""
    for name, step in _INPUTS.items():
        if not (work / name).exists():
            raise FileNotFoundError(f"{work / name} is missing: run `kgb-augment {step}` first")
    return Inputs(
        pairs=list(records.read_records(work / records.PAIRS, Pair)),
        entities={e.id: e for e in records.read_records(work / records.ENTITIES, Entity)},
        pubmed={p.pair_id: p for p in records.read_records(work / records.PUBMED, PubmedRecord)},
        rules=rules_of(work),
    )


def read_pairs(path: Path) -> list[str]:
    """The pairs a pairs file names (`evaluate --draw` writes one), in order, once each: one
    JSON object per line with a `pair_id`, 'a|b' either way round; other keys are ignored.
    Raises ValueError for a line that names none."""
    out: dict[str, None] = {}
    try:
        for n, line in enumerate(read_jsonl(path), 1):
            pid = line.get("pair_id") if isinstance(line, dict) else None
            if not isinstance(pid, str) or pid.count("|") != 1:
                raise ValueError(f"{path}:{n}: a line needs a pair_id ('a|b')")
            out[pair_id(*pid.split("|"))] = None
    except json.JSONDecodeError as e:
        raise ValueError(f"{path}: not JSONL ({e})") from e
    return list(out)


def pair_pmids(record: PubmedRecord, texts: Mapping[str, Abstract]) -> list[str]:
    """The PMIDs step 3 asks about for a pair: those step 2 found whose abstract the PubMed
    cache (`texts`) has with text, once each, in order. A pair is fully labeled when each has an
    answer."""
    pmids = dict.fromkeys(p.pmid for p in record.pmids)
    return [p for p in pmids if p in texts and texts[p].has_text]


@dataclass
class PairJob:
    """What a pair's calls need, except the abstract texts, which are loaded only when its
    calls are made so that a large run doesn't hold every abstract in memory."""

    pair_id: str
    entities: list[Entity]  # X, then Y
    synonyms: dict[str, list[str]]
    mesh: dict[str, list[str]]
    rules: tuple[str, ...]
    predicates: tuple[str, ...]
    pmids: list[str] = field(default_factory=list)

    @property
    def nodes(self) -> dict[str, str]:
        """'X' and 'Y' -> the node ids they stand for."""
        return {side: e.id for side, e in zip(SIDES, self.entities, strict=True)}

    @property
    def categories(self) -> dict[str, list[str]]:
        return {e.id: list(e.category) for e in self.entities}

    @property
    def schema(self) -> llm.Schema:
        return reply_schema(self.predicates)


def build_jobs(inputs: Inputs, pubmed: PubMedCache) -> tuple[list[PairJob], Counter]:
    """A job per pair with abstracts to read, in `pairs.jsonl` order, and the counts of what
    was skipped."""
    skipped: Counter = Counter()
    known = {r.id for r in inputs.rules if r.tier != "skip"}
    jobs: list[PairJob] = []
    for pair in inputs.pairs:
        record = inputs.pubmed.get(pair.pair_id)
        if record is None:
            skipped["pairs without a PubMed record"] += 1
            continue
        if not pair.rules or not set(pair.rules) <= known:
            skipped["pairs whose scope rules aren't known"] += 1
            continue
        found = list(dict.fromkeys(p.pmid for p in record.pmids))
        texts = pubmed.get_abstracts(found)
        missing = sum(p not in texts for p in found)
        if missing:
            skipped["abstracts missing from the PubMed cache"] += missing
        pmids = pair_pmids(record, texts)
        if not pmids:
            skipped["pairs without abstracts"] += 1
            continue
        entities = [inputs.entities.get(n) or Entity(id=n) for n in pair_nodes(pair.pair_id)]
        synonyms = {
            e.id: (record.synonyms.get(e.id) if record.synonyms.get(e.id) else e.synonyms)
            for e in entities
        }
        mesh = {e.id: list(record.mesh.get(e.id, [])) for e in entities}
        rules = tuple(pair.rules)
        predicates = predicates_of(inputs.rules, rules)
        jobs.append(PairJob(pair.pair_id, entities, synonyms, mesh, rules, predicates, pmids))
    return jobs, skipped


@dataclass
class Request:
    """One call: some of a pair's abstracts, A1, A2, ... in this order."""

    job: PairJob
    pmids: list[str]

    @property
    def pair_id(self) -> str:
        return self.job.pair_id


def plan_requests(
    job: PairJob, answered: set[tuple[str, str]], per_call: int = config.ABSTRACTS_PER_CALL
) -> list[Request]:
    """The calls a pair still needs: its abstracts with no answer yet, at most `per_call` to a
    call."""
    todo = [p for p in job.pmids if (job.pair_id, p) not in answered]
    return [Request(job, list(chunk)) for chunk in chunks(todo, per_call)]


def calls_answered(jobs: Iterable[PairJob], answered: set[tuple[str, str]], per_call: int) -> int:
    """The calls the pairs' abstracts answered before this run took, at `per_call` to a call.
    With this run's calls, they are the calls the pairs need, which the 5% rule weighs failed
    calls against (`llm.too_many_failed`)."""
    return sum(
        len(list(chunks([p for p in job.pmids if (job.pair_id, p) in answered], per_call)))
        for job in jobs
    )


# --- Qualifiers -----------------------------------------------------------------------------

# Subject and object qualifiers (aspect, direction, form or variant, part, derivative) describe
# a gene, gene product or chemical. Biolink's mixins also allow them on diseases and phenotypes
# ("decreased expression of asthma"), so they apply only to these.
QUALIFIABLE = ("biolink:GeneOrGeneProduct", "biolink:ChemicalEntity")
DISEASE_OR_PHENOTYPE = "biolink:DiseaseOrPhenotypicFeature"
# The slots that let `causes` in (`causes` only with an aspect or a direction).
CAUSAL_SLOTS = ("_aspect_qualifier", "_direction_qualifier")


def _side_qualified(qualifiers: Iterable[str]) -> bool:
    return any(k.startswith(("subject_", "object_")) for k in qualifiers)


class Qualifiers:
    """The qualifier slots a statement takes and the check of a claim's statements, memoized:
    many share an association, and bmt lookups are slow.

    A statement takes the enum-valued slots its association allows, minus those that don't fit
    its entities. Open-ranged ones (an anatomical, species or disease context) aren't in the
    reply: species and anatomy come in a claim's context. A `subject_*` or `object_*` slot needs
    a gene, gene product or chemical on that side; a chemical and a disease or phenotype
    (treats, contraindicated_in, ...) take none; and `qualified_predicate` comes only with a
    subject or object slot, as Biolink reads it.
    """

    def __init__(self) -> None:
        self._offered: dict[tuple, dict[str, QualifierSpec]] = {}
        self._errors: dict[tuple, list[str]] = {}

    def offered(
        self, subject: Sequence[str], predicate: str, object: Sequence[str]
    ) -> dict[str, QualifierSpec]:
        """The slots a statement with these subject categories, predicate and object
        categories takes, by name, sorted."""
        key = (tuple(subject), predicate, tuple(object))
        if key not in self._offered:
            allowed = biolink.allowed_qualifiers(list(subject), predicate, list(object))
            self._offered[key] = self._fitting(dict(sorted(allowed.items())), subject, object)
        return self._offered[key]

    @staticmethod
    def _fitting(
        allowed: dict[str, QualifierSpec], subject: Sequence[str], object: Sequence[str]
    ) -> dict[str, QualifierSpec]:
        if biolink.is_a(list(subject), "biolink:ChemicalEntity") and biolink.is_a(
            list(object), DISEASE_OR_PHENOTYPE
        ):
            return {}
        fits = {
            "subject": biolink.is_a(list(subject), QUALIFIABLE),
            "object": biolink.is_a(list(object), QUALIFIABLE),
        }
        out = {
            slot: spec
            for slot, spec in allowed.items()
            if spec.values and fits.get(slot.split("_", 1)[0], True)
        }
        if not _side_qualified(out):
            out.pop("qualified_predicate", None)
        return out

    def errors(
        self, statement: Mapping[str, Any], categories: Mapping[str, Sequence[str]]
    ) -> list[str]:
        """What is wrong with a stored claim statement's qualifiers, in words; [] if nothing.
        `categories` are the pair's nodes' Biolink categories."""
        subject = tuple(categories.get(statement["subject"]) or ())
        object = tuple(categories.get(statement["object"]) or ())
        predicate = statement["predicate"]
        slots = dict(statement.get("qualifiers") or {})
        if statement.get("qualified_predicate"):
            slots["qualified_predicate"] = statement["qualified_predicate"]
        key = (subject, predicate, object, canonical(slots))
        if key not in self._errors:
            self._errors[key] = self._check(slots, subject, predicate, object)
        return self._errors[key]

    def _check(
        self, slots: dict[str, Any], subject: tuple, predicate: str, object: tuple
    ) -> list[str]:
        offered = self.offered(subject, predicate, object)
        name = _bare(predicate)
        errors = []
        for slot, value in sorted(slots.items()):
            spec = offered.get(slot)
            if spec is None:
                errors.append(f"{slot} doesn't apply to {name} between these entities")
            elif value not in (spec.values or ()):
                errors.append(f"{value!r} is not an allowed value of {slot} for {name}")
        if "qualified_predicate" in slots and not any(k.endswith(CAUSAL_SLOTS) for k in slots):
            errors.append("qualified_predicate causes needs an aspect or a direction")
        if slots and not errors:
            errors += biolink.validate_qualifiers(slots, list(subject), predicate, list(object))
        return list(dict.fromkeys(errors))


# --- The user message -----------------------------------------------------------------------

# Value lists up to this long are written next to their slot; longer ones once, by name.
INLINE_VALUES = 3


def _values(slot: str, spec: QualifierSpec) -> tuple[str, ...]:
    values = tuple(spec.values or ())
    return tuple(_bare(v) for v in values) if slot == "qualified_predicate" else values


def qualifier_lines(job: PairJob, qualifiers: Qualifiers) -> list[str]:
    """The qualifier sets the pair's predicates take, by which node is the subject (predicates
    sharing a set share its id), and, once each, the long value lists they refer to by enum
    name: the aspect and mechanism enums alone run to well over a hundred values."""
    groups: dict[tuple, tuple[str, dict[str, QualifierSpec], list[str]]] = {}
    for i, side in enumerate(SIDES):
        subject, object = job.entities[i], job.entities[1 - i]
        for predicate in job.predicates:
            specs = qualifiers.offered(subject.category, predicate, object.category)
            if not specs:
                continue
            key = (side, tuple((slot, _values(slot, spec)) for slot, spec in specs.items()))
            if key not in groups:
                groups[key] = (f"Q{len(groups) + 1}", specs, [])
            groups[key][2].append(_bare(predicate))
    if not groups:
        return ["# Qualifiers", "", "None of the predicates take qualifiers for this pair.", ""]
    named: dict[tuple[str, ...], str] = {}

    def values_of(slot: str, spec: QualifierSpec) -> str:
        values = _values(slot, spec)
        if len(values) <= INLINE_VALUES:
            return ", ".join(values)
        if values not in named:
            base = spec.range or f"{slot} values"
            name, n = base, 2
            while name in named.values():
                name, n = f"{base} ({n})", n + 1
            named[values] = name
        return f"one of {named[values]}"

    lines = ["# Qualifiers", ""]
    for (side, _), (qid, specs, predicates) in groups.items():
        listed = predicates[0] if len(predicates) == 1 else ", ".join(predicates[:-1])
        if len(predicates) > 1:
            listed += f" or {predicates[-1]}"
        lines.append(f"{qid}, with {side} as the subject of {listed}:")
        lines += [f"- {slot}: {values_of(slot, spec)}" for slot, spec in specs.items()]
        lines.append("")
    lines += ["A predicate or subject not listed takes no qualifiers.", ""]
    if named:
        lines += ["# Qualifier values", ""]
        lines += [f"- {name}: {', '.join(values)}" for values, name in named.items()]
        lines.append("")
    return lines


def build_message(
    request: Request, abstracts: dict[str, Abstract], qualifier_text: list[str]
) -> str:
    """The user message of one call: the entities X and Y, the predicates and qualifiers their
    claims may use, then the abstracts. Its layout is FORMAT_VERSION."""
    job = request.job
    lines = ["# Entities", ""]
    for side, e in zip(SIDES, job.entities, strict=True):
        name = e.name or e.id
        lines.append(f"{side}: {name} ({e.id})")
        # The most specific category: KGX lists every ancestor and mixin after it.
        category = e.category[0].removeprefix("biolink:") if e.category else "unknown"
        lines.append(f"  category: {category}")
        synonyms = [
            s
            for s in dict.fromkeys(t.strip() for t in job.synonyms.get(e.id, []))
            if s and s.lower() != name.lower()
        ][:MAX_SYNONYMS_SHOWN]
        if synonyms:
            lines.append(f"  synonyms: {'; '.join(synonyms)}")
        if job.mesh.get(e.id):
            lines.append(f"  MeSH headings: {'; '.join(job.mesh[e.id])}")
    lines += ["", "# Predicates", "", ", ".join(_bare(p) for p in job.predicates), ""]
    lines += qualifier_text
    lines += ["# Abstracts", ""]
    for n, pmid in enumerate(request.pmids, 1):
        a = abstracts[pmid]
        year = f", {a.year}" if a.year else ""
        lines.append(f"## A{n} (PMID:{pmid}{year})")
        if a.publication_types:
            lines.append(f"Publication types: {'; '.join(a.publication_types)}")
        lines += [
            f"Title: {' '.join(a.title.split())}",
            f"Abstract: {a.abstract.strip()}",
            "",
        ]
    lines.append(
        f"Give the study design and the claims about X and Y of every abstract "
        f"({_ids('A', len(request.pmids))})."
    )
    return "\n".join(lines)


def _ids(prefix: str, n: int) -> str:
    return f"{prefix}1" if n == 1 else f"{prefix}1 to {prefix}{n}"


# --- Reading the reply ----------------------------------------------------------------------


def _text(value: Any) -> str | None:
    return " ".join(value.split()) or None if isinstance(value, str) else None


def stored_statement(item: Mapping[str, Any], nodes: Mapping[str, str]) -> dict[str, Any]:
    """A reply's statement as a claim stores it: the shared schema's `Statement` fields but
    `negated` (a claim's polarity carries the sign), with the pair's CURIEs for X and Y, the
    object explicit, predicates as 'biolink:' CURIEs and only the qualifiers set."""
    subject = nodes[item["subject"]]
    object = next((n for side, n in nodes.items() if side != item["subject"]), subject)
    qualified = item["qualified_predicate"]
    return {
        "subject": subject,
        "predicate": f"biolink:{item['predicate']}",
        "object": object,
        "qualified_predicate": f"biolink:{qualified}" if qualified else None,
        "qualifiers": {
            k: v for k, v in sorted(item["qualifiers"].items()) if v not in (None, UNSET)
        },
    }


def stored_claim(item: Mapping[str, Any], nodes: Mapping[str, str]) -> dict[str, Any]:
    """A reply's claim as `abstract_claims` stores it (`claim_id` hashes this): its statements
    by `stored_statement` (`also_supports` once each, in order), the species as its NCBITaxon
    term, the anatomy as the abstract names it, and texts with their whitespace collapsed."""
    also = {}
    for entry in item["also_supports"]:
        statement = stored_statement(entry, nodes)
        also.setdefault(canonical(statement), statement)
    context = item["context"]
    species = context["species"]
    return {
        "polarity": item["polarity"],
        "statement": stored_statement(item["statement"], nodes),
        "also_supports": list(also.values()),
        "claim_role": item["claim_role"],
        "hedged": bool(item["hedged"]),
        "negative_finding_type": item["negative_finding_type"],
        "context": {
            "species": config.SPECIES_TAXA.get(species) if species else None,
            "anatomy": _text(context["anatomy"]),
            "notes": _text(context["notes"]),
        },
        "evidence_sentence": _text(item["evidence_sentence"]) or "",
    }


@dataclass
class Parsed:
    rows: list[AbstractClaimsRow]
    problems: Counter


def parse_reply(
    reply: dict[str, Any],
    request: Request,
    *,
    prompt_version: str,
    model: str,
    answered_by: str | None = None,
) -> Parsed:
    """Each abstract's answer from the model's reply, which fits the pair's `reply_schema`.

    Abstracts are named by position (`A1`); one beyond the request's, or one answered twice,
    raises ValueError, so the call counts as failed, nothing of it is cached, and the next run
    asks again. An abstract the reply leaves out gets no answer, and the next run asks for it
    again; that is counted. A claim given twice in one answer is kept once. `answered_by` is
    the model that answered, on every row.
    """
    pmids = {f"A{i}": p for i, p in enumerate(request.pmids, 1)}
    problems: Counter = Counter()
    found: dict[str, dict[str, Any]] = {}
    beyond: Counter = Counter()
    twice: Counter = Counter()
    for item in reply["abstracts"]:
        pmid = pmids.get(item["abstract"])
        if pmid is None:
            beyond[item["abstract"]] += 1
        elif pmid in found:
            twice[item["abstract"]] += 1
        else:
            found[pmid] = item
    for what, counted in (("positions beyond the request", beyond), ("answered twice", twice)):
        if counted:
            listed = ", ".join(f"{p} ({n}x)" if n > 1 else p for p, n in sorted(counted.items()))
            raise ValueError(f"{what}: {listed}")
    rows = []
    for pmid in request.pmids:
        item = found.get(pmid)
        if item is None:
            problems["abstracts left out of the reply"] += 1
            continue
        claims: dict[str, dict[str, Any]] = {}
        for c in item["claims"]:
            claim = stored_claim(c, request.job.nodes)
            cid = claim_id(claim)
            if cid in claims:
                problems["claims given twice"] += 1
            claims.setdefault(cid, claim)
        rows.append(
            AbstractClaimsRow(
                request.pair_id,
                pmid,
                prompt_version,
                model,
                item["study_design"],
                list(claims.values()),
                answered_by=answered_by,
            )
        )
    return Parsed(rows, problems)


# --- Checking the claims --------------------------------------------------------------------


def statement_of(stored: Mapping[str, Any]) -> schema.Statement:
    """A stored claim statement as the shared schema's `Statement`."""
    return schema.Statement.model_validate(stored)


def check_claim(
    claim: Mapping[str, Any],
    categories: Mapping[str, Sequence[str]],
    qualifiers: Qualifiers,
    lattice: refine.Lattice | None = None,
) -> list[str]:
    """What fails a stored claim, in words; [] if nothing does. The sentence check is apart
    (`sentence_found`). Each statement is checked the way round Biolink reads it
    (`refine.directed`), as step 6 will read it: its predicate must fit the pair's nodes
    by its domain and range, one way round or the other. Then checked: each statement's
    qualifier slots and values (`Qualifiers`), `negative_finding_type` only on a negative claim,
    and each `also_supports` entry adding something: not already supported by a positive
    claim's own statement, and not contradicting a negative claim (a statement that entails the
    one it denies)."""
    lattice = lattice or refine.Lattice()
    errors: list[str] = []
    read: list[schema.Statement | None] = []
    for i, entry in enumerate([claim["statement"], *claim["also_supports"]]):
        where = f"also_supports[{i - 1}]: " if i else ""
        statement = refine.directed(statement_of(entry), categories)
        read.append(statement)
        if statement is None:
            errors.append(f"{where}{misfit(entry, categories)}")
        else:
            stored = statement.model_dump(exclude={"negated"})
            errors += [f"{where}{e}" for e in qualifiers.errors(stored, categories)]
    positive = claim["polarity"] == "positive"
    if positive and claim["negative_finding_type"] is not None:
        errors.append("negative_finding_type is for negative claims only")
    own = read[0]
    for i, also in enumerate(read[1:]):
        if own is None or also is None:
            continue
        if positive and lattice.entails(own, also):
            errors.append(f"also_supports[{i}] is already supported by the claim's statement")
        if not positive and lattice.entails(also, own):
            errors.append(f"also_supports[{i}] contradicts the negative claim")
    return errors


def misfit(statement: Mapping[str, Any], categories: Mapping[str, Sequence[str]]) -> str:
    """Why a stored statement's predicate fits its nodes neither way round, in words."""

    def kind(node: str) -> str:
        cats = biolink.most_specific(categories.get(node) or ())
        return " or ".join(map(_bare, cats)) or "unknown"

    return (
        f"{_bare(statement['predicate'])} fits neither way between "
        f"{kind(statement['subject'])} and {kind(statement['object'])} by its Biolink domain "
        "and range"
    )


def sentence_found(claim: Mapping[str, Any], abstract: Abstract | None) -> bool:
    """Whether the claim's evidence sentence is in the abstract's title or text (case,
    whitespace and '...' joins allowed, as `quotes.quote_found` reads them)."""
    if abstract is None:
        return False
    return quote_found(claim["evidence_sentence"], abstract.title, abstract.abstract)


@dataclass
class Checked:
    """What the checks found in a run's answers for some pairs."""

    abstracts: int = 0
    no_claim: int = 0
    claims: int = 0
    valid: int = 0
    sentences_not_found: int = 0  # of the valid claims
    negatives_without_type: int = 0
    errors: Counter = field(default_factory=Counter)

    @property
    def invalid(self) -> int:
        return self.claims - self.valid


def check_pairs(
    jobs: Sequence[PairJob],
    labels: LabelCache,
    pubmed: PubMedCache,
    qualifiers: Qualifiers,
    *,
    prompt_version: str,
    model: str,
) -> Checked:
    """Check every claim the run has for these pairs and store the checks, replacing earlier
    ones: `claim_checks` doesn't say which version of the checks made a row, so they're
    recomputed rather than trusted."""
    out = Checked()
    lattice = refine.Lattice()
    for chunk in chunks(list(jobs), CHECK_CHUNK):
        by_pair = {job.pair_id: job for job in chunk}
        answers = labels.abstract_claims(prompt_version, model, by_pair)
        texts = pubmed.get_abstracts(a.pmid for a in answers)
        rows = []
        for answer in sorted(answers, key=lambda a: (a.pair_id, a.pmid)):
            job = by_pair[answer.pair_id]
            out.abstracts += 1
            out.no_claim += not answer.claims
            for claim in answer.claims:
                errors = check_claim(claim, job.categories, qualifiers, lattice)
                found = sentence_found(claim, texts.get(answer.pmid))
                out.claims += 1
                out.valid += not errors
                out.sentences_not_found += not errors and not found
                out.negatives_without_type += (
                    claim["polarity"] == "negative" and claim["negative_finding_type"] is None
                )
                out.errors.update(errors)
                rows.append(
                    ClaimCheckRow(
                        answer.pair_id,
                        answer.pmid,
                        prompt_version,
                        model,
                        claim_id(claim),
                        not errors,
                        found,
                        errors,
                    )
                )
        labels.put_claim_checks(rows)
    return out


# --- Running the calls ----------------------------------------------------------------------


@dataclass
class _Stats:
    answered: int = 0  # abstracts
    claims: int = 0
    problems: Counter = field(default_factory=Counter)
    tally: llm.Tally = field(default_factory=llm.Tally)


def _calls(
    plans: list[tuple[PairJob, list[Request]]], pubmed: PubMedCache, qualifiers: Qualifiers
) -> Iterator[llm.Call]:
    """A call per request, with its pair's schema; a pair's abstracts are read only when its
    first call is made."""
    for job, requests in plans:
        if not requests:
            continue
        abstracts = pubmed.get_abstracts(p for r in requests for p in r.pmids)
        text = qualifier_lines(job, qualifiers)
        for request in requests:
            user = build_message(request, abstracts, text)
            yield llm.Call(request.pair_id, user, request, job.schema)


def _execute(
    plans: list[tuple[PairJob, list[Request]]],
    *,
    client: Any,
    system: str,
    labels: LabelCache,
    pubmed: PubMedCache,
    qualifiers: Qualifiers,
    model: str,
    prompt_version: str,
    workers: int,
) -> _Stats:
    stats = _Stats()

    def on_result(call: llm.Call, reply: Any, usage: llm.Usage) -> str | None:
        try:
            parsed = parse_reply(
                reply,
                call.context,
                prompt_version=prompt_version,
                model=model,
                answered_by=usage.model,
            )
        except ValueError as e:
            return f"unreadable reply: {e}"
        except Exception as e:
            return f"could not read the reply: {type(e).__name__}: {e}"
        labels.put_abstract_claims(parsed.rows)
        stats.answered += len(parsed.rows)
        stats.claims += sum(len(r.claims) for r in parsed.rows)
        stats.problems.update(parsed.problems)
        return None

    stats.tally = llm.run_calls(
        _calls(plans, pubmed, qualifiers),
        client,
        on_result,
        system=system,
        schema=None,
        cache=labels,
        step="label",
        model=model,
        prompt_version=prompt_version,
        total=sum(len(r) for _, r in plans),
        workers=workers,
    )
    return stats


# --- Sample files ---------------------------------------------------------------------------


def _found_note(valid: bool | None) -> str:
    return "" if valid is None else (" (found)" if valid else " (NOT FOUND)")


def _context_text(context: Mapping[str, Any]) -> str:
    parts = [f"{k} {v}" for k, v in context.items() if v]
    return "; ".join(parts)


def write_sample(
    work: Path,
    jobs: list[PairJob],
    labels: LabelCache,
    pubmed: PubMedCache,
    *,
    model: str,
    prompt_version: str,
    seed: int,
    source: Path | None = None,
) -> tuple[Path, Path]:
    """`label_sample.md`, what the agent reads to tune the prompt, and `label_sample.jsonl`, one
    line per (pair, PMID) with the abstract and its claims as stored, each with its id, checks
    and repaired sentence, for a curator to correct. A sentence that wasn't found shows its
    repair, if the repair pass made one. The pairs were chosen with `seed`, or are those the
    pairs file `source` names."""
    chosen = f"from {source}" if source is not None else f"chosen with seed {seed}"
    md = [
        "# Label sample",
        "",
        f"Model `{model}`, prompt `{prompt_version}`; {len(jobs)} pairs {chosen}.",
        "Each abstract lists its claims about the pair; texts are in label_sample.jsonl.",
        "",
    ]
    pair_ids = [j.pair_id for j in jobs]
    answers = {
        (a.pair_id, a.pmid): a for a in labels.abstract_claims(prompt_version, model, pair_ids)
    }
    checks = {
        (c.pair_id, c.pmid, c.claim_id): c
        for c in labels.claim_checks(prompt_version, model, pair_ids)
    }
    repairs = {
        (r.pair_id, r.pmid, r.claim_id): r
        for r in labels.claim_repairs(prompt_version, model, *repair.current(), pair_ids)
    }
    rows: list[dict[str, Any]] = []
    for n, job in enumerate(jobs, 1):
        names = {e.id: e.name or e.id for e in job.entities}
        x, y = job.entities
        md += [f"## {n}. {names[x.id]} and {names[y.id]}", "", f"Pair `{job.pair_id}`", ""]
        for side, e in zip(SIDES, job.entities, strict=True):
            synonyms = "; ".join(job.synonyms.get(e.id, [])[:MAX_SYNONYMS_SHOWN]) or "none"
            category = e.category[0].removeprefix("biolink:") if e.category else "unknown"
            md.append(f"- {side}: {names[e.id]} ({e.id}; {category}); synonyms: {synonyms}")
        md.append("")
        abstracts = pubmed.get_abstracts(job.pmids)
        for pmid in job.pmids:
            a = abstracts.get(pmid) or Abstract(pmid, "")
            year = f" ({a.year})" if a.year else ""
            md += [f"### PMID:{pmid}{year}: {' '.join(a.title.split())}", ""]
            answer = answers.get((job.pair_id, pmid))
            if answer is None:
                md += ["- not answered", ""]
                continue
            types = "; ".join(a.publication_types or []) or "none"
            md.append(f"Design: {answer.study_design} (publication types: {types})")
            if not answer.claims:
                md.append("- no claims")
            claims = []
            for claim in answer.claims:
                cid = claim_id(claim)
                check = checks.get((job.pair_id, pmid, cid))
                fixed = repairs.get((job.pair_id, pmid, cid))
                tags = [claim["claim_role"]]
                if claim["hedged"]:
                    tags.append("hedged")
                if claim["negative_finding_type"]:
                    tags.append(claim["negative_finding_type"])
                context = _context_text(claim["context"])
                if context:
                    tags.append(context)
                found = _found_note(check.sentence_valid if check else None)
                md.append(
                    f"- **{claim['polarity']}**: {sentences.claim_sentence(claim, names)} "
                    f'({", ".join(tags)}): "{claim["evidence_sentence"]}"{found}'
                )
                if fixed is not None:
                    text = f'"{fixed.evidence_sentence}"' if fixed.evidence_sentence else "none"
                    md.append(f"  - repair: {text}{_found_note(fixed.sentence_valid)}")
                for entry in claim["also_supports"]:
                    also = statement_of(entry)
                    md.append(f"  - also supports: {sentences.sentence(also, names)}")
                if check is not None and not check.valid:
                    md.append(f"  - INVALID: {'; '.join(check.errors)}")
                claims.append(
                    {
                        "claim_id": cid,
                        "claim": claim,
                        "valid": check.valid if check else None,
                        "errors": check.errors if check else None,
                        "sentence_valid": check.sentence_valid if check else None,
                        "repaired_sentence": fixed.evidence_sentence if fixed else None,
                    }
                )
            md.append("")
            rows.append(
                {
                    "pair_id": job.pair_id,
                    "pmid": pmid,
                    "title": a.title,
                    "abstract": a.abstract,
                    "publication_types": a.publication_types,
                    "study_design": answer.study_design,
                    "claims": claims,
                }
            )
    md_path, jsonl_path = work / SAMPLE_MD, work / SAMPLE_JSONL
    work.mkdir(parents=True, exist_ok=True)
    md_path.write_text("\n".join(md).rstrip() + "\n", encoding="utf-8")
    write_jsonl(jsonl_path, rows)
    return md_path, jsonl_path


# --- The step -------------------------------------------------------------------------------


def label_pairs(
    work: Path,
    cache_dir: Path,
    *,
    model: str = config.BULK_MODEL,
    prompt: Path = config.LABEL_PROMPT,
    client: Any = None,
    workers: int = config.WORKERS,
    limit: int | None = None,
    sample: int | None = None,
    seed: int = 0,
    pairs: Path | None = None,
    abstracts_per_call: int = config.ABSTRACTS_PER_CALL,
    max_output_tokens: int | None = None,
    reasoning_effort: str | None = None,
    dry_run: bool = False,
    repair_quotes: bool = True,
    repair_client: Any = None,
) -> dict[str, Any]:
    """Ask for the claims of the pairs' abstracts that have no answer yet for this model,
    prompt and effort (the prompt version covers the effort), check every claim of these pairs,
    and return a summary. `client` is anything with a client's `call`; by default
    `llm.client_for(model)`, built only if there is something to send, with
    `max_output_tokens` and `reasoning_effort` overriding the model's own settings. Then, unless
    `repair_quotes` is False, the quote repair pass runs on the pairs' claims, with
    `repair_client` or a client for config.REPAIR_MODEL.

    `sample` reads N pairs chosen at random with `seed`, and `pairs` exactly those a pairs file
    names (`read_pairs`; the scope rules still apply, and those it can't label are listed in
    `pairs_not_found`): both write the sample files, and leave `label_run.json` alone.

    Raises FileNotFoundError for a missing input, ValueError for an unreadable rules or pairs
    file, LLMError for missing credentials.
    """
    work, cache_dir, prompt = Path(work), Path(cache_dir), Path(prompt)
    inputs = read_inputs(work)
    wanted = read_pairs(Path(pairs)) if pairs is not None else None
    if wanted is not None:
        selected = {p.pair_id for p in inputs.pairs}
        inputs.pairs = [p for p in inputs.pairs if p.pair_id in set(wanted)]
    pubmed_path = default_cache_path()
    if not pubmed_path.exists():
        raise FileNotFoundError(f"{pubmed_path} is missing: run `kgb-augment pubmed` first")
    info = llm.model(model)
    system, version = label_prompt(prompt, model, reasoning_effort, inputs.rules)
    effort = llm.run_effort(model, reasoning_effort)
    if abstracts_per_call > config.ABSTRACTS_PER_CALL:
        _warn(
            f"at most {config.ABSTRACTS_PER_CALL} abstracts per call; sending "
            f"{config.ABSTRACTS_PER_CALL}, not {abstracts_per_call}"
        )
    abstracts_per_call = min(max(1, abstracts_per_call), config.ABSTRACTS_PER_CALL)

    summary: dict[str, Any] = {
        "model": model,
        "reasoning_effort": effort,
        "prompt_version": version,
        "prompt": str(prompt),
        "dry_run": dry_run,
    }
    labels_path = cache_dir / config.LABEL_CACHE
    with PubMedCache(pubmed_path) as pubmed:
        jobs, skipped = build_jobs(inputs, pubmed)
        if wanted is not None:
            found = {j.pair_id for j in jobs}
            summary["pairs_file"] = str(pairs)
            summary["pairs_not_found"] = {
                "not_selected": [p for p in wanted if p not in selected],
                "skipped": [p for p in wanted if p in selected and p not in found],
            }
        if limit is not None:
            jobs = jobs[: max(0, limit)]
        if sample is not None:
            picked = random.Random(seed).sample(range(len(jobs)), min(max(0, sample), len(jobs)))
            jobs = [jobs[i] for i in sorted(picked)]
        # A dry run reads the label cache but doesn't create it.
        labels = LabelCache(labels_path) if not dry_run or labels_path.exists() else None
        try:
            pair_ids = [j.pair_id for j in jobs]
            answered = labels.answered(version, model, pair_ids) if labels else set()
            plans = [(job, plan_requests(job, answered, abstracts_per_call)) for job in jobs]
            calls = sum(len(r) for _, r in plans)
            summary |= {
                "pairs": len(jobs),
                "pairs_to_call": sum(bool(r) for _, r in plans),
                "skipped": dict(skipped),
                "requests": calls,
                "calls_needed": calls + calls_answered(jobs, answered, abstracts_per_call),
                "abstracts_to_answer": sum(len(q.pmids) for _, r in plans for q in r),
            }
            qualifiers = Qualifiers()
            if dry_run:
                summary |= _dry_run(work, plans, pubmed, qualifiers, system, info, version)
                return summary
            if calls and client is None:
                client = llm.client_for(
                    info, max_output_tokens=max_output_tokens, reasoning_effort=reasoning_effort
                )
            stats = _execute(
                plans,
                client=client,
                system=system,
                labels=labels,
                pubmed=pubmed,
                qualifiers=qualifiers,
                model=model,
                prompt_version=version,
                workers=workers,
            )
            checked = check_pairs(
                jobs, labels, pubmed, qualifiers, prompt_version=version, model=model
            )
            if repair_quotes:
                names = {e.id: e.name for e in inputs.entities.values() if e.name}
                summary["repairs"] = repair.repair_quotes(
                    labels,
                    pubmed,
                    prompt_version=version,
                    model=model,
                    pair_ids=pair_ids,
                    names=names,
                    client=repair_client,
                    workers=workers,
                )
            # A sample or a pairs file tries a prompt or a model out, and a failed run is
            # incomplete: only a successful full (or --limit) run names what ground, review and
            # step 6 use.
            written = (
                sample is None
                and pairs is None
                and not llm.too_many_failed(summary["calls_needed"], stats.tally.failed)
            )
            if written:
                made = summary.get("repairs")
                repairs = None
                if made is not None and made["skipped"] is None:
                    repairs = (made["repairer"], made["repair_prompt_version"])
                _write_run(work, model, version, prompt, effort, repairs)
            summary["label_run_written"] = written
            if sample is not None or pairs is not None:
                md, jsonl = write_sample(
                    work,
                    jobs,
                    labels,
                    pubmed,
                    model=model,
                    prompt_version=version,
                    seed=seed,
                    source=pairs,
                )
                summary["sample_files"] = [str(md), str(jsonl)]
            usage = labels.usage("label", model, version)
        finally:
            if labels is not None:
                labels.close()
    tally = stats.tally
    summary |= {
        "calls": tally.calls,
        "calls_ok": tally.calls - tally.failed,
        "calls_failed": tally.failed,
        "fallback_served": tally.fallback_served,
        "pairs_called": len(tally.pairs),
        "abstracts_answered": stats.answered,
        "claims_extracted": stats.claims,
        "problems": dict(stats.problems),
        "checked": {
            "abstracts": checked.abstracts,
            "no_claim": checked.no_claim,
            "claims": checked.claims,
            "valid": checked.valid,
            "invalid": checked.invalid,
            "sentences_not_found": checked.sentences_not_found,
            "negatives_without_type": checked.negatives_without_type,
            "errors": dict(checked.errors.most_common()),
        },
        "input_tokens": tally.input_tokens,
        "cache_read_tokens": tally.cache_read_tokens,
        "cache_write_tokens": tally.cache_write_tokens,
        "output_tokens": tally.output_tokens,
        "cost": tally.cost,
        "usage": usage,
    }
    return summary


def _dry_run(
    work: Path,
    plans: list[tuple[PairJob, list[Request]]],
    pubmed: PubMedCache,
    qualifiers: Qualifiers,
    system: str,
    info: config.Model,
    version: str,
) -> dict[str, Any]:
    """Build every request without sending any: write the first to REQUEST_EXAMPLE and
    estimate tokens (characters / 4) and cost. The system prompt and the reply schema come
    first in every request, so after the first call with a schema they are billed at the
    cache-read price once they are long enough for the service's prompt caching. Each call is
    priced on its own, since a model's longer-prompt price depends on the request's prompt."""
    tokens = cached = calls = 0
    cost = 0.0
    example = None
    seen: set[str] = set()
    for call in _calls(plans, pubmed, qualifiers):
        schema_text = call.schema.text() if call.schema else ""
        prefix = len(system) + len(schema_text)
        prefix_tokens = prefix // 4
        call_tokens = (prefix + len(call.user)) // 4
        hit = schema_text in seen and prefix_tokens >= CACHED_PREFIX_TOKENS
        read = prefix_tokens if hit else 0
        seen.add(schema_text)
        cost += llm.cost_of(info, call_tokens - read, read, 0, ESTIMATED_OUTPUT_TOKENS)
        tokens, cached, calls = tokens + call_tokens, cached + read, calls + 1
        if example is None:
            example = work / REQUEST_EXAMPLE
            work.mkdir(parents=True, exist_ok=True)
            example.write_text(
                f"model: {info.id}\nprompt version: {version}\n\n"
                f"=== instructions (system) ===\n\n{system.rstrip()}\n\n"
                f"=== input (user) ===\n\n{call.user}\n",
                encoding="utf-8",
            )
    return {
        "calls": 0,
        "estimated_input_tokens": tokens,
        "estimated_cached_tokens": cached,
        "estimated_output_tokens": calls * ESTIMATED_OUTPUT_TOKENS,
        "estimated_cost": cost,
        "request_example": str(example) if example else None,
    }


def _write_run(
    work: Path,
    model: str,
    version: str,
    prompt: Path,
    effort: str,
    repairs: tuple[str, str] | None,
) -> None:
    """Name this run in `label_run.json`, with its effort and the (repairer, repair prompt
    version) of its repair pass, if one ran. Its review, and the earlier repairs when no pass
    ran, are kept only if they were of this run. The UBERON release is kept: no run changes
    it."""
    run = records.LabelRun(
        model=model, prompt_version=version, prompt=str(prompt), reasoning_effort=effort
    )
    old = records.read_label_run(work)
    if old is not None:
        run.uberon_version = old.uberon_version
    if old is not None and (old.model, old.prompt_version) == (model, version):
        run.reviewer, run.review_prompt_version = old.reviewer, old.review_prompt_version
        run.review_reasoning_effort = old.review_reasoning_effort
        run.repairer, run.repair_prompt_version = old.repairer, old.repair_prompt_version
    if repairs is not None:
        run.repairer, run.repair_prompt_version = repairs
    records.write_label_run(work, run)


def _money(value: float | None) -> str:
    return "unknown" if value is None else f"${value:.4f}"


def print_summary(s: dict[str, Any]) -> None:
    print(
        f"label: model {s['model']} (effort {s['reasoning_effort']}), prompt "
        f"{s['prompt_version']} ({s['prompt']})"
    )
    skipped = ", ".join(f"{n} {what}" for what, n in s["skipped"].items())
    print(
        f"label: {s['pairs']} pairs, {s['pairs_to_call']} with abstracts to read"
        + (f"; skipped: {skipped}" if skipped else "")
    )
    if s["dry_run"]:
        print(
            f"label: dry run: {s['requests']} calls for {s['abstracts_to_answer']} abstracts, "
            f"~{s['estimated_input_tokens']:,} input tokens (~{s['estimated_cached_tokens']:,} "
            f"of them cached); est. cost {_money(s['estimated_cost'])} with "
            f"~{ESTIMATED_OUTPUT_TOKENS} output tokens a call"
        )
        if s["request_example"]:
            print(f"label: first request in {s['request_example']}")
        return
    n = s["fallback_served"]
    served = f"; {n} answered by a fallback model" if n else ""
    print(f"label: calls: {s['calls']} ({s['calls_ok']} ok, {s['calls_failed']} failed){served}")
    print(
        f"label: answered {s['abstracts_answered']} abstracts with {s['claims_extracted']} claims"
    )
    c = s["checked"]
    print(
        f"label: these pairs' answers: {c['abstracts']} abstracts ({c['no_claim']} with no "
        f"claim), {c['claims']} claims: {c['valid']} valid, {c['invalid']} invalid; "
        f"{c['sentences_not_found']} valid claims with a sentence not found; "
        f"{c['negatives_without_type']} negative claims without a finding type"
    )
    if c["errors"]:
        top = list(c["errors"].items())[:5]
        print("label: most common check errors: " + "; ".join(f"{e} ({n}x)" for e, n in top))
    if s["problems"]:
        print("label: " + ", ".join(f"{n} {what}" for what, n in s["problems"].items()))
    per_pair = (
        s["cost"] / s["pairs_called"] if s["cost"] is not None and s["pairs_called"] else None
    )
    print(
        f"label: tokens: {s['input_tokens']:,} in ({s['cache_read_tokens']:,} read from the "
        f"cache, {s['cache_write_tokens']:,} written to it), "
        f"{s['output_tokens']:,} out; cost {_money(s['cost'])} ({_money(per_pair)} per pair)"
    )
    u = s["usage"]
    total_per_pair = u["cost"] / u["pairs"] if u["cost"] is not None and u["pairs"] else None
    print(
        f"label: all label calls of this model and prompt: {u['calls']} calls "
        f"({u['errors']} failed) over {u['pairs']} pairs, cost {_money(u['cost'])} "
        f"({_money(total_per_pair)} per pair)"
    )
    if "repairs" in s:
        repair.print_summary(s["repairs"])
    for path in s.get("sample_files", []):
        print(f"label: wrote {path}")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model", default=config.BULK_MODEL, help="bulk model (default: %(default)s)"
    )
    parser.add_argument(
        "--prompt",
        type=Path,
        default=config.LABEL_PROMPT,
        help="system prompt file; its content, with the files it includes, sets the prompt "
        "version (default: %(default)s)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=config.WORKERS,
        help="concurrent calls (default: %(default)s)",
    )
    parser.add_argument("--limit", type=int, help="read only the first N pairs' abstracts")
    chosen = parser.add_mutually_exclusive_group()
    chosen.add_argument(
        "--sample",
        type=int,
        help=f"read N pairs chosen at random and write {SAMPLE_MD} and {SAMPLE_JSONL}",
    )
    chosen.add_argument(
        "--pairs",
        type=Path,
        metavar="FILE",
        help="read exactly the pairs a JSONL file names, one pair_id a line, as `evaluate "
        f"--draw` writes them, if select kept them, and write {SAMPLE_MD} and {SAMPLE_JSONL}; "
        f"{records.LABEL_RUN} is left alone",
    )
    parser.add_argument("--seed", type=int, default=0, help="seed for --sample (default: 0)")
    parser.add_argument(
        "--abstracts-per-call",
        type=int,
        default=config.ABSTRACTS_PER_CALL,
        help="most abstracts sent in one call, at most %(default)s (default: %(default)s)",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=None,
        help="output token cap per call, reasoning included (default: the model's in "
        "config.MODELS)",
    )
    parser.add_argument(
        "--reasoning-effort",
        default=None,
        help="reasoning effort for reasoning models, or 'none' to omit (default: the model's in "
        "config.MODELS); it is part of the prompt version, so another effort reads afresh",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=f"send nothing: write the first request to {REQUEST_EXAMPLE}, estimate the cost",
    )
    parser.add_argument(
        "--no-repair",
        action="store_true",
        help="skip the quote repair pass, which asks the review model for the evidence "
        "sentences that weren't found",
    )


def run(args: argparse.Namespace) -> int:
    try:
        summary = label_pairs(
            args.work,
            args.cache_dir,
            model=args.model,
            prompt=args.prompt,
            workers=args.workers,
            limit=args.limit,
            sample=args.sample,
            seed=args.seed,
            pairs=args.pairs,
            abstracts_per_call=args.abstracts_per_call,
            max_output_tokens=args.max_output_tokens,
            reasoning_effort=args.reasoning_effort,
            dry_run=args.dry_run,
            repair_quotes=not args.no_repair,
        )
    except (FileNotFoundError, ValueError, llm.LLMError) as e:
        print(f"label: error: {e}", file=sys.stderr)
        return 1
    print_summary(summary)
    calls, failed = summary["calls"], summary.get("calls_failed", 0)
    needed = summary.get("calls_needed", calls)
    if llm.too_many_failed(needed, failed):
        print(
            f"label: error: {failed} of the {needed} calls these pairs need failed (more than "
            f"{llm.MAX_FAILED_SHARE:.0%}; those answered before count too); "
            f"{records.LABEL_RUN} not updated; rerun to retry, answers already cached are kept",
            file=sys.stderr,
        )
        return 1
    if failed:
        _warn(f"{failed} of {calls} calls failed; rerun to retry; answers already cached are kept")
    left = summary.get("problems", {}).get("abstracts left out of the reply")
    if left:
        _warn(f"{left} abstracts were left out of the replies; rerun to ask for them again")
    missing = summary.get("pairs_not_found") or {}
    if missing.get("not_selected"):
        _warn(
            f"{len(missing['not_selected'])} pairs in {summary['pairs_file']} weren't kept by "
            f"select, so they aren't labeled: {', '.join(missing['not_selected'][:5])}"
        )
    if missing.get("skipped"):
        _warn(
            f"{len(missing['skipped'])} pairs in {summary['pairs_file']} have nothing to read "
            f"(see skipped): {', '.join(missing['skipped'][:5])}"
        )
    return 0
