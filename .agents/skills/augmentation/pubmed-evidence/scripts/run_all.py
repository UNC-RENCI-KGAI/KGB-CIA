#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pydantic>=2.0", "httpx>=0.27", "bmt>=1.5.0", "biolink-model==4.4.6"]
# ///
"""Every step in order, as `kgb-augment` and the pipeline run them: select, pubmed, sentences,
label, review, apply.

    run_all.py [--input GRAPH] [--output OUT] [--model M] [--review-model M] [--limit N]
               [--skip-review] [--infores CURIE]

With a label_run.json in the work directory it continues that run (its model, prompt and
reviewer), reusing every cached label. Run from the repository root; `--help` lists every
option.
"""

from pathlib import Path

if __name__ == "__main__":
    # Python puts this directory first on the path, so the library next to it imports.
    from pubmed_evidence import cli

    raise SystemExit(cli.main(prog=Path(__file__).name))
