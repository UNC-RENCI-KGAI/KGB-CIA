# AGENTS.md

Guidance for coding agents (Claude Code, Codex, Copilot, and others) working in this repository.
Directories with their own rules have their own `AGENTS.md`: [`agents/`](agents/AGENTS.md),
[`shared/`](shared/AGENTS.md), [`evaluation/`](evaluation/AGENTS.md). Read the one for the
directory you are editing before you change anything there.

Don't add a `CLAUDE.md` anywhere in the repo, even one that only imports this file: Claude Code
reads `AGENTS.md` files directly only as long as no `CLAUDE.md` exists in the repo. (`/init`
creates one, so delete it afterwards and move anything useful into an `AGENTS.md`.)

## What this is

KGB-CIA, a team entry for the Bio-Agent Knowledge Graph Construction Challenge. A pipeline of
agents builds a Biolink labeled property graph from Pharos / TCRD data and exposes it over MCP;
a separate evaluation pipeline scores it by GNN link prediction against the Pharos baseline.
The final submission (Nov 17, 2026) is the graph from the Nov 9, 2026 transformation run. The
full plan and the reasoning behind each stage is in [`docs/agent_plan.md`](docs/agent_plan.md).

## Commands

```bash
make setup     # uv sync --dev --all-extras (uv is required)
make lint      # ruff check + ruff format --check
make format    # ruff --fix + ruff format
make test      # lint, pytest, then the metadata skill's tests
make test-neo4j  # MCP server against a toy graph in a Neo4j container (needs Docker)
make demo      # python -m shared.pipeline --dry-run; must always pass on main

uv run pytest -q tests/test_smoke.py::test_schema_round_trip   # one test
uv run python -m shared.pipeline --dry-run --only filter       # describe one stage
uv run kgb-filter --input <in> --output <out>                  # run one stage alone
```

CI (`.github/workflows/ci.yml`) runs `make test`, `make test-neo4j` and the dry run on Python
3.12, with helm installed, so every test runs there. Ruff
line length is 100; rules `E F I W UP B`. Add dependencies with `uv add <pkg>` so `uv.lock`
stays in sync.

## Architecture

- **Stages are packages under `agents/`, run in the order listed in `STAGES` in
  `shared/pipeline.py`:** metadata → transformation → cleaning → filter → augmentation →
  evidence_check → final_cleaning → mcp_interface (settled in #23 and #24). The runner imports each
  `agents.<stage>.cli` module, prints its `DESCRIPTION`, and (without `--dry-run`) calls `main`
  with that stage's arguments from `STAGES`; a nonzero return stops the pipeline. The two
  cleaning passes are the same `kgb-clean`: the first cleans the merged graph, the last applies
  the evidence check's drop lists (`data/cleaning/drop_lists`) to augmentation's output and
  writes `data/final`, which the MCP interface is then given with `--input` (on its own,
  `kgb-mcp` would fall back to `data/cleaning`).
- **Each stage is also a console script** (`kgb-metadata`, `kgb-transform`, `kgb-filter`,
  `kgb-augment`, `kgb-evidence`, `kgb-clean`, `kgb-mcp`, `kgb-pipeline`) declared in
  `pyproject.toml`.
- **Handoffs between stages use the pydantic records in `shared/schema.py`** (`Node`, `Edge`,
  `Graph`, `TransformReport`). `Node` and `Edge` are flat Biolink/KGX records shaped like
  ORION's `nodes.jsonl` and `edges.jsonl`. Every `Edge` must carry its Biolink provenance
  (`primary_knowledge_source`, `knowledge_level`, `agent_type`; plus aggregators and
  publications where known): explainability is 20% of the score, and the MCP interface has to
  answer "why does A connect to B?" from it. Which file and columns produced each node or edge
  is not Biolink, so it lives in the transformation agent's `TransformReport`, which points
  into the source's Croissant metadata.
- **Every node ID goes through NodeNorm.** In transformation, ORION normalizes them;
  code outside ORION that needs NodeNorm (such as `shared/pubmed/pairs.py`) calls
  `shared/nodenorm.py`.
- **`evaluation/` is deliberately not a stage** and is not in `STAGES`.

Every stage command is implemented on `main` except `kgb-metadata`, which is a stub that prints
"not implemented yet". `kgb-transform` builds every source
and merges them into one graph (`data/transformation/merged/`), `kgb-clean` reads that merged graph
by default and writes `data/cleaning`, `kgb-filter` cuts the user's subgraph from
`data/cleaning` into `data/filter`, `kgb-augment` reads that cut, and `kgb-mcp check` checks the
final graph before it's loaded into Neo4j. Semantic metadata comes from the
`croissant-semantics` skill in `.agents/skills/metadata/`, run per source rather than through `kgb-
metadata`. `make demo` only prints stage descriptions.

## Design rules and how work lands

[`CONTRIBUTING.md`](CONTRIBUTING.md) is the one home for the team's design rules, branches and
pull requests, issue and tracking conventions, and what may go in git. Read it before you
branch, commit or open a PR; don't repeat its rules here.
