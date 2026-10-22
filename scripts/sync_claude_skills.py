#!/usr/bin/env python3
"""Expose grouped .agents skills as flat, relative Claude Code symlinks."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


class SkillSyncError(Exception):
    """An unsafe or ambiguous skill layout."""


def discover_skills(source: Path) -> dict[str, Path]:
    if source.is_symlink() or not source.is_dir():
        raise SkillSyncError(f"Skill source must be a real directory: {source}")

    skills: dict[str, Path] = {}
    for directory, children, files in os.walk(source, followlinks=False):
        current = Path(directory)
        children[:] = sorted(
            child
            for child in children
            if not child.startswith(".")
            and child not in {"node_modules", "__pycache__"}
            and not (current / child).is_symlink()
        )
        if "SKILL.md" not in files:
            continue
        if (current / "SKILL.md").is_symlink():
            raise SkillSyncError(f"SKILL.md must be a real file: {current / 'SKILL.md'}")
        if current == source:
            raise SkillSyncError("Put skills in subdirectories, not in the skills root itself")
        children.clear()  # Bundled references/assets are not additional skills.
        name = current.name
        if name in skills:
            raise SkillSyncError(f"Duplicate skill name '{name}': {skills[name]} and {current}")
        skills[name] = current
    return dict(sorted(skills.items()))


def sync_skills(project_root: Path, *, dry_run: bool = False, prune: bool = False) -> int:
    root = project_root.resolve()
    source = root / ".agents" / "skills"
    destination = root / ".claude" / "skills"
    skills = discover_skills(source)

    for path in (destination.parent, destination):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise SkillSyncError(f"Destination must be a real directory, not a symlink: {path}")

    stale: list[Path] = []
    if prune and destination.exists():
        for link in sorted(destination.iterdir()):
            if not link.is_symlink() or link.exists():
                continue
            # Resolve the target to avoid pruning links that escape through symlinks
            # or '..'. Unresolvable targets (e.g. loops) are left for manual review.
            try:
                target = link.resolve()
            except (OSError, RuntimeError):
                continue
            if target.is_relative_to(source) and target != source:
                stale.append(link)

    # Validate every collision before creating or removing anything.
    pending: list[tuple[Path, Path]] = []
    for name, skill in skills.items():
        link = destination / name
        if link.is_symlink() and link.resolve() == skill.resolve():
            continue
        if (link.exists() or link.is_symlink()) and link not in stale:
            raise SkillSyncError(f"Refusing to overwrite existing entry: {link}")
        pending.append((link, skill))

    if not dry_run:
        destination.mkdir(parents=True, exist_ok=True)
    for link in stale:
        if not dry_run:
            link.unlink()
        print(f"{'Would prune' if dry_run else 'Pruned'} {link.relative_to(root)}")
    for link, skill in pending:
        target = os.path.relpath(skill, destination)
        if not dry_run:
            link.symlink_to(target, target_is_directory=True)
        print(f"{'Would link' if dry_run else 'Linked'} {link.relative_to(root)} -> {target}")
    print(
        f"Discovered {len(skills)} skill(s); {len(pending)} new link(s); "
        f"{len(stale)} {'link(s) to prune' if dry_run else 'link(s) pruned'}."
    )
    return len(pending)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Project root (defaults to the repository containing this script)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and preview without changes",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Remove broken destination links targeting this project's .agents/skills",
    )
    args = parser.parse_args()
    try:
        sync_skills(args.project_root, dry_run=args.dry_run, prune=args.prune)
    except (SkillSyncError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
