"""Run the stages in order.

`--dry-run` prints what each stage would do and must always work on main.
"""

from __future__ import annotations

import argparse
import importlib

STAGES = [
    ("metadata", "agents.metadata.cli"),
    ("transformation", "agents.transformation.cli"),
    ("filter", "agents.filter.cli"),
    ("augmentation", "agents.augmentation.cli"),
    ("cleaning", "agents.cleaning.cli"),
    ("mcp_interface", "agents.mcp_interface.cli"),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KGB-CIA pipeline")
    parser.add_argument("--dry-run", action="store_true", help="describe each stage, run nothing")
    parser.add_argument("--only", choices=[s for s, _ in STAGES], help="run a single stage")
    args = parser.parse_args(argv)

    for name, module_path in STAGES:
        if args.only and args.only != name:
            continue
        module = importlib.import_module(module_path)
        print(f"[{name}] {module.DESCRIPTION}")
        if not args.dry_run:
            rc = module.main([])
            if rc:
                print(f"[{name}] failed with exit code {rc}")
                return rc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
