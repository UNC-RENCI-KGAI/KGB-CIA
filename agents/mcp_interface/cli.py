"""Command-line entry point for the MCP interface.

Replace the body of `main` with the real agent. Keep the signature so `shared/pipeline.py`
and the `kgb-*` console scripts keep working.
"""

from __future__ import annotations

import argparse

DESCRIPTION = (
    "Expose the final graph to any local agent through an MCP server modeled on the OKN MCP: "
    "neighbors, paths, and edge lineage."
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--input", help="path to the previous stage's output")
    parser.add_argument("--output", help="where to write this stage's output")
    args = parser.parse_args(argv)
    print("MCP interface: not implemented yet")
    print(f"  input={args.input} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
