"""Pipeline entry point and repository-only offline graph check."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from kgbcia_mcp.cli import main as server_main

if __package__:
    from .check import check_graph, format_check_result, resolve_default_input
else:
    # Support `python agents/mcp_interface/cli.py` as documented for local debugging.
    from check import check_graph, format_check_result, resolve_default_input

DESCRIPTION = (
    "Expose the final graph to any local agent through an MCP server modeled on the OKN MCP: "
    "neighbors, paths, and edge lineage."
)


def main(argv: list[str] | None = None) -> int:
    """Run the default offline check or delegate published server commands."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] in {"serve", "hubs"}:
        return server_main(arguments)
    if arguments and arguments[0] == "check":
        arguments = arguments[1:]

    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--input", type=Path, help="directory containing nodes.jsonl/edges.jsonl")
    args = parser.parse_args(arguments)
    input_dir = args.input or resolve_default_input()
    if input_dir is None:
        print(
            "MCP check: no graph input found. Pass --input, or create data/cleaning or "
            "data/transformation/merged."
        )
        return 1
    result = check_graph(input_dir)
    print(format_check_result(result))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
