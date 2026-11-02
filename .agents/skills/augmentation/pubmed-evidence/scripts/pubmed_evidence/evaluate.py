"""Choosing the models: scoring cached labels against a hand-labeled gold set.

A gold set of 50-100 (statement, abstract) pairs labeled by hand decides which models are good
enough for step 5. `kgb-augment label --gold FILE --model X` labels exactly those pairs into
the label cache; this step only reads the cache, so comparing models and prompt versions costs
nothing once each has labeled the gold set.

Each run (model, prompt version) is scored twice: on the labels as the model gave them, and
quote-checked, with confirmed or contradicted labels whose quote isn't in the abstract counted
as no_evidence, which is what step 6 effectively does with them. Cost comes from the calls the
label cache logged, so it is what the run really cost per pair rather than an estimate.

The gold file is JSONL, one line per (statement, abstract):
`{"statement_id": "...", "pmid": "12345", "gold_label": "confirmed"}`; other keys are ignored.
`label --sample` writes `label_sample.jsonl` in this shape with `gold_label: null`, so a filled
in sample is a gold set: lines still null are skipped.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pubmed_evidence import config, records
from pubmed_evidence.cache import LABELS, LabelCache, LabelRow

GOLD = "gold.jsonl"
REPORT = "evaluate_report.md"
DECISIVE = ("confirmed", "contradicted")
PAIRS_SCALE = 10_000


class EvaluateError(RuntimeError):
    """A missing or empty input that stops the evaluation."""


@dataclass
class GoldItem:
    statement_id: str
    pmid: str
    gold_label: str


@dataclass
class Gold:
    path: Path | None
    items: list[GoldItem] = field(default_factory=list)
    unlabeled: int = 0  # lines whose gold_label is null, as in an unfilled label_sample.jsonl
    invalid: int = 0  # lines without a statement_id and pmid, or with an unknown label
    duplicates: int = 0  # repeated (statement_id, pmid); the last line wins


def _label(value: Any) -> str | None:
    text = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    return text if text in LABELS else None


def _pmid(value: Any) -> str:
    text = str(value).strip()
    return text.split(":", 1)[1].strip() if text.upper().startswith("PMID:") else text


def read_gold(path: Path) -> Gold:
    """The gold items of a gold file or a filled-in `label_sample.jsonl`."""
    path = Path(path)
    if not path.exists():
        raise EvaluateError(f"no gold file {path}")
    gold = Gold(path)
    by_key: dict[tuple[str, str], GoldItem] = {}
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            line = json.loads(raw)
        except json.JSONDecodeError:
            line = None
        if not isinstance(line, dict):
            print(f"evaluate: {path}:{n}: not a JSON object; skipped", file=sys.stderr)
            gold.invalid += 1
            continue
        if line.get("gold_label") is None:
            gold.unlabeled += 1
            continue
        label = _label(line["gold_label"])
        sid, pmid = line.get("statement_id"), line.get("pmid")
        if label is None or not sid or not pmid:
            print(
                f"evaluate: {path}:{n}: needs statement_id, pmid and a gold_label in "
                f"{', '.join(LABELS)}; skipped",
                file=sys.stderr,
            )
            gold.invalid += 1
            continue
        key = (str(sid), _pmid(pmid))
        if key in by_key:
            gold.duplicates += 1
        by_key[key] = GoldItem(key[0], key[1], label)
    gold.items = list(by_key.values())
    return gold


def parse_run(text: str) -> tuple[str, str | None]:
    """'MODEL' or 'MODEL@PROMPT_VERSION'. Prompt versions contain '@' themselves
    ('label@1a2b3c4d'), so the model is everything before the first '@'."""
    model, sep, version = text.partition("@")
    return model, (version if sep and version else None)


# --- Scoring --------------------------------------------------------------------------------


def metrics(pairs: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """Accuracy, Cohen's kappa, the confusion matrix (rows gold, columns label, in LABELS
    order), per-label precision, recall and F1, and macro F1 of (gold, label) pairs."""
    index = {label: i for i, label in enumerate(LABELS)}
    confusion = [[0] * len(LABELS) for _ in LABELS]
    for gold, label in pairs:
        confusion[index[gold]][index[label]] += 1
    n = len(pairs)
    gold_totals = [sum(row) for row in confusion]
    label_totals = [sum(row[j] for row in confusion) for j in range(len(LABELS))]
    correct = sum(confusion[i][i] for i in range(len(LABELS)))
    accuracy = correct / n if n else None
    kappa = None
    if n:
        expected = sum(g * p for g, p in zip(gold_totals, label_totals, strict=True)) / n**2
        if expected < 1:
            kappa = (correct / n - expected) / (1 - expected)
    per_label = {}
    for i, label in enumerate(LABELS):
        tp, gold, labeled = confusion[i][i], gold_totals[i], label_totals[i]
        per_label[label] = {
            "precision": tp / labeled if labeled else None,
            "recall": tp / gold if gold else None,
            # 2PR / (P + R), which is 0 rather than undefined when tp is 0.
            "f1": 2 * tp / (gold + labeled) if gold + labeled else None,
            "gold": gold,
            "labeled": labeled,
        }
    f1s = [v["f1"] for v in per_label.values() if v["f1"] is not None]
    return {
        "n": n,
        "correct": correct,
        "accuracy": accuracy,
        "kappa": kappa,
        "confusion": confusion,
        "per_label": per_label,
        "macro_f1": sum(f1s) / len(f1s) if f1s else None,
    }


def quote_checked(row: LabelRow) -> str:
    """The label as step 6 applies it: a decisive label needs a quote found in the abstract."""
    if row.label in DECISIVE and row.quote_valid is not True:
        return "no_evidence"
    return row.label


def cost_summary(usage: dict[str, Any]) -> dict[str, Any]:
    """Cost per successful call, per pair and per 10,000 pairs of a run's logged calls; None
    where the cost or the denominator is unknown."""
    calls = (usage.get("calls") or 0) - (usage.get("errors") or 0)
    pairs = usage.get("pairs") or 0
    cost = usage.get("cost")
    return {
        "cost_per_call": cost / calls if cost is not None and calls else None,
        "cost_per_pair": cost / pairs if cost is not None and pairs else None,
        "cost_per_10k_pairs": cost / pairs * PAIRS_SCALE if cost is not None and pairs else None,
        "input_tokens_per_pair": usage["input_tokens"] / pairs if pairs else None,
        "output_tokens_per_pair": usage["output_tokens"] / pairs if pairs else None,
    }


def score(gold: Sequence[GoldItem], labels: dict[tuple[str, str], LabelRow]) -> dict[str, Any]:
    """Scores of one run's labels (by (statement_id, pmid)) against the gold items."""
    found = ((g, labels.get((g.statement_id, g.pmid))) for g in gold)
    covered = [(g, r) for g, r in found if r is not None and r.label in LABELS]
    decisive = [r for _, r in covered if r.label in DECISIVE]
    invalid = [r for r in decisive if r.quote_valid is not True]
    return {
        "gold": len(gold),
        "covered": len(covered),
        "coverage": len(covered) / len(gold) if gold else None,
        "decisive": len(decisive),
        "invalid_quotes": len(invalid),
        "invalid_quote_share": len(invalid) / len(decisive) if decisive else None,
        "as_labeled": metrics([(g.gold_label, r.label) for g, r in covered]),
        "quote_checked": metrics([(g.gold_label, quote_checked(r)) for g, r in covered]),
        "disagreements": [
            {
                "statement_id": g.statement_id,
                "pmid": g.pmid,
                "gold": g.gold_label,
                "label": r.label,
                "quote_valid": r.quote_valid,
            }
            for g, r in covered
            if g.gold_label != r.label or quote_checked(r) != r.label
        ],
    }


def evaluate(
    gold: Path | Gold, cache_dir: Path, runs: Sequence[str | tuple[str, str | None]]
) -> list[dict[str, Any]]:
    """Score each run against the gold set. A run is 'MODEL', 'MODEL@PROMPT_VERSION' or a
    (model, prompt version or None) tuple; a model without a prompt version is scored once
    per prompt version it has labels for in the cache, skipping those with none on the gold
    set. A run given with its prompt version is always scored, even with no labels."""
    gold = gold if isinstance(gold, Gold) else read_gold(gold)
    if not gold.items:
        raise EvaluateError(f"no gold labels in {gold.path}")
    if not runs:
        raise EvaluateError("no run to score; pass --run MODEL[@PROMPT_VERSION]")
    path = Path(cache_dir) / config.LABEL_CACHE
    if not path.exists():
        raise EvaluateError(f"no label cache {path}; run `kgb-augment label --gold FILE` first")
    statement_ids = sorted({g.statement_id for g in gold.items})
    results = []
    with LabelCache(path) as cache:
        for spec in runs:
            model, version = parse_run(spec) if isinstance(spec, str) else spec
            versions = [version] if version else cache.prompt_versions(model)
            for v in versions:
                labels = {
                    (r.statement_id, r.pmid): r for r in cache.labels(v, model, statement_ids)
                }
                result = score(gold.items, labels)
                if not version and not result["covered"]:
                    print(
                        f"evaluate: {model}@{v} has no labels for the gold set; skipped",
                        file=sys.stderr,
                    )
                    continue
                usage = cache.usage("label", model, v)
                results.append(
                    {"model": model, "prompt_version": v, "run": f"{model}@{v}"}
                    | result
                    | {"usage": usage}
                    | cost_summary(usage)
                )
            if not versions:
                print(f"evaluate: no labels by {model} in {path}", file=sys.stderr)
    return results


# --- Report ---------------------------------------------------------------------------------


def _num(value: float | None, digits: int = 3) -> str:
    return "–" if value is None else f"{value:.{digits}f}"


def _tokens(value: float | None) -> str:
    return "–" if value is None else f"{value:,.0f}"


def _pct(value: float | None) -> str:
    return "–" if value is None else f"{value:.0%}"


def _money(value: float | None) -> str:
    return "–" if value is None else f"${value:,.2f}"


def comparison_table(results: Sequence[dict[str, Any]]) -> list[str]:
    lines = [
        "| Run | Coverage | Accuracy | Kappa | Accuracy, quote-checked | Kappa, quote-checked "
        "| Macro F1, quote-checked | Invalid quotes | Cost per 10k pairs |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        a, q = r["as_labeled"], r["quote_checked"]
        lines.append(
            f"| `{r['run']}` | {r['covered']}/{r['gold']} | {_num(a['accuracy'])} "
            f"| {_num(a['kappa'])} | {_num(q['accuracy'])} | {_num(q['kappa'])} "
            f"| {_num(q['macro_f1'])} | {r['invalid_quotes']}/{r['decisive']} "
            f"| {_money(r['cost_per_10k_pairs'])} |"
        )
    return lines


def _scores(title: str, m: dict[str, Any]) -> list[str]:
    lines = [
        f"### {title}",
        "",
        f"Accuracy {_num(m['accuracy'])} ({m['correct']}/{m['n']}), Cohen's kappa "
        f"{_num(m['kappa'])}, macro F1 {_num(m['macro_f1'])}.",
        "",
        "| gold \\ label | " + " | ".join(LABELS) + " |",
        "| --- |" + " --- |" * len(LABELS),
    ]
    for label, row in zip(LABELS, m["confusion"], strict=True):
        lines.append(f"| {label} | " + " | ".join(str(c) for c in row) + " |")
    lines += ["", "| Label | Precision | Recall | F1 | Gold | Labeled |", "| --- |" + " --- |" * 5]
    for label, v in m["per_label"].items():
        lines.append(
            f"| {label} | {_num(v['precision'])} | {_num(v['recall'])} | {_num(v['f1'])} "
            f"| {v['gold']} | {v['labeled']} |"
        )
    return lines + [""]


def report(gold: Gold, results: Sequence[dict[str, Any]]) -> str:
    counts = Counter(g.gold_label for g in gold.items)
    skipped = []
    if gold.unlabeled:
        skipped.append(f"{gold.unlabeled} without a gold label")
    if gold.invalid:
        skipped.append(f"{gold.invalid} invalid")
    if gold.duplicates:
        skipped.append(f"{gold.duplicates} duplicates (last kept)")
    lines = [
        "# Label evaluation",
        "",
        f"Gold set: `{gold.path}`, {len(gold.items)} (statement, abstract) pairs: "
        + ", ".join(f"{label} {counts[label]}" for label in LABELS)
        + "."
        + (f" Lines skipped: {', '.join(skipped)}." if skipped else ""),
        "",
        "## Comparison",
        "",
        *comparison_table(results),
        "",
        "Scores are over the gold pairs the run labeled (coverage). Quote-checked counts a "
        "confirmed or contradicted label whose quote isn't in the abstract as no_evidence, as "
        "step 6 applies it. Invalid quotes: such labels among the run's confirmed and "
        "contradicted labels. Cost is from the run's logged label calls.",
        "",
    ]
    for r in results:
        usage = r["usage"]
        lines += [
            f"## `{r['run']}`",
            "",
            f"- Coverage: {r['covered']} of {r['gold']} gold pairs ({_pct(r['coverage'])})",
            f"- Confirmed or contradicted labels with an invalid quote: {r['invalid_quotes']} of "
            f"{r['decisive']} ({_pct(r['invalid_quote_share'])})",
            f"- Calls: {usage['calls']} ({usage['errors']} failed) over {usage['pairs']} pairs; "
            f"{_tokens(r['input_tokens_per_pair'])} input and "
            f"{_tokens(r['output_tokens_per_pair'])} output tokens per pair",
            f"- Cost: {_money(r['cost_per_call'])} per call, {_money(r['cost_per_pair'])} per "
            f"pair, {_money(r['cost_per_10k_pairs'])} per 10,000 pairs",
            "",
        ]
        lines += _scores("As labeled", r["as_labeled"])
        lines += _scores("Quote-checked", r["quote_checked"])
        if r["disagreements"]:
            lines += [
                "### Disagreements",
                "",
                "| statement_id | PMID | Gold | Label | Quote valid |",
                "| --- | --- | --- | --- | --- |",
            ]
            for d in r["disagreements"]:
                valid = "–" if d["quote_valid"] is None else ("yes" if d["quote_valid"] else "no")
                lines.append(
                    f"| `{d['statement_id']}` | {d['pmid']} | {d['gold']} | {d['label']} "
                    f"| {valid} |"
                )
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --- CLI ------------------------------------------------------------------------------------


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--gold",
        type=Path,
        default=None,
        metavar="FILE",
        help=f"gold JSONL, or a filled-in label_sample.jsonl (default: <work>/{GOLD})",
    )
    parser.add_argument(
        "--run",
        action="append",
        default=None,
        metavar="MODEL[@PROMPT_VERSION]",
        help="run to score; repeat to compare. A model alone scores each of its prompt "
        "versions (default: the run in label_run.json)",
    )


def run(args: argparse.Namespace) -> int:
    gold_path = args.gold if args.gold is not None else args.work / GOLD
    runs: list[str | tuple[str, str | None]] = list(args.run or [])
    try:
        if not runs:
            label_run = records.read_label_run(args.work)
            if label_run is None:
                raise EvaluateError(
                    f"no {args.work / records.LABEL_RUN}; pass --run MODEL[@PROMPT_VERSION]"
                )
            runs = [(label_run.model, label_run.prompt_version)]
        gold = read_gold(gold_path)
        results = evaluate(gold, args.cache_dir, runs)
    except EvaluateError as e:
        print(f"evaluate: {e}", file=sys.stderr)
        return 1
    if not any(r["covered"] for r in results):
        print(
            "evaluate: no run has labels for the gold set; run `kgb-augment label --gold "
            f"{gold_path} --model MODEL` first",
            file=sys.stderr,
        )
        return 1
    path = args.work / REPORT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report(gold, results), encoding="utf-8")
    print(f"evaluate: {len(gold.items)} gold pairs, {len(results)} runs")
    print("\n".join(comparison_table(results)))
    print(f"evaluate: wrote {path}")
    return 0
