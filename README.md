# KGB-CIA: Knowledge Graph Based Creation of Informed Agents

In plain terms: we're building a pipeline of AI agents that reads raw drug-target data (from
[Pharos](https://pharos.nih.gov/) / TCRD), figures out on its own what the data means and how it
fits together, and turns it into a proper knowledge graph — then lets other agents query that
graph. No step requires a human to hand-map columns or write transform code; a person only
steps in briefly, with sensible defaults, to say which parts of the graph they want.

This is our team's entry for the **Bio-Agent Knowledge Graph Construction Challenge** (Structured
Data Integration and Link Prediction track). The output graph follows the
[Biolink](https://biolink.github.io/biolink-model/) model, and it's scored by GNN link prediction
against a Pharos baseline.

Key dates: progress report **Oct 27, 2026**, final submission **Nov 17, 2026**.

## Status (as of Nov 10, 2026)

**The graph is frozen** at the Nov 9 transformation run. Changes we'd like to make to the graph or
the metadata now go into a report (what, which source, why) instead of code, so the remaining
time goes to the user-facing steps, the demo and the submission.

| Stage | On `main` | In progress |
| --- | --- | --- |
| 1. Metadata (`agents/metadata/`) | The `croissant-semantics` skill adds Biolink semantics to RoboCroissant's Croissant files, run per source. `kgb-metadata` is still a stub. | [#41](https://github.com/UNC-RENCI-KGAI/KGB-CIA/pull/41): `kgb-metadata` |
| 2. Transformation (`agents/transformation/`) | **Working.** `kgb-transform` generates an ORION parser per source, builds each one, and merges them into one graph with ORION's merge rules. | [#42](https://github.com/UNC-RENCI-KGAI/KGB-CIA/pull/42): skip unchanged builds |
| 3. Filter (`agents/filter/`) | Stub. | [#47](https://github.com/UNC-RENCI-KGAI/KGB-CIA/pull/47): describe the graph; [#50](https://github.com/UNC-RENCI-KGAI/KGB-CIA/pull/50): cut the subgraph a user asks for |
| 4. Augmentation (`agents/augmentation/`) | Stub. | [#22](https://github.com/UNC-RENCI-KGAI/KGB-CIA/pull/22): PubMed evidence agent; [#52](https://github.com/UNC-RENCI-KGAI/KGB-CIA/pull/52): shared PubMed search and cache |
| 5. Cleaning (`agents/cleaning/`) | **Working.** `kgb-clean` drops edges that can't be right (no provenance, unresolved or missing nodes, drop lists), flags suspicious ones (Biolink type violations, rare category combinations, opposing predicates) and reports both. | [#49](https://github.com/UNC-RENCI-KGAI/KGB-CIA/pull/49): evidence check |
| 6. MCP interface (`agents/mcp_interface/`) | Stub. | A Neo4j-backed MCP server on the `mcp-interface/neo4j-server` branch, being deployed on AWS for the hosted demo |
| Evaluation (`evaluation/`) | Not started. | [#54](https://github.com/UNC-RENCI-KGAI/KGB-CIA/issues/54): link-prediction protocol |

The order of the stages is still being settled:
[#23](https://github.com/UNC-RENCI-KGAI/KGB-CIA/issues/23) (filter before or after
transformation) and [#24](https://github.com/UNC-RENCI-KGAI/KGB-CIA/issues/24) (cleaning before or
after augmentation). The diagram below is the order in `STAGES` today.

## Pipeline

```
Pharos / TCRD files
        |
        v
1. Metadata agent        agents/metadata/        Croissant metadata per file (RoboCroissant), plus Biolink semantics
        |
        v
2. Transformation agent  agents/transformation/  one ORION parser per source, built and NodeNorm'd, then merged into one graph
        |
        v
3. Filter                agents/filter/          the subgraph the user asks for; with no request, the whole graph
        |
        v
4. Augmentation agent    agents/augmentation/    PubMed evidence for edges, and more specific edges it supports
        |
        v
5. Cleaning agent        agents/cleaning/        drops edges that can't be right or that an evidence check rejects
        |
        v
6. MCP interface         agents/mcp_interface/   lets any local agent query the graph (OKN MCP pattern)

Evaluation pipeline      evaluation/             GNN link prediction vs. Pharos baseline (not an agent)
Shared code              shared/                 record schema, NodeNorm client, pipeline runner
```

[`docs/KG_AI_plandraft.png`](docs/KG_AI_plandraft.png) (source:
[`docs/pipeline_diagram.mmd`](docs/pipeline_diagram.mmd)) is the Oct 26 version of this diagram,
with each stage's planned input and output files; parts of it are out of date.

Design rules agreed by the team: agents decide, code transforms (reproducible); every node goes
through NodeNorm; Biolink is fixed, not user-selectable; strong defaults everywhere, and the
graph's scope comes from what the user asks for, not a choice we make in advance; evaluation stays
outside the agent. The original plan, with Chris Bizon's guidance:
[`docs/agent_plan.md`](docs/agent_plan.md).

## Quick start

```bash
git clone https://github.com/UNC-RENCI-KGAI/KGB-CIA.git
cd KGB-CIA
make setup        # creates .venv with uv and installs the package in editable mode
make test         # lint + unit tests
make demo         # dry run: prints what each stage does, runs nothing
```

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). `make setup` falls back to `pip` if
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
GitHub Project conventions.

## Agent skills

Put shared skills in **`.agents/skills/<category>/<skill-name>/SKILL.md`**. Categories
match the agent packages: `metadata`, `transformation`, `filter`, `augmentation`,
`cleaning`, and `mcp_interface`. Keep each skill's supporting scripts, references,
and assets inside its directory.

For example:

```text
.agents/skills/transformation/biolink-mapping/SKILL.md
```

Start `SKILL.md` with frontmatter containing a `name` matching the leaf directory
(`biolink-mapping` in this example) and a nonempty `description`. Skill names must be
unique across all categories; category prefixes are not required. Remove the
category's `.gitkeep` when adding its first skill.

Pi discovers the grouped skills directly. For Claude Code, run the sync script to
create flat, relative symlinks in `.claude/skills/` pointing to the shared originals:

```bash
# Preview new links and cleanup of broken links
python3 scripts/sync_claude_skills.py --prune --dry-run

# Create links and remove broken links targeting this project's shared skills
python3 scripts/sync_claude_skills.py --prune
```

Run sync after adding, moving, renaming, or deleting skills. Edit the originals in
`.agents/skills/`, not separate copies in `.claude/skills/`. The script rejects
duplicate names and refuses to overwrite existing content; unrelated links and
real files/directories are preserved.

Both harnesses can preload a skill by its leaf name: Pi subagent frontmatter uses
`skills: biolink-mapping`; Claude subagent frontmatter uses `skills: [biolink-mapping]`.
Run `/reload` in Pi and restart Claude Code after changes, then verify discovery.
See [the shared skills guide](.agents/skills/README.md) for a complete `SKILL.md`
example, loader details, and pruning limitations.

## Data

No data lives in this repo. TCRD dumps, Pharos exports, and generated graphs go under `data/`
locally (gitignored) or on the shared RENCI drive. Record the download date and version in
`data/README.md` when you add a source.

## License

MIT. See [`LICENSE`](LICENSE).
