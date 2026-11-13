"""Where the pipeline keeps its caches: one root, KGB_CACHE_DIR, by default the repository's
`data/cache/` (gitignored), whatever directory a command runs from. Under it:

- `croissant/`: the raw RoboCroissant files the croissant-semantics skill reads. People put them
  there; no code writes there.
- `croissant-semantics/`: that skill's downloads, validation results and Biolink vocabulary. Its
  scripts run on their own (`uv run --script`), so they find this root themselves, the same way
  (`.agents/skills/metadata/croissant-semantics/scripts/common.py`).
- `pubmed/`: `shared.pubmed`'s `pubmed.sqlite` (KGB_PUBMED_CACHE names another file).
- `augmentation/`: the augmentation agent's label cache and UBERON release
  (KGB_AUGMENT_CACHE_DIR names another directory).

Point KGB_CACHE_DIR at a directory outside the checkout to share the caches between checkouts
or git worktrees, each of which otherwise has a `data/` of its own.
"""

from __future__ import annotations

import os
from pathlib import Path

CACHE_ROOT_ENV = "KGB_CACHE_DIR"
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE_ROOT = REPO_ROOT / "data" / "cache"


def cache_root() -> Path:
    """KGB_CACHE_DIR if set (`~` expanded), else the repository's data/cache."""
    value = os.environ.get(CACHE_ROOT_ENV)
    return Path(value).expanduser() if value else DEFAULT_CACHE_ROOT


def cache_dir(name: str) -> Path:
    """One cache's directory under the root: `croissant`, `croissant-semantics`, `pubmed` or
    `augmentation`."""
    return cache_root() / name
