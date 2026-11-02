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

## Status (as of Oct 26, 2026)

This is a progress-report checkpoint, not a finished pipeline. Where things stand:

| Stage | Status |
| --- | --- |
| 1. Metadata (`agents/metadata/`) | **Working, pending merge.** Builds on RoboCroissant; the `croissant-semantics` skill adds Biolink semantics on top. Reviewed and fixed; [PR #17](https://github.com/UNC-RENCI-KGAI/KGB-CIA/pull/17) is open, blocked on a stale changes-requested review. |
| 2. Transformation (`agents/transformation/`) | **In progress.** Next piece being built — turns the metadata into actual graph nodes and edges. |
| ORION merge | Existing tool, not a new agent. Merges the per-source files Transformation produces, after NodeNorm. |
| 3. Filter & integration (`agents/filter/`) | Planned, not started. Out of scope for this checkpoint. |
| 4. Augmentation (`agents/augmentation/`) | Planned, not started. Out of scope for this checkpoint. |
| 5. Cleaning (`agents/cleaning/`) | Planned, not started. Out of scope for this checkpoint. |
| 6. MCP interface (`agents/mcp_interface/`) | Planned, not started. Out of scope for this checkpoint. |

Goal for this checkpoint: stages 1–2 working end to end, so a person can point the pipeline at a
real Pharos/TCRD source and get back a graph — filtering, augmentation, and cleaning come later.
Two design questions are still open and tracked as issues:
[#23](https://github.com/UNC-RENCI-KGAI/KGB-CIA/issues/23) (does Filter run before or after
Transformation?) and [#24](https://github.com/UNC-RENCI-KGAI/KGB-CIA/issues/24) (does Cleaning run
before, after, or both around Augmentation?).

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
   ORION merge           (not an agent)           merges per-source, NodeNorm'd files
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

A more detailed diagram, with each stage's exact input/output files, is in
[`docs/KG_AI_plandraft.png`](docs/KG_AI_plandraft.png) (source: [`docs/pipeline_diagram.mmd`](docs/pipeline_diagram.mmd)).

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
