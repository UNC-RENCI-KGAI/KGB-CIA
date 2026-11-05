"""Pipeline CLI for metadata-driven ORION transformation.

Thin wrapper around `transform.run`, which generates ORION parsers from the metadata stage's
semantic Croissant bundles via .agents/skills/transformation/generate-graph, runs them through
ORION, and writes the metadata-team handoff report. Keep DESCRIPTION and main's signature.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from agents.transformation import transform

DESCRIPTION = (
    "Generate and run ORION parsers from supplied semantic metadata, preserving provenance "
    "and source-record lineage. Full transform; selection happens in the filter stage."
)

DEFAULT_OUTPUT = Path("data/transformation")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help="metadata stage output: semantic Croissant bundles (default: autodetect under data/)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="generated ORION parsers, run artifacts, and handoff report (default: "
        "data/transformation)",
    )
    args = parser.parse_args(argv)
    return transform.run(args.input, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
