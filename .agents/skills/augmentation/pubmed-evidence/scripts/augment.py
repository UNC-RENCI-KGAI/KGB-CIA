#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pydantic>=2.0",
#     "httpx>=0.27",
#     "bmt>=1.5.0",
#     "biolink-model==4.4.6",
#     "openai>=3.44",
#     "anthropic>=2.4",
# ]
# ///
"""The augmentation agent's steps, as `kgb-augment` and the pipeline run them.

    augment.py [--input GRAPH] [--output OUT] [--work DIR] [--cache-dir DIR] STEP [OPTIONS]
    augment.py [--input GRAPH] [--output OUT] [--work DIR] [--cache-dir DIR] [--model M]
               [--reasoning-effort E] [--review-model M] [--limit N] [--max-label-cost USD]
               [--budget USD] [--skip-review]

STEP is one of select, pubmed, label, ground, review, apply and evaluate; `augment.py STEP --help`
lists its options. With no STEP it runs every step but evaluate, in that order (DESIGN.md, "How
the agent runs").

Run from the repository root; `--help` lists every option, and README.md describes them.
"""

from pathlib import Path

if __name__ == "__main__":
    # Python puts this directory first on the path, so the library next to it imports.
    from pubmed_evidence import cli

    raise SystemExit(cli.main(prog=Path(__file__).name))
