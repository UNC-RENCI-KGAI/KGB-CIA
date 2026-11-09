"""Command-line entry point for the Filter agent.

Thin wrapper around `subgraph`. Keep DESCRIPTION and main's signature so `shared/pipeline.py`
and the `kgb-*` console scripts keep working.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from agents.filter import subgraph

DESCRIPTION = (
    "Cut the subgraph the user asked for out of the full graph, from a focus file the agent "
    "writes from their request; with no focus, the user's graph is the whole graph."
)

# The cleaned graph when cleaning has run, else the merged one
DEFAULT_INPUTS = (Path("data/cleaning"), Path("data/transformation/merged"))
DEFAULT_OUTPUT = Path("data/filter")


def find_input() -> Path | None:
    for path in DEFAULT_INPUTS:
        if (path / "nodes.jsonl").exists() and (path / "edges.jsonl").exists():
            return path
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument(
        "--focus",
        type=Path,
        help="focus file written from the user's request (default: none, the whole graph)",
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="graph with nodes.jsonl and edges.jsonl (default: data/cleaning, else "
        "data/transformation/merged)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="where to write the subgraph (default: data/filter/<focus name>, or "
        "data/filter/whole)",
    )
    args = parser.parse_args(argv)

    input_dir = args.input or find_input()
    if input_dir is None or not (input_dir / "edges.jsonl").exists():
        print(f"Filter: no graph at {input_dir or ' or '.join(map(str, DEFAULT_INPUTS))}")
        print("  run the transformation (and cleaning) stages first, or pass --input")
        return 1
    try:
        if args.focus:
            focus = subgraph.load_focus(args.focus)
            output = args.output or DEFAULT_OUTPUT / focus["name"]
            report = subgraph.extract(focus, input_dir, output)
        else:
            output = args.output or DEFAULT_OUTPUT / "whole"
            report = subgraph.whole_graph(input_dir, output)
    except subgraph.FocusError as e:
        print(f"Filter: {e}")
        return 2
    print(subgraph.summary(report))
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
