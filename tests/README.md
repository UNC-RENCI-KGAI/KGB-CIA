# Tests

`make test` runs lint and then about 835 tests in roughly 15 seconds, with no network. Every
outside service (PubMed, NodeNorm, the Name Resolver, the language models, Neo4j) is faked, so
the tests pass offline and cost nothing. CI runs the same tests on every pull request.

## Where they live

Tests for a stage's code are in this folder. Tests for a skill's scripts live with the skill, so
the skill can be copied on its own and keep its tests.

| Area | Tests | About |
| --- | --- | --- |
| Pipeline and schema | `test_smoke.py`, `test_paths.py`, `test_env_example.py` | 40 |
| Transformation | `test_transformation_cli.py`; the generate-graph skill's `tests/` | 60 |
| Cleaning | `test_cleaning.py`, `test_evidence_check.py` | 65 |
| Filter | `test_filter.py`, `test_describe_graph.py` | 40 |
| Augmentation | the pubmed-evidence skill's `tests/` | 510 |
| PubMed client (`shared/pubmed`) | `test_pubmed.py` | 45 |
| MCP interface | `test_mcp_interface.py` | 60 |
| Skill conventions | `test_skill_sync.py`, `test_skill_flags.py` | 20 |
| Metadata | the croissant-semantics skill's `tests/`: run with `make test-metadata` | 135 |

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

The metadata skill's tests aren't in `make test`, because that skill's scripts declare their own
dependencies (`uv run --script`) rather than using the project's.

## Skipped tests

About 30 tests skip unless their tool is present:

- **ORION integration** (generate-graph): needs `robokop-orion==2.0.13` installed.
- **Neo4j smoke test** (MCP): set `NEO4J_URI` and `KGB_MCP_ARTIFACTS` to point it at a loaded
  graph.
- **Helm chart** (MCP): needs `helm`.

## Writing a test

- Use toy data and `tmp_path`. Never call a real service: fake `httpx` with
  `httpx.MockTransport`, or monkeypatch the function that would make the call.
- If you add an option to a skill script, document it. `test_skill_flags.py` fails on an
  option the skill's docs don't mention.
