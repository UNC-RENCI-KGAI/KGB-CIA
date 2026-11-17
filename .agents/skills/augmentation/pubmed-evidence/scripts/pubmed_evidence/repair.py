"""The end of step 3: repair the evidence sentences the label model misquoted, with code first
and the repair model only for what code can't place.

Once the claims are in and checked, each claim that passes every check but the sentence's, and
whose sentence wasn't found, is repaired by code (`quote_align.repair`), and by the repair
model (config.REPAIR_MODEL) if code can't place its quote. A claim code placed is never sent to
the model, even when the passage doesn't name both entities. `sentence_repairs` gives the
repaired sentences that count.

DESIGN.md, "Quotes and repair", has the two passes, which repairs count and the measurements;
reference/formats.md, "Repair reply", the model's reply; and reference/work-files.md the
`claim_repairs` table.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pubmed_evidence import config, llm, quote_align, records, sentences
from pubmed_evidence.cache import ClaimRepairRow, LabelCache, PubMedCache, claim_id
from pubmed_evidence.quotes import quote_found
from shared.pubmed import Abstract

# The repairer code repairs are stored under, with quote_align.VERSION as their version.
CODE_REPAIRER = "code"
# Version of the code that writes the user message; with the reply schema, part of the repair
# prompt version.
FORMAT_VERSION = "repair-message-2"
REPLY_SCHEMA = llm.Schema(
    "repair_reply", llm.strict_object({"quote": llm.nullable({"type": "string"})})
)

ClaimKey = tuple[str, str, str]  # (pair_id, pmid, claim_id)


def repair_prompt(prompt: Path = config.REPAIR_PROMPT) -> tuple[str, str]:
    """(system prompt, repair prompt version), the version covering FORMAT_VERSION, the reply
    schema and the reasoning effort config.REPAIR_MODEL sends (its own in config.MODELS; the
    pass takes no override) too, so repairs made at one effort aren't reused at another."""
    effort = llm.run_effort(config.REPAIR_MODEL)
    extra = f"{FORMAT_VERSION}\0{REPLY_SCHEMA.text()}\0effort={effort}"
    return llm.load_prompt(prompt, extra=extra)


def code_current() -> tuple[str, str]:
    """(repairer, version) of the repairs code makes now: 'code' and quote_align.VERSION."""
    return CODE_REPAIRER, quote_align.VERSION


def model_current() -> tuple[str, str]:
    """(repairer, repair prompt version) of the repairs the model makes now: config's repair
    model with the skill's repair prompt."""
    return config.REPAIR_MODEL, repair_prompt()[1]


def current() -> list[tuple[str, str]]:
    """(repairer, version) of the repairs a label run makes now, in priority order: the
    code's, then the repair model's."""
    return [code_current(), model_current()]


def _describe(order: Sequence[tuple[str, str]]) -> str:
    return ", then ".join(f"{repairer} ({version})" for repairer, version in order)


def stale(run: records.LabelRun) -> str | None:
    """A warning when the repairs the run names aren't the ones a label run would make now (the
    aligner, the repair prompt or the model changed since, or the run names no code repair);
    None if they are, or the run names none."""
    order = run.repair_order()
    if not order or order == current():
        return None
    return (
        f"the quote repairs that count are {_describe(order)}, which label_run.json names; "
        f"rerun `kgb-augment label` to repair the quotes with {_describe(current())}"
    )


@dataclass
class Item:
    """A claim of the first-pass run whose evidence sentence wasn't found, with the repairs it
    has so far."""

    pair_id: str
    pmid: str
    claim_id: str
    claim: dict[str, Any]
    code: ClaimRepairRow | None = None  # its code repair at quote_align.VERSION, once made
    model_answered: bool = False  # the repair model, with the skill's prompt, answered it

    @property
    def key(self) -> ClaimKey:
        return (self.pair_id, self.pmid, self.claim_id)

    @property
    def for_model(self) -> bool:
        """Whether it goes to the repair model: code tried it without placing the sentence,
        and the model hasn't answered it."""
        return self.code is not None and not self.code.sentence_valid and not self.model_answered


def _key(row: ClaimRepairRow) -> ClaimKey:
    return (row.pair_id, row.pmid, row.claim_id)


def pending(
    labels: LabelCache, prompt_version: str, model: str, pair_ids: Iterable[str]
) -> list[Item]:
    """The run's claims for these pairs that pass every check but the sentence's, whose
    sentence wasn't found, and that still need a repair: code hasn't tried them (at
    quote_align.VERSION), or it didn't place the sentence and the repair model
    (config.REPAIR_MODEL with the skill's prompt) hasn't answered. The checks are the ones the
    label step just stored (`label.check_pairs`)."""
    ids = list(dict.fromkeys(pair_ids))
    code = {_key(r): r for r in labels.claim_repairs(prompt_version, model, *code_current(), ids)}
    answered = {_key(r) for r in labels.claim_repairs(prompt_version, model, *model_current(), ids)}
    wanted = {
        (c.pair_id, c.pmid, c.claim_id)
        for c in labels.claim_checks(prompt_version, model, ids)
        if c.valid and not c.sentence_valid
    }
    wanted = {
        k for k in wanted if k not in code or (not code[k].sentence_valid and k not in answered)
    }
    if not wanted:
        return []
    pairs = sorted({pair for pair, _, _ in wanted})
    items = {}
    for answer in labels.abstract_claims(prompt_version, model, pairs):
        for claim in answer.claims:
            key = (answer.pair_id, answer.pmid, claim_id(claim))
            if key in wanted:
                items[key] = Item(*key, claim, code.get(key), key in answered)
    return [items[k] for k in sorted(items)]


def entity_terms(
    node: str, names: Mapping[str, str], synonyms: Mapping[str, Sequence[str]]
) -> list[str]:
    """What an abstract may call a node: its name, then its synonyms, once each."""
    terms = [names.get(node), *synonyms.get(node, ())]
    return list(dict.fromkeys(t.strip() for t in terms if t and t.strip()))


def code_repair(
    item: Item,
    abstract: Abstract,
    names: Mapping[str, str],
    synonyms: Mapping[str, Sequence[str]],
    prompt_version: str,
    model: str,
) -> tuple[ClaimRepairRow, dict[str, Any]]:
    """Code's repair of one claim, as stored, and what the aligner found (`quote_align.repair`'s
    info): the passage, whether `quote_found` finds it, or none."""
    statement = item.claim["statement"]
    quote, info = quote_align.repair(
        item.claim["evidence_sentence"],
        abstract.title,
        abstract.abstract,
        [entity_terms(statement[side], names, synonyms) for side in ("subject", "object")],
        sentences.claim_sentence(item.claim, names),
    )
    valid = quote_found(quote, abstract.title, abstract.abstract) if quote else None
    row = ClaimRepairRow(
        item.pair_id,
        item.pmid,
        prompt_version,
        model,
        item.claim_id,
        *code_current(),
        quote,
        valid,
        answered_by=CODE_REPAIRER,
    )
    return row, info


def build_message(item: Item, names: dict[str, str], abstract: Abstract) -> str:
    """The user message of one repair: the claim, in words and as stored, the sentence that
    wasn't found, then the abstract. Its layout is FORMAT_VERSION."""
    structured = {k: v for k, v in item.claim.items() if k != "evidence_sentence"}
    statement = item.claim["statement"]
    lines = [
        "## Claim",
        "",
        f"{item.claim['polarity'].capitalize()} claim: "
        f"{sentences.claim_sentence(item.claim, names)}",
        f"Entities: {names.get(statement['subject']) or statement['subject']} "
        f"({statement['subject']}) and {names.get(statement['object']) or statement['object']} "
        f"({statement['object']})",
        f"Structured: {json.dumps(structured, ensure_ascii=False, sort_keys=True)}",
        "Evidence sentence not found in the abstract: "
        f"{json.dumps(item.claim['evidence_sentence'], ensure_ascii=False)}",
    ]
    year = f", {abstract.year}" if abstract.year else ""
    lines += [
        "",
        f"## Abstract (PMID:{item.pmid}{year})",
        "",
        f"Title: {' '.join(abstract.title.split())}",
        f"Abstract: {abstract.abstract.strip()}",
    ]
    return "\n".join(lines) + "\n"


def repair_quotes(
    labels: LabelCache,
    pubmed: PubMedCache,
    *,
    prompt_version: str,
    model: str,
    pair_ids: Iterable[str],
    names: dict[str, str],
    synonyms: Mapping[str, Sequence[str]] | None = None,
    client: Any = None,
    workers: int = config.WORKERS,
) -> dict[str, Any]:
    """Repair the evidence sentences of the first-pass run's claims for these pairs that still
    need a repair (`pending`), with code first, then with the repair model those code couldn't
    place, and return a summary. `names` maps node ids to names, for the claim in words, and
    `synonyms` node ids to the other names the label step gave them (synonyms and MeSH
    headings), which code looks for in the abstract. `client` (anything with a client's `call`)
    is built for config.REPAIR_MODEL if not given and there is something to send it; without
    its credentials the model's pass is skipped, and the summary says why. The summary's
    `repairers` are the repairs that count after this pass, in priority order: the code's, then
    the model's unless its pass was skipped."""
    system, version = repair_prompt()
    summary: dict[str, Any] = {
        "repairers": [code_current(), (config.REPAIR_MODEL, version)],
        "code_version": quote_align.VERSION,
        "repairer": config.REPAIR_MODEL,
        "repair_prompt_version": version,
        "pending": 0,
        "no_abstract": 0,
        "code_tried": 0,
        "code_found": 0,
        "code_none": 0,
        "code_unresolved": 0,
        "for_model": 0,
        "calls": 0,
        "failed_calls": 0,
        "found": 0,
        "none": 0,
        "not_found": 0,
        "cost": None,
        "skipped": None,
    }
    items = pending(labels, prompt_version, model, pair_ids)
    abstracts = pubmed.get_abstracts(i.pmid for i in items)
    summary["no_abstract"] = sum(i.pmid not in abstracts for i in items)
    items = [i for i in items if i.pmid in abstracts]
    summary["pending"] = len(items)

    rows = []
    for item in items:
        if item.code is not None:
            continue
        row, info = code_repair(
            item, abstracts[item.pmid], names, synonyms or {}, prompt_version, model
        )
        item.code = row
        rows.append(row)
        summary["code_found" if row.sentence_valid else "code_none"] += 1
        summary["code_unresolved"] += bool(row.sentence_valid and info.get("unresolved"))
    summary["code_tried"] = len(rows)
    if rows:
        labels.put_claim_repairs(rows)

    items = [i for i in items if i.for_model]
    summary["for_model"] = len(items)
    if not items:
        return summary
    if client is None:
        try:
            client = llm.client_for(llm.model(config.REPAIR_MODEL))
        except llm.LLMError as e:
            summary["skipped"] = str(e)
            summary["repairers"] = [code_current()]
            return summary

    def calls() -> Iterator[llm.Call]:
        for item in items:
            abstract = abstracts[item.pmid]
            user = build_message(item, names, abstract)
            yield llm.Call(item.pair_id, user, (item, abstract))

    def on_result(call: llm.Call, reply: Any, usage: llm.Usage) -> str | None:
        item, abstract = call.context
        quote = reply["quote"].strip() or None if isinstance(reply["quote"], str) else None
        valid = quote_found(quote, abstract.title, abstract.abstract) if quote else None
        labels.put_claim_repairs(
            [
                ClaimRepairRow(
                    item.pair_id,
                    item.pmid,
                    prompt_version,
                    model,
                    item.claim_id,
                    config.REPAIR_MODEL,
                    version,
                    quote,
                    valid,
                    answered_by=usage.model,
                )
            ]
        )
        summary["found" if valid else "none" if quote is None else "not_found"] += 1
        return None

    tally = llm.run_calls(
        calls(),
        client,
        on_result,
        system=system,
        schema=REPLY_SCHEMA,
        cache=labels,
        step="repair",
        model=config.REPAIR_MODEL,
        prompt_version=version,
        total=len(items),
        workers=workers,
    )
    summary |= {"calls": tally.calls, "failed_calls": tally.failed, "cost": tally.cost}
    return summary


def chosen_repairs(
    labels: LabelCache,
    prompt_version: str,
    model: str,
    repairers: Sequence[tuple[str, str]] | None,
    pair_ids: Iterable[str] | None = None,
) -> dict[ClaimKey, ClaimRepairRow]:
    """The repair that counts for each of the first-pass run's claims these repairers repaired,
    by (pair_id, pmid, claim_id): of the repairs by `repairers` ((repairer, repair prompt
    version) in priority order), the first whose sentence was found, else the last made."""
    ids = None if pair_ids is None else list(pair_ids)
    out: dict[ClaimKey, ClaimRepairRow] = {}
    for repairer, version in repairers or ():
        for row in labels.claim_repairs(prompt_version, model, repairer, version, ids):
            held = out.get(_key(row))
            if held is None or not (held.sentence_valid and held.evidence_sentence):
                out[_key(row)] = row
    return out


def sentence_repairs(
    labels: LabelCache,
    prompt_version: str,
    model: str,
    repairers: Sequence[tuple[str, str]] | None,
    pair_ids: Iterable[str] | None = None,
) -> dict[ClaimKey, str]:
    """The first-pass run's repaired evidence sentences that were found, by (pair_id, pmid,
    claim_id), from the repairs of `repairers` ((repairer, repair prompt version) in priority
    order, as `LabelRun.repair_order` gives them): each claim's from the first whose sentence
    was found. None without repairers. A claim's sentence counts as found when its check found
    it or one of these repairs did."""
    return {
        key: row.evidence_sentence
        for key, row in chosen_repairs(labels, prompt_version, model, repairers, pair_ids).items()
        if row.sentence_valid and row.evidence_sentence
    }


def print_summary(s: dict[str, Any], step: str = "label") -> None:
    if not s["pending"]:
        return
    placed = f"code ({s['code_version']}) placed {s['code_found']} of {s['code_tried']}"
    if s["code_unresolved"]:
        placed += f" ({s['code_unresolved']} with no sentence naming both entities, kept)"
    print(
        f"{step}: quote repair: {s['pending']} evidence sentences not found and not yet "
        f"repaired; {placed}; {s['for_model']} left for {s['repairer']}"
    )
    if s["skipped"]:
        print(
            f"{step}: warning: {s['repairer']}'s quote repair skipped ({s['skipped']}); the "
            f"{s['for_model']} evidence sentences code couldn't place count as not found until "
            "a run repairs them",
            file=sys.stderr,
        )
        return
    if not s["for_model"]:
        return
    cost = "unknown" if s["cost"] is None else f"${s['cost']:.4f}"
    print(
        f"{step}: quote repair by {s['repairer']}: repaired {s['found']}, none {s['none']}, "
        f"still not found {s['not_found']}; {s['calls']} calls, {s['failed_calls']} failed "
        f"(retried next run); cost {cost}"
    )
