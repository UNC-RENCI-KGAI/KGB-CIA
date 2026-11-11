"""Shared test setup: the library on the import path, the toy graph as a fixture, the fake
model client every step's tests use (`from conftest import FakeClient`), and no credentials for
any real model, so that no test can call one.

Run from the repository root: `uv run pytest .agents/skills/augmentation/pubmed-evidence/tests`
(CI runs them with the rest, from pyproject's testpaths).
"""

from __future__ import annotations

import dataclasses
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from pubmed_evidence import config, llm  # noqa: E402
from pubmed_evidence.graphio import write_jsonl  # noqa: E402
from toy import EDGES, NODES  # noqa: E402


@pytest.fixture(autouse=True)
def no_model_credentials(monkeypatch, tmp_path_factory):
    """Every test runs without the models' keys, and the Anthropic SDK finds no `ant auth login`
    profile, so a step that builds a real client fails before calling anything. A test that
    needs a key sets a made-up one."""
    for name in (
        config.AZURE_KEY_VAR,
        config.ANTHROPIC_API_KEY,
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_PROFILE",
        # Workload identity federation, which the Anthropic SDK would use too.
        "ANTHROPIC_FEDERATION_RULE_ID",
        "ANTHROPIC_ORGANIZATION_ID",
        "ANTHROPIC_SERVICE_ACCOUNT_ID",
        "ANTHROPIC_IDENTITY_TOKEN_FILE",
        "ANTHROPIC_IDENTITY_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path_factory.mktemp("no-profile")))


@dataclass
class Sent:
    """One request a FakeClient was sent."""

    system: str
    user: str
    schema: llm.Schema


class FakeClient:
    """A model client in place of the one `llm.client_for` builds: `answer(sent)` gives each
    reply, or an exception for the call to raise, and every request is kept in `sent` in the
    order the calls came. Calls may come from several threads at once. Each call's usage is a
    copy of `usage`; by default one attempt by a model missing from config.MODELS, so of
    unknown cost."""

    def __init__(self, answer: Callable[[Sent], Any], usage: llm.Usage | None = None):
        self.answer = answer
        self.usage = usage or llm.Usage([llm.Attempt("fake-model", 1000, 0, 0, 100)])
        self.sent: list[Sent] = []
        self._lock = threading.Lock()

    def call(self, system, user, schema):
        sent = Sent(system, user, schema)
        with self._lock:
            self.sent.append(sent)
        result = self.answer(sent)
        if isinstance(result, Exception):
            raise result
        return result, llm.Usage([dataclasses.replace(a) for a in self.usage.attempts])


@pytest.fixture
def toy_graph(tmp_path: Path) -> Path:
    """A directory holding the toy graph's nodes.jsonl and edges.jsonl."""
    directory = tmp_path / "filter"
    write_jsonl(directory / "nodes.jsonl", NODES)
    write_jsonl(directory / "edges.jsonl", EDGES)
    return directory
