#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pydantic>=2.0", "httpx>=0.27", "bmt>=1.5.0", "biolink-model==4.4.6"]
# ///
"""Step 5: an LLM labels every abstract against every statement of a pair and proposes
qualifiers.

    label_abstracts.py [--output OUT] [--model M] [--prompt FILE] [--sample N --seed S]
                       [--gold FILE] [--dry-run] [--limit N] [--workers N]

Writes the labels to the label cache, and label_run.json after a full run that succeeded.
Reads RENCI_AZURE_API_KEY (not for --dry-run).

Run from the repository root; `--help` lists every option.
"""

from pathlib import Path

if __name__ == "__main__":
    # Python puts this directory first on the path, so the library next to it imports.
    from pubmed_evidence import cli

    raise SystemExit(cli.step_main("label", prog=Path(__file__).name))
