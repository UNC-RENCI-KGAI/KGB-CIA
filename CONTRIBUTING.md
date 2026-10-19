# Contributing

Six weeks, twenty people, one repo. These conventions keep us out of each other's way.

## Teams and ownership

| Directory | GitHub Team | Slack emoji |
| --- | --- | --- |
| `agents/metadata/` | @UNC-RENCI-KGAI/metadata | :file_cabinet: |
| `agents/transformation/` | @UNC-RENCI-KGAI/transformation | :phoenix: |
| `agents/filter/` | @UNC-RENCI-KGAI/filter | :scissors: |
| `agents/augmentation/` | @UNC-RENCI-KGAI/augmentation | :open_hands: |
| `agents/cleaning/` | @UNC-RENCI-KGAI/cleaning | :broom: |
| `agents/mcp_interface/` | @UNC-RENCI-KGAI/mcp-interface | :electric_plug: |
| `evaluation/` | @UNC-RENCI-KGAI/evaluation | :bar_chart: |
| `shared/`, `.github/`, root files | @UNC-RENCI-KGAI/leads | |

Current members: [`docs/teams.md`](docs/teams.md). `.github/CODEOWNERS` maps these. A PR that touches your team's directory automatically asks your
team for review; nobody else is blocked on it.

## Branches and pull requests

- Branch from `main`, name it `<team>/<short-description>`, e.g. `filter/affinity-defaults`.
- Open a PR early, as a draft if it is not ready. Link the issue with `Closes #N`.
- One approval from a code owner merges it. Squash merge; the branch is deleted automatically.
- Touching two directories? Add the `integration` label and request review from both teams.
- `main` must always pass `make test` and `make demo`. CI runs both on every PR.

## Issues and the GitHub Project

- Every piece of work is an issue with one `agent: *` or `evaluation` label and a milestone
  (`Progress report (Oct 27)` or `Final submission (Nov 17)`).
- Each team has a **Tracking** issue with its Oct 27 checklist. Break it into small issues during
  the Code Bursts and tick the boxes as they close.
- The [org Project board](https://github.com/orgs/UNC-RENCI-KGAI/projects/1) (Status: Backlog, In progress, Review, Done) is the single view of who is
  doing what. Team leads move their cards before each Code Burst and post a two-line status in
  Slack.
- Something you cannot resolve? Label it `open question` and, if it stops you, `blocked`.

## Code

- Python 3.11+, managed with `uv`. Add dependencies with `uv add <pkg>` so `pyproject.toml` and
  `uv.lock` stay in sync.
- Each agent directory is a package with a `cli.py` exposing `main()` and a `README.md` saying
  how to run it alone. Inputs and outputs between agents use the record types in `shared/schema.py`.
- Keep provenance on every edge (source file, columns, evidence). Explainability is 20% of the
  score and the MCP interface has to answer "why does A connect to B?".
- `ruff` for lint and format (`make lint`), `pytest` for tests (`make test`). Small tests on toy
  data beat none.
- No data files in git. `data/` is ignored except for its README.
