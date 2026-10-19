# KGB-CIA: Knowledge Graph Based Creation of Informed Agents

Team entry for the **Bio-Agent Knowledge Graph Construction Challenge** (Structured Data
Integration and Link Prediction track). We are building an agent that constructs a
[Biolink](https://biolink.github.io/biolink-model/) labeled property graph from
[Pharos](https://pharos.nih.gov/) / TCRD data, then exposes it to other agents through an MCP
interface. Evaluation is GNN link prediction against the Pharos baseline.

Key dates: progress report **Oct 27, 2026**, final submission **Nov 17, 2026**.

## Pipeline

```
Pharos / TCRD files
        |
        v
1. Metadata agent        agents/metadata/        Croissant metadata per file and column (RoboCroissant)
        |
        v
2. Transformation agent  agents/transformation/  columns -> Biolink classes and predicates; writes and runs code
        |
        v
3. Filter & integration  agents/filter/          user choices with strong defaults; one connected baseline graph
        |
        v
4. Augmentation agent    agents/augmentation/    extra datasets and PubMed evidence
        |
        v
5. Cleaning agent        agents/cleaning/        conflict resolution, pruning, final graph
        |
        v
6. MCP interface         agents/mcp_interface/   lets any local agent query the graph (OKN MCP pattern)

Evaluation pipeline      evaluation/             GNN link prediction vs. Pharos baseline (not an agent)
Shared code              shared/                 edge schema, NodeNorm client, Biolink helpers, I/O
```

Design rules agreed by the team: agents decide, code transforms (reproducible); every node goes
through NodeNorm; Biolink is fixed, not user-selectable; strong defaults everywhere, with user
input only at the filter step; evaluation stays outside the agent. Full plan with Chris Bizon's
guidance: [`docs/agent_plan.md`](docs/agent_plan.md).

## Quick start

```bash
git clone https://github.com/UNC-RENCI-KGAI/KGB-CIA.git
cd KGB-CIA
make setup        # creates .venv with uv and installs the package in editable mode
make test         # lint + unit tests
make demo         # dry run of every stage on the toy data in tests/fixtures
```

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). `make setup` falls back to `pip` if
uv is not installed.

## Working on the repo

**Never push to `main`. Every change, however small, goes through a pull request.** That is what
routes the review to the right team through CODEOWNERS and runs the CI gate. Admins can bypass
the protection, but should not.

```bash
git checkout -b <team>/<short-description>
# ... edit, commit ...
git push -u origin <team>/<short-description>
gh pr create
```

One monorepo, one directory per subagent, one GitHub Team per directory. Each team owns its
directory through `.github/CODEOWNERS` and reviews its own pull requests. Cross-directory changes
get the `integration` label. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the branch, review, and
GitHub Project conventions, and the
[tracking issues](https://github.com/UNC-RENCI-KGAI/KGB-CIA/issues?q=is%3Aissue+Tracking) for
each team's Oct 27 checklist.

## Data

No data lives in this repo. TCRD dumps, Pharos exports, and generated graphs go under `data/`
locally (gitignored) or on the shared RENCI drive. Record the download date and version in
`data/README.md` when you add a source.

## License

MIT. See [`LICENSE`](LICENSE).
