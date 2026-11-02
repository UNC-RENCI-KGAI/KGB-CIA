#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pydantic>=2.0", "httpx>=0.27", "bmt>=1.5.0", "biolink-model==4.4.6"]
# ///
"""Step 3: search PubMed for each pair and fetch the abstracts.

    search_pubmed.py [--output OUT] [--max-abstracts N] [--limit N] [--refresh] [--no-nodenorm]

Reads pairs.jsonl and entities.jsonl; writes pubmed.jsonl, and every search, abstract and
NodeNorm entry to the PubMed cache. Reads NCBI_EMAIL and NCBI_API_KEY.

Run from the repository root; `--help` lists every option.
"""

from pathlib import Path

if __name__ == "__main__":
    # Python puts this directory first on the path, so the library next to it imports.
    from pubmed_evidence import cli

    raise SystemExit(cli.step_main("pubmed", prog=Path(__file__).name))
