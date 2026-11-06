"""Step 5: a stronger model reviews the claims that would count.

Step 6 refines each checked edge from its pair's claims (`refine.py`): an upheld claim adds its
PMID as support to each in-scope edge whose statement it states or narrows, and one more
specific than such an edge proposes its statement as a new edge. Each of those claims is one the
bulk model extracted, so a stronger, dearer model (config.REVIEW_MODEL) reads it again against
its abstract and upholds or rejects it (`upheld`, `reject`), with a reason. A rejected claim
counts for nothing.

The claims sent are those of each bundled pair (`bundle.py`) that would count if upheld
(`refine.would_count`): quote-checked, from a paper not retracted, not a hypothesis, and with a
statement narrower than or equal to an in-scope edge's. A positive claim's statements are its
own and those it also supports (`also_supports`); a negative claim's are only the latter.
Claims that only add a PMID to an edge are among them. Each claim's effect is independent of
the others', so one pass sends every such claim with no verdict yet. One whose call fails, or
that a reply leaves out, stays unreviewed: step 6 holds its pair, and the next run sends it
again.

Calls are per pair: its claims in batches split by abstract chunk (config.ABSTRACTS_PER_CALL),
then at config.REVIEW_ITEMS_PER_CALL claims, each abstract sent once per call. Verdicts go to
the label cache's `claim_reviews` table under (reviewer, review prompt version), so a rerun
reviews only what is new, and a new prompt, model or effort starts afresh: the review prompt
version hashes the prompt with the claim rules it includes, FORMAT_VERSION, the reply schema
and the reasoning effort. `label_run.json` then names the review step 6 applies.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple, get_args

from pubmed_evidence import bundle, config, llm, records, refine, repair, sentences
from pubmed_evidence.bundle import Bundles, ClaimKey
from pubmed_evidence.cache import Abstract, ClaimReviewRow, LabelCache, PubMedCache
from pubmed_evidence.graphio import chunks, pair_nodes
from pubmed_evidence.records import Entity, LabelRun
from shared.schema import ClaimContext, ClaimRecord, ReviewVerdict

# Version of the code that writes the user message. It is part of the review prompt version,
# with the reply schema and the effort, so a change to any of them starts a fresh review.
FORMAT_VERSION = "review-message-5"
VERDICTS: tuple[str, ...] = get_args(ReviewVerdict)
# A species as the extractor named it, from its NCBITaxon term.
SPECIES = {taxon: name for name, taxon in config.SPECIES_TAXA.items() if taxon}

REPLY_SCHEMA = llm.Schema(
    "review_reply",
    llm.strict_object(
        {
            "reviews": {
                "type": "array",
                "items": llm.strict_object(
                    {
                        "item": llm.positions("R", config.REVIEW_ITEMS_PER_CALL),
                        "verdict": {"type": "string", "enum": list(VERDICTS)},
                        "reason": {"type": "string"},
                    }
                ),
            }
        }
    ),
)


class ReviewError(RuntimeError):
    """A missing input or setting that stops the review before any call."""


def review_prompt(
    prompt: Path = config.REVIEW_PROMPT,
    model: str = config.REVIEW_MODEL,
    reasoning_effort: str | None = None,
) -> tuple[str, str]:
    """(system prompt, review prompt version) of a review with this prompt file, model and
    effort override: the version covers the prompt with the files it includes, FORMAT_VERSION,
    the reply schema and the effort sent (`llm.run_effort`). Raises LLMError for a model not in
    config.MODELS."""
    effort = llm.run_effort(model, reasoning_effort)
    extra = f"{FORMAT_VERSION}\0{REPLY_SCHEMA.text()}\0effort={effort}"
    return llm.load_prompt(prompt, extra=extra)


# --- The user message -----------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    """A claim to review: its key in the claim tables, and its record as the pair's bundle has
    it (its sentence the repair's, if it needed one)."""

    key: ClaimKey
    record: ClaimRecord

    @property
    def pair_id(self) -> str:
        return self.key[0]

    @property
    def pmid(self) -> str:
        return self.key[1]


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


def _entity(
    node: str, entities: Mapping[str, Entity], synonyms: Mapping[str, list[str]]
) -> dict[str, Any]:
    entity = entities.get(node)
    name = entity.name if entity and entity.name else node
    terms = synonyms.get(node) or (entity.synonyms if entity else [])
    seen = {name.lower()}
    others = []
    for term in terms:
        if term.lower() not in seen:
            seen.add(term.lower())
            others.append(term)
    category = entity.category[0].removeprefix("biolink:") if entity and entity.category else None
    return {"name": name, "category": category, "synonyms": others[: config.MAX_SYNONYMS]}


def _context(context: ClaimContext) -> dict[str, str | None]:
    """A claim's context as the extractor gave it: the species by its name, the anatomy as the
    abstract names it."""
    species = SPECIES.get(context.species or "", context.species)
    return {"species": species, "anatomy": context.anatomy_name, "notes": context.notes}


def structured(record: ClaimRecord) -> dict[str, Any]:
    """The claim as the extractor gave it, with the pair's ids for X and Y and each statement
    the way round Biolink reads it (`bundle.claim_record`)."""

    def statement(s) -> dict[str, Any]:
        return s.model_dump(exclude={"negated"}, exclude_none=True)

    return {
        "polarity": record.polarity,
        "statement": statement(record.statement),
        "also_supports": [statement(s) for s in record.also_supports],
        "claim_role": record.claim_role,
        "hedged": record.hedged,
        "negative_finding_type": record.negative_finding_type,
        "context": _context(record.context),
    }


def claim_text(record: ClaimRecord, names: Mapping[str, str]) -> str:
    """The claim in English: its statement, negated for a negative claim."""
    negated = record.polarity == "negative"
    return sentences.sentence(record.statement.model_copy(update={"negated": negated}), names)


def build_message(
    pair_id: str,
    batch: Sequence[tuple[str, Item]],
    entities: Mapping[str, Entity],
    synonyms: Mapping[str, list[str]],
    abstracts: Mapping[str, Abstract],
) -> str:
    """The user message of one call: the pair's entities, the claims under their local ids,
    then each abstract they're from, once. Its layout is FORMAT_VERSION."""
    names = {n: _entity(n, entities, synonyms)["name"] for n in pair_nodes(pair_id)}
    lines = ["## Entities", ""]
    for node in pair_nodes(pair_id):
        e = _entity(node, entities, synonyms)
        line = f"- {node}: {e['name']}"
        if e["category"]:
            line += f" ({e['category']})"
        if e["synonyms"]:
            line += f"; other names: {'; '.join(e['synonyms'])}"
        lines.append(line)
    lines += ["", "## Claims", ""]
    for local, item in batch:
        r = item.record
        lines += [f"### {local}: PMID:{item.pmid}", f"Claim: {claim_text(r, names)}"]
        lines += [f"Also supports: {sentences.sentence(s, names)}" for s in r.also_supports]
        lines += [
            f"Structured: {_json(structured(r))}",
            f"Evidence sentence: {_json(r.evidence_sentence)}",
            "",
        ]
    lines += ["## Abstracts", ""]
    for pmid in dict.fromkeys(item.pmid for _, item in batch):
        a = abstracts[pmid]
        year = f", {a.year}" if a.year else ""
        lines.append(f"### PMID:{pmid}{year}")
        if a.publication_types:
            lines.append(f"Publication types: {'; '.join(a.publication_types)}")
        lines += [f"Title: {' '.join(a.title.split())}", f"Abstract: {a.abstract.strip()}", ""]
    return "\n".join(lines).rstrip() + "\n"


def batches(groups: Mapping[str, Sequence[Item]]) -> list[tuple[str, list[tuple[str, Item]]]]:
    """The calls for each pair's items, with local ids R1, R2, ...: a pair whose items cite more
    than config.ABSTRACTS_PER_CALL abstracts is split, as the label step splits it, and so is
    one with more than config.REVIEW_ITEMS_PER_CALL items."""
    out = []
    for pair, items in groups.items():
        pmids = list(dict.fromkeys(i.pmid for i in items))
        for chunk in map(set, chunks(pmids, config.ABSTRACTS_PER_CALL)):
            batch = [i for i in items if i.pmid in chunk]
            for group in chunks(batch, config.REVIEW_ITEMS_PER_CALL):
                out.append((pair, [(f"R{n}", item) for n, item in enumerate(group, 1)]))
    return out


class Reply(NamedTuple):
    verdicts: dict[str, tuple[str, str]]  # local id -> (verdict, reason)
    repeated: int  # entries after an item's first verdict


def parse_reply(reply: dict[str, Any], ids: Mapping[str, Item]) -> Reply:
    """Verdicts by local id from a reply that fits REPLY_SCHEMA; an item's first verdict counts,
    and one the reply leaves out stays unreviewed. Raises ValueError for an item beyond the
    request's, or a verdict the schema doesn't have (a deployment may not enforce it), so the
    call fails."""
    beyond = sorted({e["item"] for e in reply["reviews"] if e["item"] not in ids})
    if beyond:
        raise ValueError(f"items beyond the request's {len(ids)}: {', '.join(beyond)}")
    unknown = sorted({str(e["verdict"]) for e in reply["reviews"] if e["verdict"] not in VERDICTS})
    if unknown:
        raise ValueError(f"verdicts not {' or '.join(VERDICTS)}: {', '.join(unknown)}")
    verdicts: dict[str, tuple[str, str]] = {}
    repeated = 0
    for entry in reply["reviews"]:
        local = entry["item"]
        if local in verdicts:
            repeated += 1
        else:
            verdicts[local] = (entry["verdict"], entry["reason"].strip())
    return Reply(verdicts, repeated)


# --- The pass -------------------------------------------------------------------------------


def resolve_label_run(work: Path) -> LabelRun:
    """The run to review: the one `label_run.json` names."""
    run = records.read_label_run(work)
    if run is None:
        raise ReviewError(f"no {work / records.LABEL_RUN}; run `kgb-augment label` first")
    return run


def _record_review(work: Path, run: LabelRun, reviewer: str, version: str, effort: str) -> None:
    """Name the review in `label_run.json`, which names the run reviewed."""
    update = {
        "reviewer": reviewer,
        "review_prompt_version": version,
        "review_reasoning_effort": effort,
    }
    records.write_label_run(work, run.model_copy(update=update))


def _client(model: str, max_output_tokens: int | None, reasoning_effort: str | None) -> Any:
    # A client raises LLMError itself when its model's credentials are missing.
    try:
        return llm.client_for(
            llm.model(model), max_output_tokens=max_output_tokens, reasoning_effort=reasoning_effort
        )
    except llm.LLMError as e:
        raise ReviewError(f"can't use {model}: {e}") from e


def _load(
    work: Path,
    cache_dir: Path,
    input_dir: Path,
    run: LabelRun,
    reviewer: str,
    version: str,
    limit: int | None,
) -> tuple[Bundles, dict[str, dict[str, list[str]]]]:
    """The bundled pairs with this review's verdicts so far, and step 2's synonyms per pair."""
    try:
        loaded = bundle.load(
            work,
            cache_dir,
            input_dir,
            run,
            reviewer=reviewer,
            review_prompt_version=version,
            limit=limit,
        )
    except (FileNotFoundError, ValueError) as e:
        raise ReviewError(str(e)) from e
    wanted = {b.pair_id for b in loaded.pairs}
    synonyms = {
        r.pair_id: r.synonyms
        for r in records.read_records(work / records.PUBMED, records.PubmedRecord)
        if r.pair_id in wanted
    }
    return loaded, synonyms


def to_review(loaded: Bundles) -> tuple[dict[str, list[Item]], dict[str, list[Item]]]:
    """Each pair's claims that would count if upheld (`refine.would_count`), in its claims'
    order: those with no verdict of this review yet, which the pass sends, and those with one,
    upheld or rejected, which it doesn't. Pairs with none are left out. Identical claims of an
    abstract share a key, and so one item."""
    unreviewed: dict[str, list[Item]] = {}
    reviewed: dict[str, list[Item]] = {}
    for b in loaded.pairs:
        items: dict[ClaimKey, Item] = {}
        for i in refine.would_count(b.edges, b.claims, loaded.lattice, loaded.categories):
            items.setdefault(b.keys[i], Item(b.keys[i], b.claims[i].record))
        for item in items.values():
            side = unreviewed if item.record.review is None else reviewed
            side.setdefault(b.pair_id, []).append(item)
    return unreviewed, reviewed


def _count(groups: Mapping[str, Sequence[Item]]) -> int:
    return sum(map(len, groups.values()))


def _calls(
    calls: Sequence[tuple[str, list[tuple[str, Item]]]],
    groups: Mapping[str, Sequence[Item]],
    entities: Mapping[str, Entity],
    synonyms: Mapping[str, Mapping[str, list[str]]],
    pubmed: PubMedCache,
) -> Iterator[llm.Call]:
    """A call per batch; a pair's abstracts are read only when its first call is made, and
    `batches` gives a pair's calls one after another."""
    read: dict[str, dict[str, Abstract]] = {}
    for pid, batch in calls:
        if pid not in read:
            read = {pid: pubmed.get_abstracts(i.pmid for i in groups[pid])}
        user = build_message(pid, batch, entities, synonyms.get(pid, {}), read[pid])
        yield llm.Call(pid, user, dict(batch))


def review_claims(
    work: Path,
    cache_dir: Path,
    input_dir: Path,
    *,
    client: Any = None,
    model: str = config.REVIEW_MODEL,
    prompt: Path = config.REVIEW_PROMPT,
    workers: int = config.WORKERS,
    reasoning_effort: str | None = None,
    max_output_tokens: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Review, with `model`, in one pass, the claims of the run `label_run.json` names that
    would count and have no verdict of this review yet (`to_review`); store the verdicts and,
    if no more than 5% of the calls the claims that would count need failed (those reviewed
    before included), name the review in `label_run.json`. `input_dir` holds the graph whose
    edges the claims would count for. `client` (anything with a client's `call`) is built from
    `model` if not given and there is something to send, with `max_output_tokens` and
    `reasoning_effort` overriding the model's own settings. `limit` reviews only the first
    pairs by id. Raises ReviewError for a missing input or an unusable model."""
    work, cache_dir, prompt = Path(work), Path(cache_dir), Path(prompt)
    run = resolve_label_run(work)
    try:
        system, version = review_prompt(prompt, model, reasoning_effort)
        effort = llm.run_effort(model, reasoning_effort)
    except llm.LLMError as e:
        raise ReviewError(f"can't use {model}: {e}") from e
    reviewer = model
    summary: dict[str, Any] = {
        "label_model": run.model,
        "label_prompt_version": run.prompt_version,
        "reviewer": reviewer,
        "review_prompt_version": version,
        "reasoning_effort": effort,
    }
    warning = repair.stale(run)
    if warning:
        print(f"review: warning: {warning}", file=sys.stderr)
    loaded, synonyms = _load(work, cache_dir, Path(input_dir), run, reviewer, version, limit)
    groups, before = to_review(loaded)
    calls = batches(groups)
    summary |= {
        "pairs": len(loaded.pairs),
        "pairs_not_fully_labeled": len(loaded.unlabeled),
        "pairs_not_looked_up": len(loaded.not_looked_up),
        "skipped": dict(loaded.counts),
        "claims_to_review": _count(groups),
        "already_reviewed": _count(before),
        "sent": sum(len(batch) for _, batch in calls),
        "verdicts": dict.fromkeys(VERDICTS, 0),
        "omitted": 0,
        "repeated": 0,
    }
    answered: set[ClaimKey] = set()  # the claims given a verdict in this run
    total = llm.Tally()
    with (
        LabelCache(cache_dir / config.LABEL_CACHE) as cache,
        PubMedCache(cache_dir / config.PUBMED_CACHE) as pubmed,
    ):

        def on_result(call: llm.Call, reply: Any, usage: llm.Usage) -> str | None:
            ids: dict[str, Item] = call.context
            try:
                parsed = parse_reply(reply, ids)
            except ValueError as e:
                return f"malformed reply: {e}"
            except Exception as e:
                return f"could not read the reply: {type(e).__name__}: {e}"
            rows = []
            for local, (verdict, reason) in parsed.verdicts.items():
                item = ids[local]
                answered.add(item.key)
                summary["verdicts"][verdict] += 1
                rows.append(
                    ClaimReviewRow(
                        item.pair_id,
                        item.pmid,
                        run.prompt_version,
                        run.model,
                        item.key[2],
                        reviewer,
                        version,
                        verdict,
                        reason,
                        answered_by=usage.model,
                    )
                )
            cache.put_claim_reviews(rows)
            summary["repeated"] += parsed.repeated
            summary["omitted"] += len(ids) - len(parsed.verdicts)
            return None

        if calls:
            if client is None:
                client = _client(model, max_output_tokens, reasoning_effort)
            total = llm.run_calls(
                _calls(calls, groups, loaded.entities, synonyms, pubmed),
                client,
                on_result,
                system=system,
                schema=REPLY_SCHEMA,
                cache=cache,
                step="review",
                model=reviewer,
                prompt_version=version,
                total=len(calls),
                workers=workers,
            )
    # The calls the claims reviewed before took, upheld or rejected; with this run's, they are
    # the calls the review needs, and the 5% rule takes the failed calls as a share of them.
    needed = total.calls + len(batches(before))
    summary |= {
        "unreviewed": sum(i.key not in answered for items in groups.values() for i in items),
        "calls": total.calls,
        "calls_needed": needed,
        "failed_calls": total.failed,
        "fallback_served": total.fallback_served,
        "input_tokens": total.input_tokens,
        "cache_read_tokens": total.cache_read_tokens,
        "cache_write_tokens": total.cache_write_tokens,
        "output_tokens": total.output_tokens,
        "cost": total.cost,
    }
    summary["ok"] = not llm.too_many_failed(needed, total.failed)
    if summary["ok"]:
        _record_review(work, run, reviewer, version, effort)
    return summary


# --- CLI ------------------------------------------------------------------------------------


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", default=config.REVIEW_MODEL, help="review model id")
    parser.add_argument(
        "--prompt", type=Path, default=config.REVIEW_PROMPT, help="review prompt file"
    )
    parser.add_argument("--workers", type=int, default=config.WORKERS, help="parallel calls")
    parser.add_argument(
        "--reasoning-effort",
        default=None,
        help="reasoning effort sent to the model, or 'none' to leave it out (default: the "
        "model's in config.MODELS); it is part of the review prompt version, so another effort "
        "reviews afresh",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=None,
        help="output token cap per call (default: the model's in config.MODELS)",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="review only the first N pairs by id"
    )


def _print_review(s: dict[str, Any]) -> None:
    print(
        f"review: run {s['label_model']} {s['label_prompt_version']}; reviewer {s['reviewer']} "
        f"(effort {s['reasoning_effort']}) {s['review_prompt_version']}"
    )
    left = ", ".join(f"{n} {why}" for why, n in s["skipped"].items() if n)
    print(
        f"review: {s['pairs']} pairs; {s['pairs_not_fully_labeled']} not fully labeled (left for "
        f"a label run to finish), {s['pairs_not_looked_up']} with anatomy names never looked up "
        "(left for a ground run)" + (f"; skipped: {left}" if left else "")
    )
    v = s["verdicts"]
    print(
        f"review: {s['claims_to_review']} claims that would count to review, "
        f"{s['already_reviewed']} reviewed before; {s['sent']} sent: {v['upheld']} upheld, "
        f"{v['reject']} rejected; {s['omitted']} left out of the replies (sent again next run), "
        f"{s['repeated']} repeated entries"
    )
    if s["unreviewed"]:
        print(
            f"review: {s['unreviewed']} claims that would count still have no verdict; step 6 "
            "holds their pairs until a rerun reviews them",
            file=sys.stderr,
        )
    cost = "unknown" if s["cost"] is None else f"${s['cost']:.4f}"
    n = s["fallback_served"]
    served = f", {n} answered by a fallback model" if n else ""
    print(
        f"review: {s['calls']} calls, {s['failed_calls']} failed{served}; {s['input_tokens']:,} "
        f"input ({s['cache_read_tokens']:,} read from the cache, {s['cache_write_tokens']:,} "
        f"written to it) and {s['output_tokens']:,} output tokens; cost {cost}"
    )
    if not s["ok"]:
        print(
            f"review: {s['failed_calls']} of the {s['calls_needed']} calls the claims that would "
            f"count need failed (more than {llm.MAX_FAILED_SHARE:.0%}; those reviewed before "
            "count too); label_run.json unchanged, rerun to retry",
            file=sys.stderr,
        )


def run(args: argparse.Namespace) -> int:
    try:
        s = review_claims(
            args.work,
            args.cache_dir,
            args.input,
            model=args.model,
            prompt=args.prompt,
            workers=args.workers,
            reasoning_effort=args.reasoning_effort,
            max_output_tokens=args.max_output_tokens,
            limit=args.limit,
        )
    except ReviewError as e:
        print(f"review: {e}", file=sys.stderr)
        return 1
    _print_review(s)
    return 0 if s["ok"] else 1
