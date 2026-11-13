"""The command line shared by `kgb-augment` and the skill's `scripts/augment.py`.

`main` is both (`agents/augmentation/cli.py` hands it `kgb-augment`'s arguments): with no
subcommand it runs every step in order with its defaults, which is what `shared/pipeline.py`
calls, as `main([])`; each step is also a subcommand:

    kgb-augment select | pubmed | label | ground | review | apply | evaluate

The common options (`--input`, `--output`, `--work`, `--cache-dir`) go before or after the
subcommand. Step modules are imported only when they run.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

from pubmed_evidence import config, records
from shared.pubmed import CACHE_ENV, DEFAULT_CACHE_PATH

DESCRIPTION = (
    "Enrich the baseline graph with extra datasets, and with PubMed evidence for its edges and "
    "more specific edges."
)

# (subcommand, module, help). The full run is every step but `evaluate`, in this order.
STEPS = [
    ("select", "pubmed_evidence.scope", "step 1: choose the edges to check, group by pair"),
    ("pubmed", "pubmed_evidence.pubmed", "step 2: search PubMed per pair, fetch abstracts"),
    ("label", "pubmed_evidence.label", "step 3: an LLM extracts the claims of each pair"),
    ("ground", "pubmed_evidence.ground", "step 4: ground the claims' anatomy names to UBERON"),
    (
        "review",
        "pubmed_evidence.review",
        "step 5: a stronger model reviews the claims that would count",
    ),
    (
        "apply",
        "pubmed_evidence.apply",
        "step 6: refine each checked edge from its claims, write the graph",
    ),
    (
        "evaluate",
        "pubmed_evidence.evaluate",
        "score claims and refinements against a gold set, or draw the pairs to curate",
    ),
]
MODULES = {name: module for name, module, _ in STEPS}
FULL_RUN = ["select", "pubmed", "label", "ground", "review", "apply"]


def _add_common(parser: argparse.ArgumentParser, *, top: bool) -> None:
    """The options every step shares. Subcommands repeat them with SUPPRESS defaults so a value
    given before the subcommand isn't overwritten by the subparser's default."""

    def default(value):
        return value if top else argparse.SUPPRESS

    parser.add_argument(
        "--input",
        type=Path,
        default=default(config.DEFAULT_INPUT),
        help=f"the filter stage's graph: a directory with nodes.jsonl and edges.jsonl "
        f"(default {config.DEFAULT_INPUT})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=default(config.DEFAULT_OUTPUT),
        help=f"where step 6 writes the augmented graph (default {config.DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--work",
        type=Path,
        default=default(None),
        help=f"intermediate files of each step (default <output>/{config.WORK_SUBDIR})",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=default(config.DEFAULT_CACHE_DIR),
        help=f"the label cache and the UBERON release (default {config.DEFAULT_CACHE_DIR}; "
        f"env KGB_AUGMENT_CACHE_DIR). The PubMed cache is the file env {CACHE_ENV} names "
        f"(default {DEFAULT_CACHE_PATH})",
    )


def _parser(prog: str = "kgb-augment") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description=DESCRIPTION)
    _add_common(parser, top=True)
    full = parser.add_argument_group(
        "full run (no subcommand; give a step's own options after its name)"
    )
    full.add_argument(
        "--model",
        dest="full_model",
        help=f"bulk labeling model for step 3 (default: the one label_run.json names, else "
        f"{config.BULK_MODEL})",
    )
    full.add_argument(
        "--reasoning-effort",
        dest="full_reasoning_effort",
        help="reasoning effort for step 3's model, or 'none' to omit (default: the one "
        "label_run.json names, if its model is labeling, else the model's in config.MODELS)",
    )
    full.add_argument(
        "--review-model",
        dest="full_review_model",
        help=f"model for the second pass (default: the reviewer label_run.json names, else "
        f"{config.REVIEW_MODEL})",
    )
    full.add_argument(
        "--limit", dest="full_limit", type=int, help="search and label only the first N pairs"
    )
    full.add_argument(
        "--max-label-cost",
        dest="full_max_label_cost",
        type=float,
        help=f"US dollars step 3 may spend, by its estimate; a run that would cost more labels "
        f"only the first pairs that fit, and says so (default {config.FULL_RUN_LABEL_BUDGET:g}; "
        f"'inf' for no cap; ignored with --limit)",
    )
    full.add_argument(
        "--budget",
        dest="full_budget",
        type=float,
        metavar="USD",
        help="US dollars labeling may cost by step 3's dry-run estimate; a run estimated above "
        "it stops before any model call (review and quote repair aren't in the estimate; see "
        "--max-label-cost to label only the first pairs that fit instead)",
    )
    full.add_argument(
        "--skip-review",
        action="store_true",
        help="skip the review step; step 6 holds every pair with a claim that would count but "
        "has no review, and writes its edges unchanged",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="STEP")
    for name, module_path, help_text in STEPS:
        sub = subparsers.add_parser(name, help=help_text, description=help_text)
        _add_common(sub, top=False)
        importlib.import_module(module_path).add_arguments(sub)
    return parser


def _resolve(args: argparse.Namespace) -> argparse.Namespace:
    args.work = config.work_dir(args.output, args.work)
    return args


FULL_RUN_OPTIONS = (
    "full_model",
    "full_reasoning_effort",
    "full_review_model",
    "full_limit",
    "full_max_label_cost",
    "full_budget",
)


def _overrides(name: str, common: argparse.Namespace, run) -> dict:
    """What the full run sets on a step, beyond its defaults.

    It continues the run `label_run.json` names, if any: that run's model, prompt, effort,
    reviewer and review effort, so after the skill has tuned a run, `main([])` reuses its cached
    claims and reviews instead of relabeling everything with the defaults. `--model`,
    `--reasoning-effort` and `--review-model` override; the run's effort carries over only
    while its model is labeling, and the review's while its reviewer is reviewing.
    """
    if name == "pubmed":
        return {"limit": common.full_limit}
    if name == "label":
        out = {
            "limit": common.full_limit,
            "model": common.full_model,
            "reasoning_effort": common.full_reasoning_effort,
        }
        if run is not None:
            out["model"] = common.full_model or run.model
            if run.prompt and Path(run.prompt).exists():
                out["prompt"] = Path(run.prompt)
            if common.full_reasoning_effort is None and out["model"] == run.model:
                out["reasoning_effort"] = run.reasoning_effort
        return out
    if name == "review":
        reviewer = run.reviewer if run is not None else None
        out = {"model": common.full_review_model or reviewer}
        if reviewer is not None and out["model"] == reviewer:
            out["reasoning_effort"] = run.review_reasoning_effort
        return out
    return {}


def _step_args(name: str, common: argparse.Namespace, run=None) -> argparse.Namespace:
    """One step's defaults, with the common options and the full run's overrides on top."""
    sub = argparse.ArgumentParser(prog=f"kgb-augment {name}")
    importlib.import_module(MODULES[name]).add_arguments(sub)
    args = sub.parse_args([])
    for key in ("input", "output", "work", "cache_dir"):
        setattr(args, key, getattr(common, key))
    for key, value in _overrides(name, common, run).items():
        if value is not None and hasattr(args, key):
            setattr(args, key, value)
    return args


def affordable(costs: list[float], budget: float) -> int:
    """How many of the first pairs fit the budget, given each pair's estimated cost in order."""
    spent = 0.0
    for n, cost in enumerate(costs):
        spent += cost
        if spent > budget:
            return n
    return len(costs)


def _label_estimate(common: argparse.Namespace, run, limit: int | None = None) -> dict:
    """Label's dry run on the full run's step 3 arguments (the first `limit` pairs, if given),
    which sends nothing: the pairs with abstracts still to answer (`pairs_to_call`) and what
    answering them would cost. Pairs already labeled cost nothing."""
    label = importlib.import_module(MODULES["label"])
    args = _step_args("label", common, run)
    if limit is not None:
        args.limit = limit
    return label.label_pairs(
        args.work,
        args.cache_dir,
        model=args.model,
        prompt=args.prompt,
        limit=args.limit,
        abstracts_per_call=args.abstracts_per_call,
        reasoning_effort=args.reasoning_effort,
        dry_run=True,
    )


def _over_budget(common: argparse.Namespace, estimate: dict) -> bool:
    """Whether --budget was given and the estimate is over it; says so if it is."""
    budget, cost = common.full_budget, estimate["estimated_cost"]
    if budget is None or cost is None or cost <= budget:
        return False
    label = importlib.import_module(MODULES["label"])
    print(
        f"[augmentation] error: labeling would cost about {label._money(cost)} by the dry "
        f"run's estimate, over the {label._money(budget)} budget; stopping before any model "
        "call. The estimate covers labeling alone: review and quote repair come on top. Rerun "
        "with a larger --budget, or with --max-label-cost to label only the first pairs that "
        "fit.",
        file=sys.stderr,
    )
    return True


def _label_limit(common: argparse.Namespace, estimate: dict) -> int | None:
    """The full run's cap on step 3: None if every pair fits the budget (or --limit was given),
    else how many of the first pairs do, by the dry run's estimate."""
    if common.full_limit is not None:
        return None
    budget = common.full_max_label_cost
    if budget is None:
        budget = config.FULL_RUN_LABEL_BUDGET
    label = importlib.import_module(MODULES["label"])
    costs = estimate.get("estimated_cost_by_pair", [])
    n = affordable(costs, budget)
    if n == len(costs):
        return None
    print(
        f"[augmentation] flag: labeling every pair would cost about {label._money(sum(costs))}, "
        f"over the {label._money(budget)} budget; labeling the first {n:,} of {len(costs):,} "
        f"pairs. To label more, rerun with --max-label-cost or --limit; pairs already labeled "
        f"are cached.",
        file=sys.stderr,
    )
    return n


def _finish_labeling(common: argparse.Namespace, run, label, args: argparse.Namespace) -> int:
    """After a label run that returned 0: if pairs are still not fully labeled (a call failed,
    or a reply left an abstract out), run label once more, since answers already made are
    cached; if pairs still are, stop with 1 instead of going on to write their edges
    unchecked. The count is the dry run's `pairs_to_call` on the same pairs."""
    left = _label_estimate(common, run, args.limit)["pairs_to_call"]
    if not left:
        return 0
    print(
        f"[augmentation] {left:,} pairs aren't fully labeled (a call failed, or a reply left "
        "an abstract out); running label again",
        file=sys.stderr,
    )
    rc = label.run(args)
    if rc:
        return rc
    left = _label_estimate(common, run, args.limit)["pairs_to_call"]
    if left:
        print(
            f"[augmentation] error: {left:,} pairs still aren't fully labeled after a second "
            "label run, so their edges can't be checked; stopping before ground, review and "
            "apply. Rerun to retry; answers already made are cached.",
            file=sys.stderr,
        )
        return 1
    return 0


def run_all(common: argparse.Namespace) -> int:
    run = records.read_label_run(common.work)
    steps = [s for s in FULL_RUN if not (common.skip_review and s == "review")]
    for i, name in enumerate(steps, 1):
        print(f"[augmentation] {i}/{len(steps)} {name}", file=sys.stderr)
        module = importlib.import_module(MODULES[name])
        args = _step_args(name, common, run)
        if name == "label":
            try:
                estimate = _label_estimate(common, run)
            except (FileNotFoundError, ValueError) as e:
                print(f"label: error: {e}", file=sys.stderr)
                return 1
            if _over_budget(common, estimate):
                return 1
            limit = _label_limit(common, estimate)
            if limit is not None:
                args.limit = limit
        rc = module.run(args)
        if name == "label" and not rc:
            rc = _finish_labeling(common, run, module, args)
        if rc:
            print(f"[augmentation] {name} failed (exit {rc}); later steps not run", file=sys.stderr)
            return rc
    return 0


def main(argv: list[str] | None = None, prog: str = "kgb-augment") -> int:
    parser = _parser(prog)
    args = _resolve(parser.parse_args(argv))
    if args.command is None:
        return run_all(args)
    # A step's own defaults would silently win over these.
    if any(getattr(args, k) is not None for k in FULL_RUN_OPTIONS) or args.skip_review:
        parser.error(
            "--model, --reasoning-effort, --review-model, --limit, --max-label-cost, --budget "
            f"and --skip-review before a step name are for the full run; put them after it "
            f"({parser.prog} {args.command} --model ...)"
        )
    module = importlib.import_module(MODULES[args.command])
    return module.run(args)
