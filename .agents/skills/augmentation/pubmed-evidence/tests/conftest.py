"""Shared test setup: the library on the import path, and the toy graph as a fixture.

Run from the repository root: `uv run pytest .agents/skills/augmentation/pubmed-evidence/tests`
(CI runs them with the rest, from pyproject's testpaths).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from pubmed_evidence.graphio import write_jsonl  # noqa: E402
from toy import EDGES, NODES  # noqa: E402


@pytest.fixture
def toy_graph(tmp_path: Path) -> Path:
    """A directory holding the toy graph's nodes.jsonl and edges.jsonl."""
    directory = tmp_path / "filter"
    write_jsonl(directory / "nodes.jsonl", NODES)
    write_jsonl(directory / "edges.jsonl", EDGES)
    return directory
