"""Command-line entry point for the Cleaning agent.

Thin wrapper around `garbage.collect`. Keep DESCRIPTION and main's signature so
`shared/pipeline.py` and the `kgb-*` console scripts keep working.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from agents.cleaning import garbage

DESCRIPTION = (
    "Garbage-collect the merged graph: drop dangling, unresolved, unexplained and "
    "drop-listed edges and the nodes left orphaned, and report what was removed and why."
)

DEFAULT_INPUT = Path("data/transformation/merged")
DEFAULT_OUTPUT = Path("data/cleaning")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help="directory holding nodes.jsonl and edges.jsonl (default: data/transformation/merged)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="where to write the cleaned graph and cleaning_report.json; drop lists are read "
        "from its drop_lists/ (default: data/cleaning)",
    )
    parser.add_argument(
        "--drop-lists",
        type=Path,
        default=None,
        help="read drop lists from this directory instead of <output>/drop_lists; use it when "
        "they were written for another cleaning pass, as the pipeline's final pass reads "
        "data/cleaning/drop_lists",
    )
    args = parser.parse_args(argv)

    if args.input.resolve() == args.output.resolve():
        print("Cleaning: --output must differ from --input")
        return 2
    try:
        report = garbage.collect(args.input, args.output, args.drop_lists)
    except FileNotFoundError as e:
        print(f"Cleaning: {e}")
        print("  run the transformation stage first, or pass --input")
        return 1
    print(garbage.summary(report))
    print(f"Wrote {args.output}/nodes.jsonl, edges.jsonl and cleaning_report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
