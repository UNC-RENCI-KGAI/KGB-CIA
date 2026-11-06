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
        help=f"the PubMed and label caches (default {config.DEFAULT_CACHE_DIR}; "
        "env KGB_AUGMENT_CACHE_DIR)",
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


FULL_RUN_OPTIONS = ("full_model", "full_reasoning_effort", "full_review_model", "full_limit")


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


def run_all(common: argparse.Namespace) -> int:
    run = records.read_label_run(common.work)
    steps = [s for s in FULL_RUN if not (common.skip_review and s == "review")]
    for i, name in enumerate(steps, 1):
        print(f"[augmentation] {i}/{len(steps)} {name}", file=sys.stderr)
        module = importlib.import_module(MODULES[name])
        rc = module.run(_step_args(name, common, run))
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
            "--model, --reasoning-effort, --review-model, --limit and --skip-review before a "
            f"step name are for the full run; put them after it ({parser.prog} {args.command} "
            "--model ...)"
        )
    module = importlib.import_module(MODULES[args.command])
    return module.run(args)
