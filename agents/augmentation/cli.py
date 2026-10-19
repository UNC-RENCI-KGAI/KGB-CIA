"""Command-line entry point for the Augmentation agent.

Replace the body of `main` with the real agent. Keep the signature so `shared/pipeline.py`
and the `kgb-*` console scripts keep working.
"""

from __future__ import annotations

import argparse

DESCRIPTION = (
    "Enrich the baseline graph with extra datasets and PubMed evidence for or against edges."
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--input", help="path to the previous stage's output")
    parser.add_argument("--output", help="where to write this stage's output")
    args = parser.parse_args(argv)
    print(f"Augmentation agent: not implemented yet")
    print(f"  input={args.input} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
