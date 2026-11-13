#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pydantic>=2.0",
#     "httpx>=0.27",
#     "kgbcia-mcp",
#     "bmt>=1.5.0",
#     "biolink-model==4.4.6",
#     "openai>=3.44",
#     "anthropic>=2.4",
# ]
# ///
"""The augmentation agent's steps, as `kgb-augment` and the pipeline run them.

    augment.py [--input GRAPH] [--output OUT] [--work DIR] [--cache-dir DIR] STEP [OPTIONS]
    augment.py [--input GRAPH] [--output OUT] [--model M] [--review-model M] [--limit N]
               [--skip-review]

STEP is one of select, pubmed, label, ground, review, apply and evaluate; `augment.py STEP --help`
lists its options. With no STEP it runs every step but evaluate, in order: select, pubmed,
label, ground, review, apply. With a label_run.json in the work directory, that full run
continues the run it names (its model, prompt and reviewer), reusing every cached claim and
verdict.

Run from the repository root; `--help` lists every option.
"""

from pathlib import Path

if __name__ == "__main__":
    # Python puts this directory first on the path, so the library next to it imports.
    from pubmed_evidence import cli

    raise SystemExit(cli.main(prog=Path(__file__).name))
