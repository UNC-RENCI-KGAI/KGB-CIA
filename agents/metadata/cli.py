"""Pipeline CLI for the metadata stage's semantics step.

Thin wrapper around `semantics.run`, which applies and checks each source's semantics plan and
skips sources whose inputs haven't changed. Keep DESCRIPTION and main's signature.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from agents.metadata import semantics

DESCRIPTION = (
    "Describe each source file and its columns as Croissant metadata. Purely descriptive; "
    "builds no graph."
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/croissants"),
        help="prepared croissants, one directory per source (default: data/croissants)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/metadata"),
        help="semantic Croissant bundles, one directory per source (default: data/metadata)",
    )
    parser.add_argument(
        "--plans",
        type=Path,
        help="earlier bundles to take a source's semantics_plan.json from when it has none yet",
    )
    parser.add_argument(
        "--force", action="store_true", help="apply and check every source even if unchanged"
    )
    args = parser.parse_args(argv)
    return semantics.run(args.input, args.output, args.plans, args.force)


if __name__ == "__main__":
    raise SystemExit(main())
