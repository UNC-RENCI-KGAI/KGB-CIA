"""Command-line entry point for the Augmentation agent: `kgb-augment`, and the pipeline's stage.

The agent is the pubmed-evidence skill in `.agents/skills/augmentation/pubmed-evidence/`. Its
library (`scripts/pubmed_evidence/`) does the work; this module hands it `kgb-augment`'s
arguments. The library is imported only when `main` runs, so importing this module for its
`DESCRIPTION` (as `make demo` does) stays cheap.
"""

from __future__ import annotations

import sys
from pathlib import Path

DESCRIPTION = (
    "Enrich the baseline graph with extra datasets, and with PubMed evidence for its edges and "
    "more specific edges."
)

SKILL_SCRIPTS = (
    Path(__file__).resolve().parents[2]
    / ".agents"
    / "skills"
    / "augmentation"
    / "pubmed-evidence"
    / "scripts"
)


def main(argv: list[str] | None = None) -> int:
    # Appended, not prepended: the directory also holds the skill's entry script, augment.py,
    # which must not shadow anything else of the same name.
    if str(SKILL_SCRIPTS) not in sys.path:
        sys.path.append(str(SKILL_SCRIPTS))
    from pubmed_evidence import cli

    return cli.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
