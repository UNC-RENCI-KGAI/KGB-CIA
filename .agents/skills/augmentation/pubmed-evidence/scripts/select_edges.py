#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pydantic>=2.0", "httpx>=0.27", "bmt>=1.5.0", "biolink-model==4.4.6"]
# ///
"""Step 2: choose the edges worth checking in the literature, and group them by node pair.

    select_edges.py [--input GRAPH] [--output OUT] [--rules FILE] [--capped-limit N]
                    [--min-score X]

Reads the graph's nodes.jsonl and edges.jsonl (or ORION's normalized_*.jsonl); writes
entities.jsonl, statements.jsonl, pairs.jsonl and scope_report.json in the work directory.

Run from the repository root; `--help` lists every option.
"""

from pathlib import Path

if __name__ == "__main__":
    # Python puts this directory first on the path, so the library next to it imports.
    from pubmed_evidence import cli

    raise SystemExit(cli.step_main("select", prog=Path(__file__).name))
