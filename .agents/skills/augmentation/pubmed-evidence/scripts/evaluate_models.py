#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pydantic>=2.0", "httpx>=0.27", "bmt>=1.5.0", "biolink-model==4.4.6"]
# ///
"""Score cached labels against a hand-labeled gold set, to choose the models.

    evaluate_models.py [--output OUT] --gold FILE [--run MODEL[@PROMPT_VERSION] ...]

Writes evaluate_report.md in the work directory.

Run from the repository root; `--help` lists every option.
"""

from pathlib import Path

if __name__ == "__main__":
    # Python puts this directory first on the path, so the library next to it imports.
    from pubmed_evidence import cli

    raise SystemExit(cli.step_main("evaluate", prog=Path(__file__).name))
