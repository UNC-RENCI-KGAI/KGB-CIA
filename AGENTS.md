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
Deadlines: progress report Oct 27, 2026; final submission Nov 17, 2026. The full plan and the
reasoning behind each stage is in [`docs/agent_plan.md`](docs/agent_plan.md).

## Commands

```bash
make setup     # uv sync --dev (falls back to a pip venv if uv is missing)
make lint      # ruff check + ruff format --check
make format    # ruff --fix + ruff format
make test      # lint, then pytest -q
make demo      # python -m shared.pipeline --dry-run; must always pass on main

uv run pytest -q tests/test_smoke.py::test_schema_round_trip   # one test
uv run python -m shared.pipeline --dry-run --only filter       # describe one stage
uv run kgb-filter --input <in> --output <out>                  # run one stage alone
```

CI (`.github/workflows/ci.yml`) runs exactly `make test` plus `make demo` on Python 3.12. Ruff
line length is 100; rules `E F I W UP B`. Add dependencies with `uv add <pkg>` so `uv.lock`
stays in sync.

## Architecture

- **Stages are packages under `agents/`, run in the order listed in `STAGES` in
  `shared/pipeline.py`:** metadata → transformation → cleaning → filter → augmentation →
  final_cleaning → mcp_interface (settled in #23 and #24). The runner imports each
  `agents.<stage>.cli` module, prints its `DESCRIPTION`, and (without `--dry-run`) calls `main`
  with that stage's arguments from `STAGES`; a nonzero return stops the pipeline. The two
  cleaning passes are the same `kgb-clean`: the first cleans the merged graph, the last applies
  the evidence check's drop lists (`data/cleaning/drop_lists`) to augmentation's output and
  writes `data/final`, which the MCP interface is then given with `--input` (on its own,
  `kgb-mcp` would fall back to `data/cleaning`).
- **Each stage is also a console script** (`kgb-metadata`, `kgb-transform`, `kgb-filter`,
  `kgb-augment`, `kgb-clean`, `kgb-mcp`, `kgb-pipeline`) declared in `pyproject.toml`.
- **Handoffs between stages use the pydantic records in `shared/schema.py`** (`Node`, `Edge`,
  `Graph`, `TransformReport`). `Node` and `Edge` are flat Biolink/KGX records shaped like
  ORION's `nodes.jsonl` and `edges.jsonl`. Every `Edge` must carry its Biolink provenance
  (`primary_knowledge_source`, `knowledge_level`, `agent_type`; plus aggregators and
  publications where known): explainability is 20% of the score, and the MCP interface has to
  answer "why does A connect to B?" from it. Which file and columns produced each node or edge
  is not Biolink, so it lives in the transformation agent's `TransformReport`, which points
  into the source's Croissant metadata.
- **Every node ID goes through NodeNorm** via `shared/nodenorm.py`.
- **`evaluation/` is deliberately not a stage** and is not in `STAGES`.

`kgb-transform` is the only stage command implemented; the other stages' `main` are stubs that
print "not implemented yet". Semantic metadata comes from the `croissant-semantics` skill in
`.agents/skills/metadata/`, run per source rather than through `kgb-metadata`. `make demo` only
prints stage descriptions. The README mentions toy data in `tests/fixtures`; that directory does
not exist yet.

## Design rules agreed by the team

- Agents decide, code transforms: an LLM may choose a mapping, but the transform itself is code
  that can be re-run to reproduce the graph.
- Biolink is fixed, not user-selectable.
- Strong defaults everywhere; the filter stage is the only place that asks the user anything,
  and "go with your default" must always work.
- Evaluation stays outside the agent.

## Working in the repo

- Never push to `main`; every change goes through a PR. Branch names are
  `<team>/<short-description>`, e.g. `filter/affinity-defaults`.
- Each directory is owned by one GitHub Team via `.github/CODEOWNERS` (team list in
  `CONTRIBUTING.md`, members in `docs/teams.md`). Keep a change inside one team's directory
  where you can; a PR that touches two needs the `integration` label and review from both.
  `shared/`, `.github/` and root files belong to the leads.
- The PR template's checklist: `make test` passes, new edges carry provenance, and the agent's
  `README.md` is updated if its CLI changed.
- No data in git. Source dumps and generated graphs go in `data/` (gitignored except its
  README) or on the shared RENCI drive; record each source's version and download date in
  `data/README.md`. `.gitignore` also blocks `*.jsonl`, `*.parquet`, `*.db`, `outputs/` and
  `runs/` anywhere in the tree.
