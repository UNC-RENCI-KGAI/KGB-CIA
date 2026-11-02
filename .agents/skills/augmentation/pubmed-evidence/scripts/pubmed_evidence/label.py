"""Step 5: an LLM labels every (statement, abstract) of a pair and proposes qualifiers.

One call per pair rather than per edge: abstracts are most of the tokens, so each is sent once
for all of the pair's statements (in chunks of `--abstracts-per-call` when a pair has more).
The model only labels. Code checks each quote against its abstract, validates each proposed
qualifier set with Biolink, records `no_evidence` for every combination the model left out
(it lists only the others, to save output tokens), and stores it all in the label cache,
keyed by (statement, PMID, prompt version, model). Step 6 reads only that cache.

A rerun sends only the (statement, PMID) combinations with no label yet, and a new prompt or
model starts fresh: the prompt version hashes the prompt file together with FORMAT_VERSION,
the version of the code that writes the user message, so a change to either counts.

The calls run in a thread pool; everything else, including every cache write and every
Biolink lookup, stays on the main thread, since neither SQLite connections nor the toolkit are
shared safely between threads.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pubmed_evidence import biolink, config, llm, records
from pubmed_evidence.biolink import QualifierSpec
from pubmed_evidence.cache import (
    LABELS,
    Abstract,
    CallRow,
    LabelCache,
    LabelRow,
    ProposalRow,
    PubMedCache,
    canonical,
)
from pubmed_evidence.graphio import pair_nodes, read_jsonl, write_jsonl
from pubmed_evidence.quotes import quote_found
from pubmed_evidence.records import Entity, Pair, PubmedRecord, SentenceRecord, Statement

# Bump when the user message changes shape: it is part of the prompt version.
FORMAT_VERSION = "label-message-3"

DECISIVE = ("confirmed", "contradicted")
CLAIMS = ("causal", "associative")
MAX_OUTPUT_TOKENS = 16000
REASONING_EFFORT = "low"
# A run with more failed calls than this fails; below it, it warns.
MAX_FAILED_SHARE = 0.05
MAX_SYNONYMS_SHOWN = 12
# The dry run's cost estimate assumes this many output tokens per call (DESIGN.md: ~1k per pair).
ESTIMATED_OUTPUT_TOKENS = 1000
# Azure OpenAI caches a prompt prefix of at least this many tokens.
CACHED_PREFIX_TOKENS = 1024

SAMPLE_MD = "label_sample.md"
SAMPLE_JSONL = "label_sample.jsonl"
REQUEST_EXAMPLE = "label_request_example.txt"

# The step that writes each input file, for the error when one is missing.
_INPUTS = {
    records.PAIRS: "select",
    records.STATEMENTS: "select",
    records.ENTITIES: "select",
    records.SENTENCES: "sentences",
    records.PUBMED: "pubmed",
}


def _warn(message: str) -> None:
    print(f"label: warning: {message}", file=sys.stderr)


def too_many_failed(calls: int, failed: int) -> bool:
    """Whether a run failed: more than MAX_FAILED_SHARE of its calls failed."""
    return bool(calls) and failed / calls > MAX_FAILED_SHARE


def label_prompt(prompt: Path = config.LABEL_PROMPT) -> tuple[str, str]:
    """(system prompt, prompt version) of a label prompt file, the version covering
    FORMAT_VERSION too. Anything that needs the version of a label run can call this."""
    return llm.load_prompt(prompt, extra=FORMAT_VERSION)


# --- Inputs ---------------------------------------------------------------------------------


@dataclass
class Inputs:
    pairs: list[Pair]
    statements: dict[str, Statement]
    entities: dict[str, Entity]
    sentences: dict[str, str]
    pubmed: dict[str, PubmedRecord]


def read_inputs(work: Path) -> Inputs:
    """The work files of steps 2-4. Raises FileNotFoundError naming the step to run first."""
    for name, step in _INPUTS.items():
        if not (work / name).exists():
            raise FileNotFoundError(f"{work / name} is missing: run `kgb-augment {step}` first")
    return Inputs(
        pairs=list(records.read_records(work / records.PAIRS, Pair)),
        statements={
            s.statement_id: s for s in records.read_records(work / records.STATEMENTS, Statement)
        },
        entities={e.id: e for e in records.read_records(work / records.ENTITIES, Entity)},
        sentences={
            s.statement_id: s.sentence
            for s in records.read_records(work / records.SENTENCES, SentenceRecord)
        },
        pubmed={p.pair_id: p for p in records.read_records(work / records.PUBMED, PubmedRecord)},
    )


def _pmid(value: Any) -> str | None:
    """The digits of a PMID however it is written ('12345', 12345, 'PMID:12345', 'PMID 12345');
    None if there are none."""
    if value is None or isinstance(value, bool):
        return None
    m = re.search(r"\d+", str(value))
    return m.group(0) if m else None


def read_gold(path: Path) -> list[tuple[str, str]]:
    """The (statement_id, pmid) items of a gold file, in file order, without repeats."""
    items: dict[tuple[str, str], None] = {}
    for n, d in enumerate(read_jsonl(Path(path)), 1):
        sid, pmid = d.get("statement_id"), _pmid(d.get("pmid"))
        if not isinstance(sid, str) or not sid or pmid is None:
            raise ValueError(f"{path}, line {n}: a gold item needs a statement_id and a pmid")
        items[(sid, pmid)] = None
    return list(items)


# --- Jobs and requests ----------------------------------------------------------------------


@dataclass
class PairJob:
    """What a pair's calls need, except the abstract texts, which are loaded only when its
    calls are made so that a large run doesn't hold every abstract in memory."""

    pair_id: str
    entities: list[Entity]
    synonyms: dict[str, list[str]]
    mesh: dict[str, list[str]]
    statements: list[Statement]
    sentences: dict[str, str]
    pmids: list[str] = field(default_factory=list)
    wanted: set[tuple[str, str]] | None = None  # only these (statement, PMID), for --gold

    def combos(self) -> set[tuple[str, str]]:
        every = {(s.statement_id, p) for s in self.statements for p in self.pmids}
        return every if self.wanted is None else every & self.wanted

    def categories(self, node: str) -> list[str]:
        for e in self.entities:
            if e.id == node:
                return list(e.category)
        return []


@dataclass
class Request:
    """One call: some of a pair's statements (S1, S2, ... in this order) against some of its
    abstracts, and the (statement_id, pmid) combinations it labels."""

    pair_id: str
    statements: list[Statement]
    pmids: list[str]
    combos: set[tuple[str, str]]


def _job(
    pair_id: str,
    statement_ids: Iterable[str],
    pmids: Iterable[str],
    inputs: Inputs,
    pubmed: PubMedCache,
    skipped: Counter,
    wanted: set[tuple[str, str]] | None = None,
) -> PairJob | None:
    statements = []
    for sid in dict.fromkeys(statement_ids):
        statement = inputs.statements.get(sid)
        if statement is None:
            skipped["statements missing from statements.jsonl"] += 1
        elif sid not in inputs.sentences:
            skipped["statements without a sentence"] += 1
        else:
            statements.append(statement)
    if not statements:
        skipped["pairs without statements to label"] += 1
        return None
    pmids = list(dict.fromkeys(pmids))
    texts = pubmed.get_abstracts(pmids)
    missing = sum(p not in texts for p in pmids)
    if missing:
        skipped["abstracts missing from the PubMed cache"] += missing
    pmids = [p for p in pmids if p in texts and texts[p].has_text]
    if not pmids:
        skipped["pairs without abstracts"] += 1
        return None
    record = inputs.pubmed.get(pair_id)
    entities = [inputs.entities.get(n) or Entity(id=n) for n in pair_nodes(pair_id)]
    synonyms = {
        e.id: (record.synonyms.get(e.id) if record and record.synonyms.get(e.id) else e.synonyms)
        for e in entities
    }
    mesh = {e.id: list(record.mesh.get(e.id, [])) if record else [] for e in entities}
    return PairJob(
        pair_id,
        entities,
        synonyms,
        mesh,
        statements,
        {s.statement_id: inputs.sentences[s.statement_id] for s in statements},
        pmids,
        wanted,
    )


def build_jobs(
    inputs: Inputs, pubmed: PubMedCache, gold: list[tuple[str, str]] | None = None
) -> tuple[list[PairJob], Counter]:
    """A job per pair with something to label, in `pairs.jsonl` order, and the counts of
    what was skipped. With `gold`, only the gold items' statements and PMIDs."""
    skipped: Counter = Counter()
    jobs: list[PairJob] = []
    if gold is None:
        for pair in inputs.pairs:
            record = inputs.pubmed.get(pair.pair_id)
            if record is None:
                skipped["pairs without a PubMed record"] += 1
                continue
            pmids = [p.pmid for p in record.pmids]
            job = _job(pair.pair_id, pair.statement_ids, pmids, inputs, pubmed, skipped)
            if job is not None:
                jobs.append(job)
        return jobs, skipped
    by_pair: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for sid, pmid in gold:
        statement = inputs.statements.get(sid)
        if statement is None:
            skipped["gold items with an unknown statement"] += 1
            continue
        by_pair[statement.pair_id].append((sid, pmid))
    order = {p.pair_id: i for i, p in enumerate(inputs.pairs)}
    for pair_id in sorted(by_pair, key=lambda p: (order.get(p, len(order)), p)):
        items = by_pair[pair_id]
        job = _job(
            pair_id,
            [sid for sid, _ in items],
            [pmid for _, pmid in items],
            inputs,
            pubmed,
            skipped,
            wanted=set(items),
        )
        if job is not None:
            jobs.append(job)
    return jobs, skipped


def plan_requests(
    job: PairJob, labeled: set[tuple[str, str]], per_call: int = config.ABSTRACTS_PER_CALL
) -> list[Request]:
    """The calls a pair still needs: the abstracts missing a label for some statement, at most
    `per_call` to a call, each with the statements missing a label for one of them."""
    needed = job.combos() - labeled
    pmids = [p for p in job.pmids if any((s.statement_id, p) in needed for s in job.statements)]
    out = []
    for i in range(0, len(pmids), max(1, per_call)):
        chunk = pmids[i : i + max(1, per_call)]
        statements = [
            s for s in job.statements if any((s.statement_id, p) in needed for p in chunk)
        ]
        combos = {(s.statement_id, p) for s in statements for p in chunk} & needed
        out.append(Request(job.pair_id, statements, chunk, combos))
    return out


# --- Qualifiers -----------------------------------------------------------------------------


def normalize_qualifiers(qualifiers: Any) -> dict[str, Any] | None:
    """Slot names without 'biolink:', values stripped, `qualified_predicate` as a 'biolink:'
    CURIE, empty values dropped, sorted. None if it isn't a dict."""
    if not isinstance(qualifiers, dict):
        return None
    out: dict[str, Any] = {}
    for slot, value in qualifiers.items():
        slot = str(slot).strip().removeprefix("biolink:")
        if isinstance(value, str):
            value = value.strip()
            if slot == "qualified_predicate" and value:
                value = "biolink:" + value.removeprefix("biolink:").strip().replace(" ", "_")
        if slot and value not in (None, "", [], {}):
            out[slot] = value
    return dict(sorted(out.items()))


# Subject and object qualifiers (aspect, direction, form or variant, part, derivative) describe
# a gene, gene product or chemical. Biolink's mixins also allow them on diseases and phenotypes
# ("decreased expression of asthma"), so they are offered only for these.
QUALIFIABLE = ("biolink:GeneOrGeneProduct", "biolink:ChemicalEntity")
DISEASE_OR_PHENOTYPE = "biolink:DiseaseOrPhenotypicFeature"


# Forms that say only "variants of the gene": a gene-to-condition edge already means that, so
# "genetic variant form of GENE causes DISEASE" restates "GENE causes DISEASE". The model
# proposes it for nearly every "mutations in GENE cause DISEASE" abstract; a specific form
# (loss_of_function_variant_form, gain_of_function_variant_form, ...) does add something.
GENERIC_FORMS = frozenset({"genetic_variant_form", "mutant_form", "polymorphic_form"})
GENERIC_FORM_ERROR = "a generic variant form restates a gene-to-condition edge"


def _restates_variants(proposed: dict[str, Any], subject: list[str], object: list[str]) -> bool:
    form = proposed.get("subject_form_or_variant_qualifier")
    return (
        form in GENERIC_FORMS
        and not any(k.startswith(("subject_", "object_")) for k in proposed if "form" not in k)
        and biolink.is_a(subject, "biolink:GeneOrGeneProduct")
        and biolink.is_a(object, "biolink:DiseaseOrPhenotypicFeature")
    )


class Qualifiers:
    """The qualifiers offered for each statement and the check of each proposal, memoized:
    many statements share an association, and bmt lookups are slow.

    Offered: the enum-valued slots the edge's associations allow, minus those that don't fit
    its entities. Open-ranged ones (an anatomical, species or disease context) take a CURIE,
    which the model would have to make up and which Biolink can check only for shape. A
    `subject_*` or `object_*` slot needs a gene, gene product or chemical on that side; a
    chemical and a disease or phenotype (treats, contraindicated_in, ...) get nothing; and
    `qualified_predicate` comes only with a subject or object slot, as Biolink reads it. A
    proposal using any slot not offered is invalid.
    """

    OPEN_RANGE = "open-range qualifier not proposed by the LLM"

    def __init__(self) -> None:
        self._allowed: dict[tuple, dict[str, QualifierSpec]] = {}
        self._offered: dict[tuple, dict[str, QualifierSpec]] = {}
        self._errors: dict[tuple[str, str], list[str]] = {}

    @staticmethod
    def _edge(job: PairJob, s: Statement) -> tuple[list[str], str, list[str]]:
        return job.categories(s.subject), s.predicate, job.categories(s.object)

    def _all(self, job: PairJob, s: Statement) -> dict[str, QualifierSpec]:
        subject, predicate, object = self._edge(job, s)
        key = (tuple(subject), predicate, tuple(object))
        if key not in self._allowed:
            allowed = biolink.allowed_qualifiers(subject, predicate, object)
            self._allowed[key] = dict(sorted(allowed.items()))
        return self._allowed[key]

    def offered(self, job: PairJob, s: Statement) -> dict[str, QualifierSpec]:
        subject, predicate, object = self._edge(job, s)
        key = (tuple(subject), predicate, tuple(object))
        if key not in self._offered:
            self._offered[key] = self._fitting(self._all(job, s), subject, object)
        return self._offered[key]

    @staticmethod
    def _fitting(
        allowed: dict[str, QualifierSpec], subject: list[str], object: list[str]
    ) -> dict[str, QualifierSpec]:
        if biolink.is_a(subject, "biolink:ChemicalEntity") and biolink.is_a(
            object, DISEASE_OR_PHENOTYPE
        ):
            return {}
        fits = {
            "subject": biolink.is_a(subject, QUALIFIABLE),
            "object": biolink.is_a(object, QUALIFIABLE),
        }
        out = {
            slot: spec
            for slot, spec in allowed.items()
            if spec.values and fits.get(slot.split("_", 1)[0], True)
        }
        if not _side_qualified(out):
            out.pop("qualified_predicate", None)
        return out

    def errors(self, job: PairJob, s: Statement, proposed: dict[str, Any]) -> list[str]:
        """What is wrong with the statement's qualifiers once `proposed` is merged onto them
        exactly as step 6 merges them (`{**statement.qualifiers, **proposed}`), in words, plus
        any proposed slot or value that wasn't offered; [] if it is valid."""
        key = (s.statement_id, canonical(proposed))
        if key not in self._errors:
            allowed, offered = self._all(job, s), self.offered(job, s)
            errors = []
            for slot, value in proposed.items():
                spec = offered.get(slot)
                if spec is None:
                    if slot in allowed and not allowed[slot].values:
                        errors.append(f"{slot}: {self.OPEN_RANGE}")
                    else:
                        errors.append(
                            f"{slot} is not among the qualifiers offered for the statement"
                        )
                elif not isinstance(value, str) or value not in spec.values:
                    errors.append(f"{value!r} is not an allowed value of {slot}")
            subject, predicate, object = self._edge(job, s)
            merged = {**s.qualifiers, **proposed}
            errors += biolink.validate_qualifiers(merged, subject, predicate, object)
            if _restates_variants(proposed, subject, object):
                errors.append(GENERIC_FORM_ERROR)
            self._errors[key] = list(dict.fromkeys(errors))
        return self._errors[key]


# --- The user message -----------------------------------------------------------------------


def _qualifier_text(qualifiers: dict[str, Any]) -> str:
    return "; ".join(f"{k}: {v}" for k, v in sorted(qualifiers.items())) or "none"


# Value lists up to this long are written next to their slot; longer ones once, by name.
INLINE_VALUES = 3


def _qualifier_lines(groups: dict[tuple, tuple[str, dict[str, QualifierSpec], list[str]]]):
    """The allowed qualifier sets (statements sharing a set share its id) and, once each, the
    long value lists they refer to by range name: the aspect and mechanism enums alone run to
    well over a hundred values, and several slots and sets share them."""
    named: dict[tuple[str, ...], str] = {}

    def values_of(slot: str, spec: QualifierSpec) -> str:
        values = tuple(spec.values or ())
        if len(values) <= INLINE_VALUES:
            return ", ".join(values)
        if values not in named:
            base = spec.range or f"{slot} values"
            name, n = base, 2
            while name in named.values():
                name, n = f"{base} ({n})", n + 1
            named[values] = name
        return f"one of {named[values]}"

    lines = ["# Allowed qualifiers", ""]
    for qid, specs, sids in groups.values():
        members = sids[0] if len(sids) == 1 else ", ".join(sids[:-1]) + f" and {sids[-1]}"
        lines.append(f"{qid}, for {members}:")
        lines += [f"- {slot}: {values_of(slot, spec)}" for slot, spec in specs.items()]
        lines.append("")
    if named:
        lines += ["# Qualifier values", ""]
        lines += [f"- {name}: {', '.join(values)}" for values, name in named.items()]
        lines.append("")
    return lines


def build_message(
    job: PairJob,
    request: Request,
    abstracts: dict[str, Abstract],
    offered: dict[str, dict[str, QualifierSpec]],
) -> str:
    """The user message of one call: the entities, the statements S1..Sn with their allowed
    qualifier sets, then the abstracts. Its layout is FORMAT_VERSION."""
    names = {e.id: e.name or e.id for e in job.entities}
    lines = ["# Entities", ""]
    for e in job.entities:
        name = names[e.id]
        lines.append(f"- {name} ({e.id})")
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
    lines += ["", "# Statements", ""]
    groups: dict[tuple, tuple[str, dict[str, QualifierSpec], list[str]]] = {}
    for i, s in enumerate(request.statements, 1):
        specs = offered.get(s.statement_id) or {}
        group = "none"
        if specs:
            key = tuple((slot, spec.values) for slot, spec in specs.items())
            if key not in groups:
                groups[key] = (f"Q{len(groups) + 1}", specs, [])
            group = groups[key][0]
            groups[key][2].append(f"S{i}")
        subject = f"{names.get(s.subject, s.subject)} ({s.subject})"
        object = f"{names.get(s.object, s.object)} ({s.object})"
        lines += [
            f"S{i}: {job.sentences[s.statement_id]}",
            f"  subject: {subject}",
            f"  predicate: {s.predicate}",
            f"  object: {object}",
            f"  qualifiers: {_qualifier_text(normalize_qualifiers(s.qualifiers) or {})}",
            f"  allowed qualifiers: {group}",
            "",
        ]
    if groups:
        lines += _qualifier_lines(groups)
    lines += ["# Abstracts", ""]
    for pmid in request.pmids:
        a = abstracts[pmid]
        year = f" ({a.year})" if a.year else ""
        lines += [
            f"## PMID:{pmid}{year}",
            f"Title: {' '.join(a.title.split())}",
            f"Abstract: {a.abstract.strip()}",
            "",
        ]
    n = len(request.statements)
    ids = "S1" if n == 1 else f"S1 to S{n}"
    lines.append(
        f"Label every statement ({ids}) against every one of the {len(request.pmids)} "
        "abstracts above, propose qualifiers where they apply, and reply with the JSON object "
        "only."
    )
    return "\n".join(lines)


# --- Reading the reply ----------------------------------------------------------------------


@dataclass
class Parsed:
    labels: list[LabelRow]
    proposals: list[ProposalRow]
    problems: Counter


def _local_id(value: Any) -> str | None:
    """'S1', 's1', 1, 'S 1', 'S1.' -> 'S1'; None if it isn't a statement id."""
    if value is None or isinstance(value, bool):
        return None
    m = re.fullmatch(r"\s*S?\s*(\d+)\s*[.:]?\s*", str(value), re.I)
    return f"S{int(m.group(1))}" if m else None


def _label_value(value: Any) -> str | None:
    """A label value whatever its case, spacing or surrounding punctuation ('Confirmed.',
    'no evidence', 'no-evidence'); None if it isn't one of the four."""
    if not isinstance(value, str):
        return None
    value = re.sub(r"^[^a-z]+|[^a-z]+$", "", value.lower())
    value = re.sub(r"[\s_-]+", "_", value)
    return value if value in LABELS else None


def _claim(value: Any) -> str | None:
    value = value.strip().lower() if isinstance(value, str) else None
    return value if value in CLAIMS else None


def _quote(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


CAUSES = "biolink:causes"


def _directional(qualifiers: dict[str, Any]) -> bool:
    return any(k.endswith(("_direction_qualifier", "_aspect_qualifier")) for k in qualifiers)


def with_claim(
    proposed: dict[str, Any], own: dict[str, Any], claim: str | None
) -> tuple[dict[str, Any], list[str]]:
    """Make a proposal's qualifiers agree with its claim, since step 6 reads only the
    qualifiers: a causal direction or aspect gets `qualified_predicate: biolink:causes` unless
    the statement or the proposal already has a qualified predicate. An associative claim is
    kept without one, but one that would end up with `biolink:causes` (proposed, or the
    statement's own) is an error: a correlation can't support a causal edge."""
    errors = []
    merged = {**own, **proposed}
    if claim == "causal" and _directional(proposed) and "qualified_predicate" not in merged:
        proposed = dict(sorted({**proposed, "qualified_predicate": CAUSES}.items()))
    elif claim == "associative" and merged.get("qualified_predicate") == CAUSES:
        errors.append(f"an associative claim can't support qualified_predicate {CAUSES}")
    return proposed, errors


def _rank(row: LabelRow) -> int:
    if row.label in DECISIVE:
        return 3 if row.quote_valid else 1
    return 2 if row.label == "ambiguous" else 0


def _keep(a: LabelRow, b: LabelRow) -> LabelRow:
    """One label from two the model gave the same (statement, PMID): a decisive label with
    its quote found first. Confirmed and contradicted, both quoted, is mixed: ambiguous."""
    if _rank(a) == _rank(b) == 3 and a.label != b.label:
        return LabelRow(a.statement_id, a.pmid, a.prompt_version, a.model, "ambiguous")
    return a if _rank(a) >= _rank(b) else b


def _side_qualified(qualifiers: dict[str, Any]) -> bool:
    return any(k.startswith(("subject_", "object_")) for k in qualifiers)


def parse_reply(
    reply: Any,
    request: Request,
    abstracts: dict[str, Abstract],
    *,
    prompt_version: str,
    model: str,
    check: Callable[[Statement, dict[str, Any]], list[str]],
) -> Parsed:
    """Labels and proposals for the request's combinations from the model's reply.

    Ids are read leniently ('S1', '1', 'PMID 123'). A reply with anything that still can't be
    read (no `labels` list, an unknown statement id or PMID, an invalid label, a malformed
    proposal) raises ValueError: the call then counts as failed, nothing of it is cached, and
    the next run asks again, rather than caching every combination as `no_evidence` and losing
    the findings for good. Labels and proposals for combinations the request didn't ask for are
    ignored and counted. Decisive labels get `quote_valid` from the abstract; a bad quote keeps
    the label (step 6 ignores it). Every combination a readable reply leaves out is
    `no_evidence`. Each proposal becomes a row per evidence PMID, with `check`'s verdict.
    """
    if not isinstance(reply, dict) or not isinstance(reply.get("labels"), list):
        raise ValueError("the reply has no 'labels' list")
    local = {f"S{i}": s for i, s in enumerate(request.statements, 1)}
    pmids = set(request.pmids)
    problems: Counter = Counter()
    unreadable: Counter = Counter()

    def statement_of(item: dict) -> Statement | None:
        statement = local.get(_local_id(item.get("statement")) or "")
        if statement is None:
            unreadable[f"unknown statement id {item.get('statement')!r}"] += 1
        return statement

    def pmid_of(statement: Statement, value: Any) -> str | None:
        pmid = _pmid(value)
        if pmid not in pmids:
            unreadable[f"unknown PMID {value!r}"] += 1
            return None
        if (statement.statement_id, pmid) not in request.combos:
            problems["combination not asked for"] += 1
            return None
        return pmid

    labels: dict[tuple[str, str], LabelRow] = {}
    for item in reply["labels"]:
        if not isinstance(item, dict):
            unreadable["label that isn't an object"] += 1
            continue
        label = _label_value(item.get("label"))
        if label is None:
            unreadable[f"invalid label {item.get('label')!r}"] += 1
            continue
        statement = statement_of(item)
        pmid = pmid_of(statement, item.get("pmid")) if statement else None
        if statement is None or pmid is None:
            continue
        quote = _quote(item.get("quote"))
        a = abstracts[pmid]
        valid = None
        if label in DECISIVE or (label == "ambiguous" and quote):
            valid = quote_found(quote, a.title, a.abstract)
        claim = _claim(item.get("claim")) if label in DECISIVE else None
        row = LabelRow(
            statement.statement_id, pmid, prompt_version, model, label, quote, valid, claim
        )
        key = (statement.statement_id, pmid)
        if key in labels:
            problems["duplicate label"] += 1
            row = _keep(labels[key], row)
        labels[key] = row

    raw = reply.get("proposals")
    if raw is not None and not isinstance(raw, list):
        unreadable["'proposals' that isn't a list"] += 1
    proposals: dict[tuple[str, str, str], ProposalRow] = {}
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            unreadable["proposal that isn't an object"] += 1
            continue
        statement = statement_of(item)
        if statement is None:
            continue
        proposed = normalize_qualifiers(item.get("qualifiers"))
        if proposed is None:
            unreadable["proposal without a qualifiers object"] += 1
            continue
        evidence = item.get("evidence")
        if not isinstance(evidence, list):
            evidence = [item] if item.get("pmid") is not None else []
        if not evidence or not all(isinstance(ev, dict) for ev in evidence):
            unreadable["proposal without evidence objects"] += 1
            continue
        own = normalize_qualifiers(statement.qualifiers) or {}
        # Store only what the proposal adds or changes: step 6 and the review merge it onto
        # the statement's own qualifiers.
        proposed = {k: v for k, v in proposed.items() if own.get(k) != v}
        # Biolink reads qualified_predicate only with a subject or object qualifier; without
        # one (a mechanism alone) it would make the set invalid, so keep the rest without it.
        if "qualified_predicate" in proposed and not _side_qualified({**own, **proposed}):
            problems["qualified_predicate dropped: no subject or object qualifier"] += 1
            proposed = {k: v for k, v in proposed.items() if k != "qualified_predicate"}
        if not proposed:
            problems["proposal adding nothing"] += 1
            continue
        claim = _claim(item.get("claim"))
        proposed, claim_errors = with_claim(proposed, own, claim)
        errors = [*check(statement, proposed), *claim_errors]
        for ev in evidence:
            pmid = pmid_of(statement, ev.get("pmid"))
            if pmid is None:
                continue
            quote = _quote(ev.get("quote"))
            a = abstracts[pmid]
            row = ProposalRow(
                statement.statement_id,
                pmid,
                prompt_version,
                model,
                proposed,
                quote,
                quote_found(quote, a.title, a.abstract),
                not errors,
                list(errors),
                claim,
            )
            key = (statement.statement_id, pmid, canonical(proposed))
            if key in proposals:
                problems["duplicate proposal"] += 1
                if proposals[key].quote_valid or not row.quote_valid:
                    continue
            proposals[key] = row

    if unreadable:
        raise ValueError("; ".join(f"{n} {what}" for what, n in sorted(unreadable.items())))
    for sid, pmid in sorted(request.combos - labels.keys()):
        labels[(sid, pmid)] = LabelRow(sid, pmid, prompt_version, model, "no_evidence")
    return Parsed(
        [labels[k] for k in sorted(labels)], [proposals[k] for k in sorted(proposals)], problems
    )


# --- Running the calls ----------------------------------------------------------------------


@dataclass
class _Stats:
    calls: int = 0
    failed: int = 0
    labels: Counter = field(default_factory=Counter)
    invalid_quotes: int = 0
    proposals_valid: int = 0
    proposals_invalid: int = 0
    problems: Counter = field(default_factory=Counter)
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    cost: float | None = None
    pairs_called: set[str] = field(default_factory=set)

    def add_usage(self, usage: llm.Usage) -> None:
        self.input_tokens += usage.input_tokens
        self.cached_tokens += usage.cached_tokens
        self.output_tokens += usage.output_tokens
        if usage.cost is not None:
            self.cost = (self.cost or 0.0) + usage.cost


def _messages(
    plans: list[tuple[PairJob, list[Request]]], pubmed: PubMedCache, qualifiers: Qualifiers
) -> Iterator[tuple[PairJob, Request, dict[str, Abstract], str]]:
    for job, requests in plans:
        if not requests:
            continue
        abstracts = pubmed.get_abstracts(p for r in requests for p in r.pmids)
        offered = {s.statement_id: qualifiers.offered(job, s) for s in job.statements}
        for request in requests:
            yield job, request, abstracts, build_message(job, request, abstracts, offered)


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
    max_output_tokens: int,
    reasoning_effort: str | None,
) -> _Stats:
    stats = _Stats()
    total = sum(len(r) for _, r in plans)
    every = max(1, total // 20)

    def call(user: str):
        return client.complete_json(
            system, user, max_output_tokens=max_output_tokens, reasoning_effort=reasoning_effort
        )

    def finish(future: Future, job: PairJob, request: Request, abstracts: dict) -> None:
        stats.calls += 1
        stats.pairs_called.add(job.pair_id)
        error = None
        parsed = None
        # Any exception is a failed call, logged and retried by the next run: raising here
        # would lose the replies of the calls still in flight.
        try:
            reply, usage = future.result()
        except Exception as e:
            usage = getattr(e, "usage", None) or llm.Usage()
            error = str(e) if isinstance(e, llm.LLMError) else f"{type(e).__name__}: {e}"
            error = error or type(e).__name__
        else:
            try:
                parsed = parse_reply(
                    reply,
                    request,
                    abstracts,
                    prompt_version=prompt_version,
                    model=model,
                    check=lambda s, q: qualifiers.errors(job, s, q),
                )
            except ValueError as e:
                error = f"unreadable reply: {e}"
            except Exception as e:
                error = f"could not read the reply: {type(e).__name__}: {e}"
        stats.add_usage(usage)
        if parsed is not None:
            labels.put_labels(parsed.labels)
            labels.put_proposals(parsed.proposals)
            for row in parsed.labels:
                stats.labels[row.label] += 1
                stats.invalid_quotes += row.label in DECISIVE and not row.quote_valid
            for row in parsed.proposals:
                if row.valid:
                    stats.proposals_valid += 1
                else:
                    stats.proposals_invalid += 1
            stats.problems.update(parsed.problems)
        else:
            stats.failed += 1
            _warn(f"{request.pair_id}: {error}")
        labels.log_call(
            CallRow(
                step="label",
                pair_id=request.pair_id,
                model=model,
                prompt_version=prompt_version,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost=usage.cost,
                seconds=usage.seconds,
                error=error,
            )
        )
        if stats.calls % every == 0 or stats.calls == total:
            cost = f", ${stats.cost:.4f}" if stats.cost is not None else ""
            print(
                f"label: {stats.calls}/{total} calls ({stats.failed} failed{cost})",
                file=sys.stderr,
            )

    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    pending: dict[Future, tuple[PairJob, Request, dict]] = {}
    try:
        for job, request, abstracts, user in _messages(plans, pubmed, qualifiers):
            pending[pool.submit(call, user)] = (job, request, abstracts)
            # A bounded window: messages are built only as calls free up.
            while len(pending) >= 2 * max(1, workers):
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    finish(future, *pending.pop(future))
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                finish(future, *pending.pop(future))
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown()
    return stats


# --- Sample files ---------------------------------------------------------------------------


def _quote_note(valid: bool | None) -> str:
    return "" if valid is None else (" (quote found)" if valid else " (QUOTE NOT FOUND)")


def write_sample(
    work: Path,
    jobs: list[PairJob],
    labels: LabelCache,
    pubmed: PubMedCache,
    *,
    model: str,
    prompt_version: str,
    seed: int,
) -> tuple[Path, Path]:
    """`label_sample.md`, what the agent reads to tune the prompt, and `label_sample.jsonl`,
    one line per (statement, PMID) in the gold format with `gold_label` to fill in."""
    md = [
        "# Label sample",
        "",
        f"Model `{model}`, prompt `{prompt_version}`; {len(jobs)} pairs chosen with seed {seed}.",
        "Each abstract lists the statements it was labeled for; no_evidence ones are grouped.",
        "Texts are in label_sample.jsonl.",
        "",
    ]
    rows: list[dict[str, Any]] = []
    for n, job in enumerate(jobs, 1):
        local = {s.statement_id: f"S{i}" for i, s in enumerate(job.statements, 1)}
        found = {(r.statement_id, r.pmid): r for r in labels.labels(prompt_version, model, local)}
        proposed = defaultdict(list)
        for p in labels.proposals(prompt_version, model, local):
            proposed[(p.statement_id, p.pmid)].append(p)
        abstracts = pubmed.get_abstracts(job.pmids)
        combos = job.combos()
        names = [e.name or e.id for e in job.entities]
        md += [f"## {n}. {names[0]} and {names[1]}", "", f"Pair `{job.pair_id}`", ""]
        for e in job.entities:
            synonyms = "; ".join(job.synonyms.get(e.id, [])[:MAX_SYNONYMS_SHOWN]) or "none"
            category = e.category[0].removeprefix("biolink:") if e.category else "unknown"
            md.append(f"- {e.name or e.id} ({e.id}; {category}); synonyms: {synonyms}")
        md += ["", "Statements:", ""]
        for s in job.statements:
            quals = _qualifier_text(normalize_qualifiers(s.qualifiers) or {})
            md.append(
                f"- **{local[s.statement_id]}** {job.sentences[s.statement_id]} "
                f"(`{s.predicate}`; qualifiers: {quals}; `{s.statement_id}`)"
            )
        md.append("")
        for pmid in job.pmids:
            a = abstracts.get(pmid) or Abstract(pmid, "")
            year = f" ({a.year})" if a.year else ""
            md += [f"### PMID:{pmid}{year}: {' '.join(a.title.split())}", ""]
            quiet: dict[str, list[str]] = defaultdict(list)
            for s in job.statements:
                key = (s.statement_id, pmid)
                if key not in combos:
                    continue
                row = found.get(key)
                sid = local[s.statement_id]
                if row is None or row.label == "no_evidence":
                    quiet["not labeled" if row is None else "no_evidence"].append(sid)
                else:
                    claim = f", {row.claim}" if row.claim else ""
                    quote = f': "{row.quote}"' if row.quote else ""
                    md.append(
                        f"- {sid}: **{row.label}**{claim}{quote}{_quote_note(row.quote_valid)}"
                    )
                for p in proposed.get(key, []):
                    verdict = "valid" if p.valid else "INVALID: " + "; ".join(p.errors)
                    claim = f", {p.claim}" if p.claim else ""
                    md.append(
                        f"- {sid} proposal `{json.dumps(p.qualifiers, sort_keys=True)}`{claim} "
                        f'({verdict}): "{p.quote or ""}"{_quote_note(p.quote_valid)}'
                    )
                rows.append(
                    {
                        "statement_id": s.statement_id,
                        "pair_id": job.pair_id,
                        "sentence": job.sentences[s.statement_id],
                        "pmid": pmid,
                        "title": a.title,
                        "abstract": a.abstract,
                        "label": row.label if row else None,
                        "quote": row.quote if row else None,
                        "quote_valid": row.quote_valid if row else None,
                        "claim": row.claim if row else None,
                        "gold_label": None,
                    }
                )
            for what, sids in quiet.items():
                md.append(f"- {what}: {', '.join(sids)}")
            md.append("")
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
    gold: Path | None = None,
    abstracts_per_call: int = config.ABSTRACTS_PER_CALL,
    max_output_tokens: int = MAX_OUTPUT_TOKENS,
    reasoning_effort: str | None = REASONING_EFFORT,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Label the pairs' (statement, PMID) combinations that have no label yet for this model
    and prompt, and return a summary. `client` is anything with `Client.complete_json`; by
    default a `llm.Client` for `model`, built only if there is something to send.

    Raises FileNotFoundError for a missing input, LLMError for a missing API key.
    """
    work, cache_dir, prompt = Path(work), Path(cache_dir), Path(prompt)
    inputs = read_inputs(work)
    pubmed_path = cache_dir / config.PUBMED_CACHE
    if not pubmed_path.exists():
        raise FileNotFoundError(f"{pubmed_path} is missing: run `kgb-augment pubmed` first")
    system, version = label_prompt(prompt)
    info = llm.load_model(model)
    gold_items = read_gold(gold) if gold is not None else None

    summary: dict[str, Any] = {
        "model": model,
        "prompt_version": version,
        "prompt": str(prompt),
        "dry_run": dry_run,
    }
    labels_path = cache_dir / config.LABEL_CACHE
    with PubMedCache(pubmed_path) as pubmed:
        jobs, skipped = build_jobs(inputs, pubmed, gold_items)
        if limit is not None:
            jobs = jobs[: max(0, limit)]
        if sample is not None:
            picked = random.Random(seed).sample(range(len(jobs)), min(max(0, sample), len(jobs)))
            jobs = [jobs[i] for i in sorted(picked)]
        # A dry run reads the label cache but doesn't create it.
        labels = LabelCache(labels_path) if not dry_run or labels_path.exists() else None
        try:
            plans = []
            for job in jobs:
                ids = [s.statement_id for s in job.statements]
                done = labels.labeled(version, model, ids) if labels else set()
                plans.append((job, plan_requests(job, done, abstracts_per_call)))
            calls = sum(len(r) for _, r in plans)
            summary |= {
                "pairs": len(jobs),
                "pairs_to_call": sum(bool(r) for _, r in plans),
                "skipped": dict(skipped),
                "requests": calls,
                "combinations": sum(len(q.combos) for _, r in plans for q in r),
            }
            qualifiers = Qualifiers()
            if dry_run:
                summary |= _dry_run(work, plans, pubmed, qualifiers, system, info, version)
                return summary
            if calls and client is None:
                client = llm.Client(info)
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
                max_output_tokens=max_output_tokens,
                reasoning_effort=reasoning_effort,
            )
            # A sample or a gold run tries a prompt or a model out, and a failed run is
            # incomplete: only a successful full (or --limit) run names what review and step 6
            # use.
            written = (
                sample is None and gold is None and not too_many_failed(stats.calls, stats.failed)
            )
            if written:
                _write_run(work, model, version, prompt)
            summary["label_run_written"] = written
            if sample is not None:
                md, jsonl = write_sample(
                    work, jobs, labels, pubmed, model=model, prompt_version=version, seed=seed
                )
                summary["sample_files"] = [str(md), str(jsonl)]
            usage = labels.usage("label", model, version)
        finally:
            if labels is not None:
                labels.close()
    summary |= {
        "calls": stats.calls,
        "calls_ok": stats.calls - stats.failed,
        "calls_failed": stats.failed,
        "pairs_called": len(stats.pairs_called),
        "labels": {k: stats.labels[k] for k in LABELS},
        "invalid_quotes": stats.invalid_quotes,
        "proposals_valid": stats.proposals_valid,
        "proposals_invalid": stats.proposals_invalid,
        "problems": dict(stats.problems),
        "input_tokens": stats.input_tokens,
        "cached_tokens": stats.cached_tokens,
        "output_tokens": stats.output_tokens,
        "cost": stats.cost,
        "usage": usage,
    }
    return summary


def _dry_run(
    work: Path,
    plans: list[tuple[PairJob, list[Request]]],
    pubmed: PubMedCache,
    qualifiers: Qualifiers,
    system: str,
    info: llm.ModelInfo,
    version: str,
) -> dict[str, Any]:
    """Build every request without sending any: write the first to REQUEST_EXAMPLE and
    estimate tokens (characters / 4) and cost. The system prompt comes first in every request,
    so after the first call it is billed at the cache-read price once it is long enough for
    the service's prompt caching."""
    chars = 0
    calls = 0
    example = None
    for _, _, _, user in _messages(plans, pubmed, qualifiers):
        calls += 1
        chars += len(system) + len(user)
        if example is None:
            example = work / REQUEST_EXAMPLE
            work.mkdir(parents=True, exist_ok=True)
            example.write_text(
                f"model: {info.id}\nprompt version: {version}\n\n"
                f"=== instructions (system) ===\n\n{system.rstrip()}\n\n"
                f"=== input (user) ===\n\n{user}\n",
                encoding="utf-8",
            )
    tokens = chars // 4
    system_tokens = len(system) // 4
    cached = max(0, calls - 1) * system_tokens if system_tokens >= CACHED_PREFIX_TOKENS else 0
    output = calls * ESTIMATED_OUTPUT_TOKENS
    return {
        "calls": 0,
        "estimated_input_tokens": tokens,
        "estimated_cached_tokens": cached,
        "estimated_output_tokens": output,
        "estimated_cost": llm.cost_of(info, tokens, output, cached),
        "request_example": str(example) if example else None,
    }


def _write_run(work: Path, model: str, version: str, prompt: Path) -> None:
    """Name this run in `label_run.json`, keeping its review only if it was of this run."""
    run = records.LabelRun(model=model, prompt_version=version, prompt=str(prompt))
    old = records.read_label_run(work)
    if old is not None and (old.model, old.prompt_version) == (model, version):
        run.reviewer, run.review_prompt_version = old.reviewer, old.review_prompt_version
    records.write_label_run(work, run)


def _money(value: float | None) -> str:
    return "unknown" if value is None else f"${value:.4f}"


def print_summary(s: dict[str, Any]) -> None:
    print(f"label: model {s['model']}, prompt {s['prompt_version']} ({s['prompt']})")
    skipped = ", ".join(f"{n} {what}" for what, n in s["skipped"].items())
    print(
        f"label: {s['pairs']} pairs, {s['pairs_to_call']} with combinations to label"
        + (f"; skipped: {skipped}" if skipped else "")
    )
    if s["dry_run"]:
        print(
            f"label: dry run: {s['requests']} calls for {s['combinations']} combinations, "
            f"~{s['estimated_input_tokens']:,} input tokens (~{s['estimated_cached_tokens']:,} "
            f"of them cached); est. cost {_money(s['estimated_cost'])} with "
            f"~{ESTIMATED_OUTPUT_TOKENS} output tokens a call"
        )
        if s["request_example"]:
            print(f"label: first request in {s['request_example']}")
        return
    print(f"label: calls: {s['calls']} ({s['calls_ok']} ok, {s['calls_failed']} failed)")
    counts = ", ".join(f"{k} {v}" for k, v in s["labels"].items())
    print(f"label: labels: {counts}; {s['invalid_quotes']} with a quote not found")
    print(f"label: proposals: {s['proposals_valid']} valid, {s['proposals_invalid']} invalid")
    if s["problems"]:
        print("label: ignored: " + ", ".join(f"{n} {what}" for what, n in s["problems"].items()))
    per_pair = (
        s["cost"] / s["pairs_called"] if s["cost"] is not None and s["pairs_called"] else None
    )
    print(
        f"label: tokens: {s['input_tokens']:,} in ({s['cached_tokens']:,} cached), "
        f"{s['output_tokens']:,} out; cost {_money(s['cost'])} ({_money(per_pair)} per pair)"
    )
    u = s["usage"]
    total_per_pair = u["cost"] / u["pairs"] if u["cost"] is not None and u["pairs"] else None
    print(
        f"label: all label calls of this model and prompt: {u['calls']} calls "
        f"({u['errors']} failed) over {u['pairs']} pairs, cost {_money(u['cost'])} "
        f"({_money(total_per_pair)} per pair)"
    )
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
        help="system prompt file; its content sets the prompt version (default: %(default)s)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=config.WORKERS,
        help="concurrent calls (default: %(default)s)",
    )
    parser.add_argument("--limit", type=int, help="label only the first N pairs")
    parser.add_argument(
        "--sample",
        type=int,
        help=f"label N pairs chosen at random and write {SAMPLE_MD} and {SAMPLE_JSONL}",
    )
    parser.add_argument("--seed", type=int, default=0, help="seed for --sample (default: 0)")
    parser.add_argument(
        "--gold", type=Path, help="label only the (statement_id, pmid) items of a gold JSONL file"
    )
    parser.add_argument(
        "--abstracts-per-call",
        type=int,
        default=config.ABSTRACTS_PER_CALL,
        help="most abstracts sent in one call (default: %(default)s)",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=MAX_OUTPUT_TOKENS,
        help="output token cap per call, reasoning included (default: %(default)s)",
    )
    parser.add_argument(
        "--reasoning-effort",
        default=REASONING_EFFORT,
        help="reasoning effort for reasoning models, or 'none' to omit (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=f"send nothing: write the first request to {REQUEST_EXAMPLE}, estimate the cost",
    )


def run(args: argparse.Namespace) -> int:
    effort = args.reasoning_effort
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
            gold=args.gold,
            abstracts_per_call=args.abstracts_per_call,
            max_output_tokens=args.max_output_tokens,
            reasoning_effort=None if not effort or effort.lower() == "none" else effort,
            dry_run=args.dry_run,
        )
    except (FileNotFoundError, ValueError, llm.LLMError) as e:
        print(f"label: error: {e}", file=sys.stderr)
        return 1
    print_summary(summary)
    calls, failed = summary["calls"], summary.get("calls_failed", 0)
    if too_many_failed(calls, failed):
        print(
            f"label: error: {failed} of {calls} calls failed (more than "
            f"{MAX_FAILED_SHARE:.0%}); {records.LABEL_RUN} not updated; rerun to retry, labels "
            "already cached are kept",
            file=sys.stderr,
        )
        return 1
    if failed:
        _warn(f"{failed} of {calls} calls failed; rerun to retry; labels already cached are kept")
    return 0
