# Tests

`make test` runs lint, then about 835 tests in roughly 15 seconds, then the metadata skill's
136 tests. No test touches the network. Every outside service (PubMed, NodeNorm, the Name
Resolver, the language models) is faked, so the tests pass offline and cost nothing.
`make test-neo4j` runs the MCP server against a toy graph in a real Neo4j. CI runs both on every
pull request; between the two, every test runs there.

## Where they live

Tests for a stage's code are in this folder. Tests for a skill's scripts live with the skill, so
the skill can be copied on its own and keep its tests.

| Area | Tests | About |
| --- | --- | --- |
| Pipeline and schema | `test_smoke.py`, `test_paths.py`, `test_env_example.py` | 40 |
| Transformation | `test_transformation_cli.py`; the generate-graph skill's `tests/` | 60 |
| Cleaning | `test_cleaning.py`, `test_evidence_check.py` | 75 |
| Filter | `test_filter.py`, `test_describe_graph.py` | 40 |
| Augmentation | the pubmed-evidence skill's `tests/` | 510 |
| PubMed client (`shared/pubmed`) | `test_pubmed.py` | 45 |
| MCP interface | `test_mcp_interface.py`; `make test-neo4j` | 60 |
| Skill conventions | `test_skill_sync.py`, `test_skill_flags.py` | 20 |
| Metadata | the croissant-semantics skill's `tests/` (`make test-metadata`) | 135 |

Most of the augmentation tests check one rule of the refinement or labeling step at a time on
toy pairs (`tests/toy.py` in that skill). That's why there are so many of them, and why each
runs in milliseconds.

## Running some of them

```bash
uv run pytest -q tests/test_cleaning.py                         # one file
uv run pytest -q tests/test_smoke.py::test_schema_round_trip    # one test
uv run pytest -q .agents/skills/augmentation/pubmed-evidence/tests
uv run pytest -q -k drop_list                                   # by name
make test-metadata                                              # the metadata skill's tests
```

The metadata skill's scripts declare their own dependencies (`uv run --script`) rather than
using the project's, so `make test-metadata` runs its tests with those dependencies, separately
from pytest's main run. `make test` runs it too.

## Tests that need a tool

Three groups of tests need something beyond the project's Python packages. CI provides all of
them; on your own machine, a test skips (and says why) when its tool is missing.

- **ORION integration** (generate-graph, 27 tests) needs `robokop-orion==2.0.13`, a dev
  dependency, so `make setup` installs it.
- **Helm chart** (MCP, 1 test) needs `helm` on your `PATH`.
- **Neo4j smoke test** (MCP, 1 test) needs a loaded Neo4j. `make test-neo4j` provides one:
  [`neo4j_smoke.sh`](neo4j_smoke.sh) starts a throwaway Neo4j container (Docker), loads a toy
  graph with [`neo4j_toy_graph.py`](neo4j_toy_graph.py), restarts it read-only as the
  deployment does, and runs the test against it. To run it against another graph instead, set
  `NEO4J_URI` and `KGB_MCP_ARTIFACTS` yourself and run that test.

## Writing a test

- Use toy data and `tmp_path`. Never call a real service: fake `httpx` with
  `httpx.MockTransport`, or monkeypatch the function that would make the call.
- If you add an option to a skill script, document it. `test_skill_flags.py` fails on an
  option the skill's docs don't mention.
