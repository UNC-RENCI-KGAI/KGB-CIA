"""Step 5b: a stronger model reviews the labels that would add edges.

Step 6 adds a new edge on the strength of one abstract: a negated edge for a `contradicted`
label, a qualified edge for a qualifier proposal. They are a small share of the labels, so a
stronger, dearer model than the bulk one reads each of them again and accepts or rejects it.
Confirmed labels only add a PMID to an existing edge and aren't reviewed; nor are labels whose
quote isn't in the abstract or proposals that fail the Biolink check, which step 6 drops anyway.

Like step 5 it makes one call per pair, sending each cited abstract once for all of the pair's
items, and only the abstracts the items cite. Verdicts go to the label cache under (reviewer,
review prompt version), so a rerun reviews only what is new and a prompt edit starts afresh;
`label_run.json` then names the review that step 6 applies.

The agent can also run this pass as a Pi subagent: `--export` writes the pending items, one
self-contained JSON line each, with the instructions next to them, and `--import` stores the
subagent's verdict lines. Item ids hash (kind, statement, PMID, qualifiers), so an item keeps
its id across exports and imports.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pubmed_evidence import config, llm, records
from pubmed_evidence.cache import (
    Abstract,
    CallRow,
    LabelCache,
    PubMedCache,
    ReviewRow,
    canonical,
)
from pubmed_evidence.graphio import pair_nodes, write_jsonl
from pubmed_evidence.records import Entity, LabelRun, PubmedRecord, SentenceRecord, Statement

# Version of the code that writes the user message and the export lines. It is part of the
# review prompt version, so a change to either format starts a fresh set of reviews.
FORMAT_VERSION = "review-format-1"
KINDS: tuple[str, ...] = ("contradicted", "qualifier")
VERDICTS: tuple[str, ...] = ("accept", "reject")
MAX_FAILED_SHARE = 0.05
MAX_OUTPUT_TOKENS = 16000
REASONING_EFFORT = "medium"

# The section of the review prompt that describes the message and the reply. `--export`
# replaces it with EXPORT_FORMAT in the instructions it writes for a Pi subagent.
REPLY_SECTION = "## Input and reply format"
EXPORT_FORMAT = """## Input and reply format

The items are in a JSONL file, one self-contained JSON object per line:

- `item_id`: the item's id. Copy it exactly into your verdict.
- `kind`: `contradicted` or `qualifier`.
- `pmid`, `title`, `abstract`: the abstract the item cites. Judge the item on it alone.
- `entities`: the pair's two entities by id, each with its name, category and other names.
- `sentence` and `structured`: the edge as a sentence and as structured Biolink JSON (subject,
  predicate, object and any qualifiers it already has).
- `quote` and `claim`: the first pass's quote from the abstract, and whether it read the finding
  as `causal` or `associative` (may be null).
- Qualifier items only: `qualifiers` (the proposed set), `resulting_qualifiers` (the edge's own
  qualifiers with the proposed ones added or replacing them) and `resulting_statement` (the
  same in words).

Other keys are bookkeeping; ignore them.

Write a verdict file with one JSON object per line, one line per item, and nothing else. The
reason says in one or two sentences what in the abstract decided the verdict:

```json
{"item_id": "<the item's item_id>", "verdict": "accept", "reason": "<one or two sentences>"}
```

`verdict` is `accept` or `reject`. Review every item: an item without a verdict line is
exported again next time.
"""


class ReviewError(RuntimeError):
    """A missing input or setting that stops the review before any call."""


@dataclass
class Item:
    """A label or proposal to review: a contradicted label (`qualifiers` None) or a qualifier
    proposal of the first-pass run."""

    kind: str
    statement_id: str
    pmid: str
    qualifiers: dict[str, Any] | None = None
    quote: str | None = None
    claim: str | None = None

    @property
    def key(self) -> tuple[str, str, str, str]:
        return _key(self.kind, self.statement_id, self.pmid, self.qualifiers)

    @property
    def item_id(self) -> str:
        return item_id(self.kind, self.statement_id, self.pmid, self.qualifiers)


def _key(
    kind: str, statement_id: str, pmid: str, qualifiers: dict[str, Any] | None
) -> tuple[str, str, str, str]:
    # The reviews table stores '' for a contradicted label's qualifiers.
    return (kind, statement_id, pmid, canonical(qualifiers) if qualifiers is not None else "")


def item_id(
    kind: str, statement_id: str, pmid: str, qualifiers: dict[str, Any] | None = None
) -> str:
    """Stable id of an item: its kind's initial and 12 hex digits of the SHA-256 of
    (kind, statement_id, pmid, canonical qualifiers), e.g. 'c-1f3a09d2b7c4'."""
    text = "\0".join(_key(kind, statement_id, pmid, qualifiers))
    return f"{kind[0]}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}"


# --- Inputs ---------------------------------------------------------------------------------


@dataclass
class Work:
    """What the review needs from the work directory."""

    statements: dict[str, Statement]
    sentences: dict[str, str]
    entities: dict[str, Entity]
    synonyms: dict[str, dict[str, list[str]]]  # pair_id -> node id -> step 3's [tiab] terms


def load_work(work: Path) -> Work:
    path = work / records.STATEMENTS
    if not path.exists():
        raise ReviewError(f"no {path}; run `kgb-augment select` first")
    statements = {s.statement_id: s for s in records.read_records(path, Statement)}
    sentences: dict[str, str] = {}
    if (work / records.SENTENCES).exists():
        for r in records.read_records(work / records.SENTENCES, SentenceRecord):
            sentences[r.statement_id] = r.sentence
    else:
        missing = work / records.SENTENCES
        print(f"review: no {missing}; describing edges from their slots", file=sys.stderr)
    entities: dict[str, Entity] = {}
    if (work / records.ENTITIES).exists():
        entities = {e.id: e for e in records.read_records(work / records.ENTITIES, Entity)}
    synonyms: dict[str, dict[str, list[str]]] = {}
    if (work / records.PUBMED).exists():
        for p in records.read_records(work / records.PUBMED, PubmedRecord):
            synonyms[p.pair_id] = p.synonyms
    return Work(statements, sentences, entities, synonyms)


def resolve_label_run(
    work: Path, label_model: str | None = None, label_prompt_version: str | None = None
) -> tuple[str, str, LabelRun | None]:
    """(model, prompt version) of the first-pass run to review, and `label_run.json`."""
    run = records.read_label_run(work)
    model = label_model or (run.model if run else None)
    version = label_prompt_version or (run.prompt_version if run else None)
    if not model or not version:
        raise ReviewError(f"no {work / records.LABEL_RUN}; run `kgb-augment label` first")
    return model, version, run


def reviewable_items(
    cache: LabelCache, model: str, prompt_version: str, statement_ids: list[str]
) -> list[Item]:
    """The run's labels that would add an edge: contradicted labels with a verified quote, and
    qualifier proposals with a verified quote that pass the Biolink check."""
    items = [
        Item("contradicted", r.statement_id, r.pmid, None, r.quote, r.claim)
        for r in cache.labels(prompt_version, model, statement_ids, label="contradicted")
        if r.quote_valid is True
    ]
    items += [
        Item("qualifier", p.statement_id, p.pmid, p.qualifiers, p.quote, p.claim)
        for p in cache.proposals(prompt_version, model, statement_ids)
        if p.quote_valid is True and p.valid is True
    ]
    return sorted(items, key=lambda i: (i.statement_id, KINDS.index(i.kind), i.pmid, i.key[3]))


def reviewed_keys(
    cache: LabelCache,
    model: str,
    prompt_version: str,
    reviewer: str | None,
    review_prompt_version: str,
) -> set[tuple[str, str, str, str]]:
    rows = cache.reviews(prompt_version, model, reviewer, review_prompt_version)
    return {_key(r.kind, r.statement_id, r.pmid, r.qualifiers) for r in rows}


def _with_abstracts(
    items: list[Item], pubmed: PubMedCache
) -> tuple[list[Item], dict[str, Abstract], int]:
    """The items whose abstract is cached, the abstracts, and how many items had none."""
    abstracts = pubmed.get_abstracts(i.pmid for i in items)
    kept = [i for i in items if i.pmid in abstracts]
    missing = len(items) - len(kept)
    if missing:
        print(
            f"review: skipped {missing} items citing a PMID with no cached abstract",
            file=sys.stderr,
        )
    return kept, abstracts, missing


def by_pair(items: list[Item], work: Work, limit: int | None = None) -> dict[str, list[Item]]:
    """Items grouped by pair, pairs sorted; only the first `limit` pairs if given."""
    groups: dict[str, list[Item]] = defaultdict(list)
    for item in items:
        groups[work.statements[item.statement_id].pair_id].append(item)
    pairs = sorted(groups)[:limit] if limit is not None else sorted(groups)
    return {p: groups[p] for p in pairs}


# --- Describing an item ---------------------------------------------------------------------


def _words(value: Any) -> str:
    if isinstance(value, list | tuple):
        return ", ".join(_words(v) for v in value)
    return str(value).removeprefix("biolink:").replace("_", " ")


def _role_phrase(name: str, qualifiers: dict[str, Any], role: str) -> str:
    phrase = name
    for slot in ("form_or_variant", "part", "derivative", "aspect"):
        value = qualifiers.pop(f"{role}_{slot}_qualifier", None)
        if value:
            phrase = f"{_words(value)} of {phrase}"
    direction = qualifiers.pop(f"{role}_direction_qualifier", None)
    return f"{_words(direction)} {phrase}" if direction else phrase


def statement_words(
    subject: str, predicate: str, object: str, qualifiers: dict[str, Any] | None = None
) -> str:
    """A plain rendering of a statement with its qualifiers, e.g. 'imatinib causes decreased
    activity of ABL1'. Qualifiers with no place in the sentence follow in parentheses."""
    rest = {k: v for k, v in (qualifiers or {}).items() if v}
    predicate = rest.pop("qualified_predicate", None) or predicate
    text = f"{_role_phrase(subject, rest, 'subject')} {_words(predicate)} "
    text += _role_phrase(object, rest, "object")
    if rest:
        extra = [f"{_words(k.removesuffix('_qualifier'))}: {_words(v)}" for k, v in rest.items()]
        text += f" ({'; '.join(sorted(extra))})"
    return text


def _name(node: str, work: Work) -> str:
    entity = work.entities.get(node)
    return entity.name if entity and entity.name else node


def _entity(node: str, pair_id: str, work: Work) -> dict[str, Any]:
    entity = work.entities.get(node)
    name = _name(node, work)
    terms = work.synonyms.get(pair_id, {}).get(node) or (entity.synonyms if entity else [])
    seen = {name.lower()}
    synonyms = []
    for term in terms:
        if term.lower() not in seen:
            seen.add(term.lower())
            synonyms.append(term)
    category = entity.category[0].removeprefix("biolink:") if entity and entity.category else None
    return {"name": name, "category": category, "synonyms": synonyms[: config.MAX_SYNONYMS]}


def item_fields(item: Item, work: Work) -> dict[str, Any]:
    """The item's statement, in words and structured, and for a qualifier item the
    resulting qualifiers and statement."""
    s = work.statements[item.statement_id]
    subject, object = _name(s.subject, work), _name(s.object, work)
    sentence = work.sentences.get(s.statement_id) or statement_words(
        subject, s.predicate, object, s.qualifiers
    )
    fields: dict[str, Any] = {
        "sentence": sentence,
        "structured": {
            "subject": s.subject,
            "predicate": s.predicate,
            "object": s.object,
            **dict(sorted(s.qualifiers.items())),
        },
        "qualifiers": None,
        "resulting_qualifiers": None,
        "resulting_statement": None,
        "quote": item.quote,
        "claim": item.claim,
    }
    if item.kind == "qualifier":
        merged = dict(sorted({**s.qualifiers, **(item.qualifiers or {})}.items()))
        fields["qualifiers"] = item.qualifiers
        fields["resulting_qualifiers"] = merged
        fields["resulting_statement"] = statement_words(subject, s.predicate, object, merged)
    return fields


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


def build_message(
    pair_id: str, batch: list[tuple[str, Item]], work: Work, abstracts: dict[str, Abstract]
) -> str:
    """The user message for one call: the pair's entities, the items under their local ids,
    then each abstract the items cite, once."""
    lines = ["## Entities", ""]
    for node in pair_nodes(pair_id):
        e = _entity(node, pair_id, work)
        line = f"- {node}: {e['name']}"
        if e["category"]:
            line += f" ({e['category']})"
        if e["synonyms"]:
            line += f"; other names: {'; '.join(e['synonyms'])}"
        lines.append(line)
    lines += ["", "## Items", ""]
    for local, item in batch:
        f = item_fields(item, work)
        lines += [
            f"### {local}: {item.kind}, PMID {item.pmid}",
            f"Edge: {f['sentence']}",
            f"Structured: {_json(f['structured'])}",
        ]
        if item.kind == "qualifier":
            lines += [
                f"Proposed qualifiers: {_json(f['qualifiers'])}",
                f"Resulting qualifiers: {_json(f['resulting_qualifiers'])}",
                f"Resulting statement: {f['resulting_statement']}",
            ]
        if item.claim:
            lines.append(f"First-pass claim: {item.claim}")
        lines += [f"First-pass quote: {_json(item.quote or '')}", ""]
    lines += ["## Abstracts", ""]
    for pmid in dict.fromkeys(item.pmid for _, item in batch):
        a = abstracts[pmid]
        lines += [f"### PMID {pmid}", f"Title: {a.title}", "", a.abstract.strip(), ""]
    return "\n".join(lines).rstrip() + "\n"


def batches(groups: dict[str, list[Item]]) -> list[tuple[str, list[tuple[str, Item]]]]:
    """One call per pair, with local ids R1, R2, ...; a pair whose items cite more than
    config.ABSTRACTS_PER_CALL abstracts is split, as step 5 splits it."""
    out = []
    for pair, items in groups.items():
        pmids = list(dict.fromkeys(i.pmid for i in items))
        for start in range(0, len(pmids), config.ABSTRACTS_PER_CALL):
            chunk = set(pmids[start : start + config.ABSTRACTS_PER_CALL])
            batch = [i for i in items if i.pmid in chunk]
            out.append((pair, [(f"R{n}", item) for n, item in enumerate(batch, 1)]))
    return out


def parse_reply(reply: Any, ids: dict[str, Item]) -> tuple[dict[str, tuple[str, str]], int]:
    """Verdicts by local id, and the number of entries with an unknown id or verdict. Raises
    ValueError if the reply has no `reviews` list."""
    entries = reply.get("reviews") if isinstance(reply, dict) else None
    if not isinstance(entries, list):
        raise ValueError("no 'reviews' list in the reply")
    verdicts: dict[str, tuple[str, str]] = {}
    invalid = 0
    for entry in entries:
        if not isinstance(entry, dict):
            invalid += 1
            continue
        local = str(entry.get("item", "")).strip()
        verdict = str(entry.get("verdict", "")).strip().lower()
        if local not in ids or verdict not in VERDICTS or local in verdicts:
            invalid += 1
            continue
        verdicts[local] = (verdict, str(entry.get("reason") or "").strip())
    return verdicts, invalid


# --- Direct review --------------------------------------------------------------------------


def _review_row(
    item: Item,
    verdict: str,
    reason: str,
    model: str,
    prompt_version: str,
    reviewer: str,
    review_prompt_version: str,
) -> ReviewRow:
    return ReviewRow(
        kind=item.kind,
        statement_id=item.statement_id,
        pmid=item.pmid,
        prompt_version=prompt_version,
        model=model,
        qualifiers=item.qualifiers if item.kind == "qualifier" else None,
        reviewer=reviewer,
        review_prompt_version=review_prompt_version,
        verdict=verdict,
        reason=reason,
    )


def _counts() -> dict[str, int]:
    return dict.fromkeys(KINDS, 0)


def _record_review(
    work: Path, run: LabelRun | None, model: str, prompt_version: str, reviewer: str, version: str
) -> bool:
    """Name the review in `label_run.json`, if that file names the run reviewed."""
    if run is None or (run.model, run.prompt_version) != (model, prompt_version):
        print(
            f"review: {work / records.LABEL_RUN} names another run; left unchanged", file=sys.stderr
        )
        return False
    update = {"reviewer": reviewer, "review_prompt_version": version}
    records.write_label_run(work, run.model_copy(update=update))
    return True


def _call(client: Any, system: str, user: str, max_output_tokens: int, effort: str | None):
    start = time.monotonic()
    try:
        reply, usage = client.complete_json(
            system, user, max_output_tokens=max_output_tokens, reasoning_effort=effort
        )
    except Exception as e:
        # Any failure, not only LLMError, counts against the failure budget: an exception
        # escaping here would discard the replies of the calls still in flight.
        message = str(e) if isinstance(e, llm.LLMError) else f"{type(e).__name__}: {e}"
        return None, None, message or type(e).__name__, time.monotonic() - start
    return reply, usage, None, time.monotonic() - start


def _client(model: str) -> Any:
    # `llm.Client` raises LLMError itself when the API key is missing.
    try:
        return llm.Client(llm.load_model(model))
    except llm.LLMError as e:
        raise ReviewError(f"can't use {model}: {e}") from e


def _store(
    cache: LabelCache,
    summary: dict[str, Any],
    pair: str,
    ids: dict[str, Item],
    result: tuple[Any, llm.Usage | None, str | None, float],
    run_keys: tuple[str, str, str, str],
) -> None:
    """Log one call and store its verdicts, counting them in `summary`. Main thread only:
    SQLite connections stay in the thread that made them."""
    label_model, label_version, reviewer, version = run_keys
    reply, usage, error, seconds = result
    verdicts: dict[str, tuple[str, str]] = {}
    if error is None:
        try:
            verdicts, invalid = parse_reply(reply, ids)
            summary["invalid"] += invalid
        except ValueError as e:
            error = f"malformed reply: {e}"
    usage = usage or llm.Usage()
    cache.log_call(
        CallRow(
            step="review",
            pair_id=pair,
            model=reviewer,
            prompt_version=version,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost=usage.cost,
            seconds=usage.seconds or seconds,
            error=error,
        )
    )
    summary["calls"] += 1
    summary["input_tokens"] += usage.input_tokens
    summary["output_tokens"] += usage.output_tokens
    if usage.cost is not None:
        summary["cost"] = (summary["cost"] or 0.0) + usage.cost
    if error is not None:
        summary["failed_calls"] += 1
        print(f"review: {pair}: {error}", file=sys.stderr)
        return
    rows = []
    for local, (verdict, reason) in verdicts.items():
        item = ids[local]
        summary["accepted" if verdict == "accept" else "rejected"][item.kind] += 1
        rows.append(
            _review_row(item, verdict, reason, label_model, label_version, reviewer, version)
        )
    cache.put_reviews(rows)
    summary["omitted"] += len(ids) - len(verdicts)


def review_items(
    work: Path,
    cache_dir: Path,
    *,
    client: Any = None,
    model: str = config.REVIEW_MODEL,
    prompt: Path = config.REVIEW_PROMPT,
    workers: int = config.WORKERS,
    reasoning_effort: str | None = REASONING_EFFORT,
    max_output_tokens: int = MAX_OUTPUT_TOKENS,
    limit: int | None = None,
    label_model: str | None = None,
    label_prompt_version: str | None = None,
) -> dict[str, Any]:
    """Review the pending items of the first-pass run with `model`, store the verdicts and
    name the review in `label_run.json`. `client` (anything with `complete_json`) is built
    from `model` if not given. The summary's `ok` is False if more than 5% of calls failed."""
    label_model, label_version, run = resolve_label_run(work, label_model, label_prompt_version)
    w = load_work(work)
    system, version = llm.load_prompt(prompt, extra=FORMAT_VERSION)
    reviewer = model
    summary: dict[str, Any] = {
        "label_model": label_model,
        "label_prompt_version": label_version,
        "reviewer": reviewer,
        "review_prompt_version": version,
        "items": _counts(),
        "already_reviewed": 0,
        "no_abstract": 0,
        "pending": 0,
        "pending_pairs": 0,
        "sent": 0,
        "accepted": _counts(),
        "rejected": _counts(),
        "omitted": 0,
        "invalid": 0,
        "calls": 0,
        "failed_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost": None,
        "label_run_updated": False,
        "ok": True,
    }
    with LabelCache(cache_dir / config.LABEL_CACHE) as cache:
        items = reviewable_items(cache, label_model, label_version, list(w.statements))
        for item in items:
            summary["items"][item.kind] += 1
        done = reviewed_keys(cache, label_model, label_version, reviewer, version)
        pending = [i for i in items if i.key not in done]
        summary["already_reviewed"] = len(items) - len(pending)
        with PubMedCache(cache_dir / config.PUBMED_CACHE) as pubmed:
            pending, abstracts, summary["no_abstract"] = _with_abstracts(pending, pubmed)
        summary["pending"] = len(pending)
        groups = by_pair(pending, w, limit)
        summary["pending_pairs"] = len(groups)
        requests = [
            (pair, dict(batch), build_message(pair, batch, w, abstracts))
            for pair, batch in batches(groups)
        ]
        summary["sent"] = sum(len(ids) for _, ids, _ in requests)
        if requests and client is None:
            client = _client(model)
        run_keys = (label_model, label_version, reviewer, version)
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {}
            for pair, ids, user in requests:
                args = (client, system, user, max_output_tokens, reasoning_effort)
                futures[pool.submit(_call, *args)] = (pair, ids)
            for n, future in enumerate(as_completed(futures), 1):
                pair, ids = futures[future]
                _store(cache, summary, pair, ids, future.result(), run_keys)
                if len(requests) >= 20 and n % max(1, len(requests) // 10) == 0:
                    print(f"review: {n}/{len(requests)} calls", file=sys.stderr)
    calls = summary["calls"]
    summary["ok"] = not calls or summary["failed_calls"] / calls <= MAX_FAILED_SHARE
    if summary["ok"]:
        summary["label_run_updated"] = _record_review(
            work, run, label_model, label_version, reviewer, version
        )
    return summary


# --- Pi subagent: export and import ---------------------------------------------------------


def instructions(prompt_text: str) -> str:
    """The review prompt with its reply section replaced by the export's line formats."""
    section = re.search(rf"^{re.escape(REPLY_SECTION)}[ \t]*$", prompt_text, re.M)
    if section is None:
        return prompt_text.rstrip() + "\n\n" + EXPORT_FORMAT
    after = re.search(r"^## ", prompt_text[section.end() :], re.M)
    rest = prompt_text[section.end() + after.start() :] if after else ""
    return prompt_text[: section.start()] + EXPORT_FORMAT + ("\n" + rest if rest else "")


def export_version(prompt: Path = config.REVIEW_PROMPT) -> tuple[str, str]:
    """(instructions text, review prompt version) of the export. The version hashes the
    instructions the prompt file produces, plus FORMAT_VERSION, under the name
    '<prompt stem>-pi' (e.g. 'review-pi@1a2b3c4d'), so an import recomputes it as long as the
    prompt is unchanged since the export."""
    text = instructions(Path(prompt).read_text(encoding="utf-8"))
    return text, llm.prompt_version(text, FORMAT_VERSION, f"{Path(prompt).stem}-pi")


def instructions_path(path: Path) -> Path:
    return Path(path).with_suffix(".instructions.md")


def export_items(
    work: Path,
    cache_dir: Path,
    path: Path,
    *,
    reviewer: str,
    prompt: Path = config.REVIEW_PROMPT,
    limit: int | None = None,
    label_model: str | None = None,
    label_prompt_version: str | None = None,
) -> dict[str, Any]:
    """Write the pending items to `path` as JSONL and the instructions next to it; no calls.

    Pending means without a review by `reviewer` at the export's version: the reviewer the
    import will record, and the one step 6 then honours (`label_run.json` names one)."""
    label_model, label_version, _ = resolve_label_run(work, label_model, label_prompt_version)
    w = load_work(work)
    text, version = export_version(prompt)
    with LabelCache(cache_dir / config.LABEL_CACHE) as cache:
        items = reviewable_items(cache, label_model, label_version, list(w.statements))
        done = reviewed_keys(cache, label_model, label_version, reviewer, version)
    pending = [i for i in items if i.key not in done]
    with PubMedCache(cache_dir / config.PUBMED_CACHE) as pubmed:
        pending, abstracts, no_abstract = _with_abstracts(pending, pubmed)
    groups = by_pair(pending, w, limit)
    lines = []
    for pair, pair_items in groups.items():
        entities = {node: _entity(node, pair, w) for node in pair_nodes(pair)}
        for item in pair_items:
            a = abstracts[item.pmid]
            lines.append(
                {
                    "item_id": item.item_id,
                    "kind": item.kind,
                    "statement_id": item.statement_id,
                    "pair_id": pair,
                    "pmid": item.pmid,
                    "entities": entities,
                    **item_fields(item, w),
                    "title": a.title,
                    "abstract": a.abstract,
                    "label_model": label_model,
                    "label_prompt_version": label_version,
                    "review_prompt_version": version,
                }
            )
    write_jsonl(Path(path), lines)
    instructions_path(path).write_text(text, encoding="utf-8")
    return {
        "label_model": label_model,
        "label_prompt_version": label_version,
        "review_prompt_version": version,
        "items": len(items),
        "already_reviewed": len(items) - len(pending) - no_abstract,
        "no_abstract": no_abstract,
        "exported": len(lines),
        "pairs": len(groups),
        "path": Path(path),
        "instructions": instructions_path(path),
    }


def import_verdicts(
    work: Path,
    cache_dir: Path,
    path: Path,
    *,
    reviewer: str,
    review_prompt_version: str | None = None,
    prompt: Path = config.REVIEW_PROMPT,
    label_model: str | None = None,
    label_prompt_version: str | None = None,
) -> dict[str, Any]:
    """Store a Pi subagent's verdict lines, `{"item_id", "verdict", "reason"}`, as reviews
    by `reviewer`, and name the review in `label_run.json`. Lines with an id that isn't an
    item of the run are counted as `unknown`, lines that don't parse or have another verdict
    as `invalid`; neither is stored."""
    path = Path(path)
    if not path.exists():
        raise ReviewError(f"no verdict file {path}")
    label_model, label_version, run = resolve_label_run(work, label_model, label_prompt_version)
    w = load_work(work)
    version = review_prompt_version or export_version(prompt)[1]
    with LabelCache(cache_dir / config.LABEL_CACHE) as cache:
        items = {
            i.item_id: i
            for i in reviewable_items(cache, label_model, label_version, list(w.statements))
        }
        rows: dict[str, ReviewRow] = {}
        lines = unknown = invalid = 0
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            lines += 1
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                invalid += 1
                continue
            if not isinstance(entry, dict):
                invalid += 1
                continue
            iid = str(entry.get("item_id") or entry.get("item") or "").strip()
            verdict = str(entry.get("verdict", "")).strip().lower()
            if iid not in items:
                unknown += 1
            elif verdict not in VERDICTS:
                invalid += 1
            else:
                reason = str(entry.get("reason") or "").strip()
                rows[iid] = _review_row(
                    items[iid], verdict, reason, label_model, label_version, reviewer, version
                )
        cache.put_reviews(rows.values())
    accepted, rejected = _counts(), _counts()
    for row in rows.values():
        (accepted if row.verdict == "accept" else rejected)[row.kind] += 1
    updated = bool(rows) and _record_review(
        work, run, label_model, label_version, reviewer, version
    )
    return {
        "label_model": label_model,
        "label_prompt_version": label_version,
        "reviewer": reviewer,
        "review_prompt_version": version,
        "lines": lines,
        "imported": len(rows),
        "accepted": accepted,
        "rejected": rejected,
        "unknown": unknown,
        "invalid": invalid,
        "label_run_updated": updated,
    }


# --- CLI ------------------------------------------------------------------------------------


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", default=config.REVIEW_MODEL, help="review model id")
    parser.add_argument(
        "--prompt", type=Path, default=config.REVIEW_PROMPT, help="review prompt file"
    )
    parser.add_argument("--workers", type=int, default=config.WORKERS, help="parallel calls")
    parser.add_argument(
        "--reasoning-effort",
        default=REASONING_EFFORT,
        help="reasoning effort sent to the model; 'none' to leave it out",
    )
    parser.add_argument("--max-output-tokens", type=int, default=MAX_OUTPUT_TOKENS)
    parser.add_argument("--limit", type=int, default=None, help="review at most N pairs")
    parser.add_argument(
        "--label-model", default=None, help="first-pass model (default: label_run.json)"
    )
    parser.add_argument(
        "--label-prompt-version",
        default=None,
        help="first-pass prompt version (default: label_run.json)",
    )
    io = parser.add_mutually_exclusive_group()
    io.add_argument(
        "--export",
        dest="export_file",
        type=Path,
        default=None,
        metavar="FILE",
        help="write the pending items as JSONL for a Pi subagent (and FILE's .instructions.md); "
        "needs --reviewer; no calls",
    )
    io.add_argument(
        "--import",
        dest="import_file",
        type=Path,
        default=None,
        metavar="FILE",
        help="store a Pi subagent's verdict lines; needs --reviewer",
    )
    parser.add_argument(
        "--reviewer",
        default=None,
        help="who reviews, for --export and --import (e.g. 'pi:gpt-6.1-sol'): export "
        "exports what this reviewer hasn't reviewed, import records the verdicts as theirs",
    )
    parser.add_argument(
        "--review-prompt-version",
        default=None,
        help="for --import: the version printed by --export (default: recomputed from --prompt)",
    )


def _kinds(counts: dict[str, int]) -> str:
    return ", ".join(f"{counts[k]} {k}" for k in KINDS)


def _print_review(s: dict[str, Any]) -> None:
    print(
        f"review: run {s['label_model']} {s['label_prompt_version']}; "
        f"reviewer {s['reviewer']} {s['review_prompt_version']}"
    )
    print(
        f"review: {sum(s['items'].values())} items to review ({_kinds(s['items'])}); "
        f"{s['already_reviewed']} already reviewed, {s['no_abstract']} without an abstract, "
        f"{s['pending']} pending in {s['pending_pairs']} pairs"
    )
    reviewed = sum(s["accepted"].values()) + sum(s["rejected"].values())
    detail = "; ".join(
        f"{k} {s['accepted'][k]} accepted, {s['rejected'][k]} rejected" for k in KINDS
    )
    print(
        f"review: {reviewed} reviewed ({detail}); {s['omitted']} left out of the replies "
        f"(retried next run), {s['invalid']} invalid entries"
    )
    cost = "unknown" if s["cost"] is None else f"${s['cost']:.4f}"
    print(
        f"review: {s['calls']} calls, {s['failed_calls']} failed; {s['input_tokens']:,} input "
        f"and {s['output_tokens']:,} output tokens; cost {cost}"
    )
    if not s["ok"]:
        print(
            f"review: more than {MAX_FAILED_SHARE:.0%} of calls failed; label_run.json "
            "unchanged, rerun to retry",
            file=sys.stderr,
        )


def run(args: argparse.Namespace) -> int:
    effort = args.reasoning_effort
    effort = None if effort is None or effort.lower() in ("", "none") else effort
    try:
        if args.export_file is not None:
            if not args.reviewer:
                raise ReviewError(
                    "--export needs --reviewer NAME (who will review, e.g. 'pi:MODEL'), the "
                    "same one --import is given"
                )
            s = export_items(
                args.work,
                args.cache_dir,
                args.export_file,
                prompt=args.prompt,
                reviewer=args.reviewer,
                limit=args.limit,
                label_model=args.label_model,
                label_prompt_version=args.label_prompt_version,
            )
            print(
                f"review: exported {s['exported']} items in {s['pairs']} pairs to {s['path']} "
                f"({s['already_reviewed']} already reviewed, {s['no_abstract']} without an "
                f"abstract); instructions in {s['instructions']}"
            )
            print(f"review: review prompt version {s['review_prompt_version']}")
            return 0
        if args.import_file is not None:
            if not args.reviewer:
                raise ReviewError("--import needs --reviewer NAME (who reviewed, e.g. 'pi:MODEL')")
            s = import_verdicts(
                args.work,
                args.cache_dir,
                args.import_file,
                reviewer=args.reviewer,
                review_prompt_version=args.review_prompt_version,
                prompt=args.prompt,
                label_model=args.label_model,
                label_prompt_version=args.label_prompt_version,
            )
            print(
                f"review: imported {s['imported']} of {s['lines']} verdict lines as "
                f"{s['reviewer']} {s['review_prompt_version']}: accepted {_kinds(s['accepted'])}; "
                f"rejected {_kinds(s['rejected'])}; {s['unknown']} unknown item ids, "
                f"{s['invalid']} invalid lines"
            )
            return 0 if s["imported"] or not s["lines"] else 1
        s = review_items(
            args.work,
            args.cache_dir,
            model=args.model,
            prompt=args.prompt,
            workers=args.workers,
            reasoning_effort=effort,
            max_output_tokens=args.max_output_tokens,
            limit=args.limit,
            label_model=args.label_model,
            label_prompt_version=args.label_prompt_version,
        )
    except ReviewError as e:
        print(f"review: {e}", file=sys.stderr)
        return 1
    _print_review(s)
    return 0 if s["ok"] else 1
