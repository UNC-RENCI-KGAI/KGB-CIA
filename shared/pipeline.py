"""Run the stages in order.

`--dry-run` prints what each stage would do and must always work on main.

The order (settled in #23 and #24): clean the merged graph before the filter, so the paid steps
(augmentation, the evidence check) never spend on edges cleaning would drop; filter before
augmentation, so they only run on what the user asked for; clean again at the end, to apply the
drop lists the evidence check wrote. Each entry is (name, module, arguments): the two cleaning
passes run the same command with different inputs and outputs.
"""

from __future__ import annotations

import argparse
import importlib

STAGES: list[tuple[str, str, list[str]]] = [
    ("metadata", "agents.metadata.cli", []),
    ("transformation", "agents.transformation.cli", []),
    # data/transformation/merged -> data/cleaning
    ("cleaning", "agents.cleaning.cli", []),
    ("filter", "agents.filter.cli", []),
    ("augmentation", "agents.augmentation.cli", []),
    # the evidence check writes its drop lists to data/cleaning/drop_lists; this pass applies them
    (
        "final_cleaning",
        "agents.cleaning.cli",
        [
            "--input",
            "data/augmentation",
            "--output",
            "data/final",
            "--drop-lists",
            "data/cleaning/drop_lists",
        ],
    ),
    # serves the final pass's output, not data/cleaning, which kgb-mcp would pick on its own
    ("mcp_interface", "agents.mcp_interface.cli", ["--input", "data/final"]),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KGB-CIA pipeline")
    parser.add_argument("--dry-run", action="store_true", help="describe each stage, run nothing")
    parser.add_argument("--only", choices=[s for s, _, _ in STAGES], help="run a single stage")
    args = parser.parse_args(argv)

    for name, module_path, stage_args in STAGES:
        if args.only and args.only != name:
            continue
        module = importlib.import_module(module_path)
        print(
            f"[{name}] {module.DESCRIPTION}" + (f" ({' '.join(stage_args)})" if stage_args else "")
        )
        if not args.dry_run:
            rc = module.main(list(stage_args))
            if rc:
                print(f"[{name}] failed with exit code {rc}")
                return rc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
