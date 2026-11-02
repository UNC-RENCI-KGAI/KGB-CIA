#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pydantic>=2.0", "httpx>=0.27", "bmt>=1.5.0", "biolink-model==4.4.6"]
# ///
"""Step 6: apply the cached labels to the graph.

    apply_labels.py [--input GRAPH] [--output OUT] [--infores CURIE] [--no-review]

Writes OUT/nodes.jsonl and OUT/edges.jsonl: supporting PMIDs added to the original edges, new
negated and qualified edges after them; and apply_report.json in the work directory.

Run from the repository root; `--help` lists every option.
"""

from pathlib import Path

if __name__ == "__main__":
    # Python puts this directory first on the path, so the library next to it imports.
    from pubmed_evidence import cli

    raise SystemExit(cli.step_main("apply", prog=Path(__file__).name))
