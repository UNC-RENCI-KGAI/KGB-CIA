"""The end of step 3: a stronger model repairs the evidence sentences code couldn't find.

About 1% of the bulk model's quotes aren't in the abstract: it paraphrased, shortened or
stitched them. A claim whose evidence sentence isn't found counts for nothing (rule 1 in
DESIGN.md's step 6). So once the claims are in and checked, each claim that passes every check
but the sentence's, and whose sentence wasn't found, gets one small call to the repair model
(config.REPAIR_MODEL, the review model): the claim, the sentence that wasn't found, and the
abstract. The reply is a verbatim quote or null, and code checks it as it checks the first
pass's. A repair supplies a sentence and never changes the claim, which stays as stored.

Repairs go to the label cache's `claim_repairs` table by claim (`cache.claim_id`) under
(repairer, repair prompt version), one answered attempt per claim: a null, or a sentence that
still isn't found, is kept and not asked again. A failed call stores nothing, so the next run
asks again; until then the sentence counts as not found. Step 6 takes a claim's sentence from
its repair when the repair's sentence was found (`sentence_repairs`), from the repairs
`label_run.json` names, as it names the review.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pubmed_evidence import config, llm, records, sentences
from pubmed_evidence.cache import (
    Abstract,
    ClaimRepairRow,
    LabelCache,
    PubMedCache,
    claim_id,
)
from pubmed_evidence.quotes import quote_found

# Version of the code that writes the user message; with the reply schema, part of the repair
# prompt version.
FORMAT_VERSION = "repair-message-2"
REPLY_SCHEMA = llm.Schema(
    "repair_reply", llm.strict_object({"quote": llm.nullable({"type": "string"})})
)


def repair_prompt(prompt: Path = config.REPAIR_PROMPT) -> tuple[str, str]:
    """(system prompt, repair prompt version), the version covering FORMAT_VERSION, the reply
    schema and the reasoning effort config.REPAIR_MODEL sends (its own in config.MODELS; the
    pass takes no override) too, so repairs made at one effort aren't reused at another."""
    effort = llm.run_effort(config.REPAIR_MODEL)
    extra = f"{FORMAT_VERSION}\0{REPLY_SCHEMA.text()}\0effort={effort}"
    return llm.load_prompt(prompt, extra=extra)


def current() -> tuple[str, str]:
    """(repairer, repair prompt version) of the repairs a label run makes now: config's repair
    model with the skill's repair prompt."""
    return config.REPAIR_MODEL, repair_prompt()[1]


def stale(run: records.LabelRun) -> str | None:
    """A warning when the repairs the run names aren't the ones a label run would make now (the
    repair prompt or model changed since); None if they are, or the run names none."""
    if run.repairer is None or (run.repairer, run.repair_prompt_version) == current():
        return None
    repairer, version = current()
    return (
        f"the quote repairs that count are {run.repairer}'s with {run.repair_prompt_version}, "
        f"which label_run.json names; rerun `kgb-augment label` to repair the quotes with "
        f"{repairer} and {version}"
    )


@dataclass
class Item:
    """A claim of the first-pass run whose evidence sentence wasn't found."""

    pair_id: str
    pmid: str
    claim_id: str
    claim: dict[str, Any]

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.pair_id, self.pmid, self.claim_id)


def pending(
    labels: LabelCache, prompt_version: str, model: str, pair_ids: Iterable[str]
) -> list[Item]:
    """The run's claims for these pairs that pass every check but the sentence's, whose
    sentence wasn't found, and that have no repair yet by config.REPAIR_MODEL with the skill's
    prompt. The checks are the ones the label step just stored (`label.check_pairs`)."""
    ids = list(dict.fromkeys(pair_ids))
    done = {
        (r.pair_id, r.pmid, r.claim_id)
        for r in labels.claim_repairs(prompt_version, model, *current(), ids)
    }
    wanted = {
        (c.pair_id, c.pmid, c.claim_id)
        for c in labels.claim_checks(prompt_version, model, ids)
        if c.valid and not c.sentence_valid
    } - done
    if not wanted:
        return []
    pairs = sorted({pair for pair, _, _ in wanted})
    items = {}
    for answer in labels.abstract_claims(prompt_version, model, pairs):
        for claim in answer.claims:
            key = (answer.pair_id, answer.pmid, claim_id(claim))
            if key in wanted:
                items[key] = Item(*key, claim)
    return [items[k] for k in sorted(items)]


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
    client: Any = None,
    workers: int = config.WORKERS,
) -> dict[str, Any]:
    """Repair the evidence sentences of the first-pass run's claims for these pairs that have
    no repair yet (`pending`), and return a summary. `names` maps node ids to names, for the
    claim in words. `client` (anything with a client's `call`) is built for config.REPAIR_MODEL
    if not given and there is something to send; without its credentials the pass is skipped,
    and the summary says why."""
    system, version = repair_prompt()
    summary: dict[str, Any] = {
        "repairer": config.REPAIR_MODEL,
        "repair_prompt_version": version,
        "pending": 0,
        "no_abstract": 0,
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
    if not items:
        return summary
    if client is None:
        try:
            client = llm.client_for(llm.model(config.REPAIR_MODEL))
        except llm.LLMError as e:
            summary["skipped"] = str(e)
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


def sentence_repairs(
    labels: LabelCache,
    prompt_version: str,
    model: str,
    repairer: str | None,
    repair_prompt_version: str | None,
    pair_ids: Iterable[str] | None = None,
) -> dict[tuple[str, str, str], str]:
    """The first-pass run's repaired evidence sentences that were found, by (pair_id, pmid,
    claim_id), from the repairs of this repairer and repair prompt version; none without
    them. A claim's sentence counts as found when its check found it or its repair here was."""
    if repairer is None or repair_prompt_version is None:
        return {}
    rows = labels.claim_repairs(prompt_version, model, repairer, repair_prompt_version, pair_ids)
    return {
        (r.pair_id, r.pmid, r.claim_id): r.evidence_sentence
        for r in rows
        if r.sentence_valid and r.evidence_sentence
    }


def print_summary(s: dict[str, Any], step: str = "label") -> None:
    if s["skipped"]:
        print(
            f"{step}: warning: quote repair skipped ({s['skipped']}); the {s['pending']} evidence "
            "sentences not found count as not found until a run repairs them",
            file=sys.stderr,
        )
        return
    if not s["pending"]:
        return
    cost = "unknown" if s["cost"] is None else f"${s['cost']:.4f}"
    print(
        f"{step}: quote repair by {s['repairer']}: {s['pending']} evidence sentences not found; "
        f"repaired "
        f"{s['found']}, none {s['none']}, still not found {s['not_found']}; "
        f"{s['calls']} calls, {s['failed_calls']} failed (retried next run); cost {cost}"
    )
