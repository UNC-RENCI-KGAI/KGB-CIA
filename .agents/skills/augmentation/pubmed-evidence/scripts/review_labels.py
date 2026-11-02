#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pydantic>=2.0", "httpx>=0.27", "bmt>=1.5.0", "biolink-model==4.4.6"]
# ///
"""Step 5b: a stronger model reviews the labels that would add edges (contradictions and
qualifier proposals).

    review_labels.py [--output OUT] [--model M] [--prompt FILE]
    review_labels.py --export FILE --reviewer NAME     (for a Pi subagent; no calls)
    review_labels.py --import FILE --reviewer NAME

Writes the verdicts to the label cache and names the reviewer in label_run.json.

Run from the repository root; `--help` lists every option.
"""

from pathlib import Path

if __name__ == "__main__":
    # Python puts this directory first on the path, so the library next to it imports.
    from pubmed_evidence import cli

    raise SystemExit(cli.step_main("review", prog=Path(__file__).name))
