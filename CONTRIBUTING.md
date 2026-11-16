# Contributing

How changes get into this repo. Coding agents should also read [`AGENTS.md`](AGENTS.md), which
has the commands, the architecture and the rules for each directory.

## Design rules

- Agents decide, code transforms: an LLM may choose a mapping, but the transform itself is code
  that can be re-run to reproduce the graph.
- Every node ID goes through NodeNorm.
- Biolink is fixed, not something the user selects.
- Every stage has strong defaults. The filter is the only stage that asks the user anything,
  and "go with your default" must always work.
- The graph's scope comes from what the user asks for, never from a choice made in advance
  about which sources or edges matter.
- Evaluation stays outside the agents.

## Directories and reviewers

| Directory | Reviewed by |
| --- | --- |
| `agents/metadata/` | @UNC-RENCI-KGAI/metadata |
| `agents/transformation/` | @UNC-RENCI-KGAI/transformation |
| `agents/filter/` | @UNC-RENCI-KGAI/filter |
| `agents/augmentation/` | @UNC-RENCI-KGAI/augmentation |
| `agents/cleaning/` | @UNC-RENCI-KGAI/cleaning |
| `agents/mcp_interface/` | @UNC-RENCI-KGAI/mcp-interface |
| `evaluation/` | @UNC-RENCI-KGAI/evaluation |
| `shared/`, `.github/`, root files | @UNC-RENCI-KGAI/leads |

`.github/CODEOWNERS` maps these, so a pull request that touches a directory automatically asks
that directory's team for review.

## Branches and pull requests

**Nothing lands on `main` except through a pull request.** No direct pushes, no admin bypass,
no "it's just a typo". The pull request is what asks the right team for review and runs CI
before the merge.

```bash
git checkout -b <team>/<short-description>    # e.g. filter/affinity-defaults
# ... edit, commit ...
git push -u origin <team>/<short-description>
gh pr create
```

- Open a pull request early, as a draft if it isn't ready.
- One approval from a code owner merges it. Squash merge; the branch is deleted automatically.
- Touching two directories? Add the `integration` label and request review from both teams.
- `main` must always pass `make test` and `make demo`. CI runs both on every pull request.
- If a stage's CLI changes, update its `README.md` in the same pull request.

## Issues

- Every piece of work is an issue with one `agent: *` or `evaluation` label.
- Something you can't resolve? Label it `open question`, and `blocked` if it stops you.
- Larger pieces of work have a tracking issue. Link each child to it with a task-list row
  (`- [ ] #123`) and as a sub-issue. A pull request's body carries `Closes #<child>` and
  `Part of #<tracking>`. The commands are in [`AGENTS.md`](AGENTS.md), under "Working in the
  repo".
- Work deferred until after the challenge is labelled `post-submission` and grouped in #104.

## Code

- Python 3.12+, managed with `uv`. Add dependencies with `uv add <pkg>`, so `pyproject.toml` and
  `uv.lock` stay in sync.
- Each agent directory is a package with a `cli.py` exposing `main()`, and a `README.md` saying
  how to run it alone. Stages hand records to each other using the types in `shared/schema.py`.
- Every edge carries Biolink provenance: `primary_knowledge_source`, `knowledge_level` and
  `agent_type`, plus aggregators and publications where known. Which file and columns produced
  it goes in the transformation agent's `TransformReport`. The MCP interface answers "why does
  A connect to B?" from these.
- `ruff` for lint and format (`make lint`), `pytest` for tests (`make test`). Small tests on toy
  data are better than none.
- No data files in git. `data/` is ignored except for its README.

## Agent skills

Shared skills go in **`.agents/skills/<category>/<skill-name>/SKILL.md`**. The categories match
the agent packages: `metadata`, `transformation`, `filter`, `augmentation`, `cleaning` and
`mcp_interface`. Keep each skill's scripts, references and assets inside its own directory, for
example `.agents/skills/transformation/biolink-mapping/SKILL.md`.

Start `SKILL.md` with frontmatter containing a `name` that matches the leaf directory
(`biolink-mapping` in this example) and a nonempty `description`. Skill names must be unique
across all categories; category prefixes aren't required. Remove the category's `.gitkeep` when
you add its first skill.

Pi discovers the grouped skills directly. For Claude Code, run the sync script, which creates
flat, relative symlinks in `.claude/skills/` pointing to the originals:

```bash
python3 scripts/sync_claude_skills.py --prune --dry-run   # preview new links and cleanup
python3 scripts/sync_claude_skills.py --prune             # create links, remove broken ones
```

Run the sync after adding, moving, renaming or deleting a skill. Edit the originals in
`.agents/skills/`, not the links in `.claude/skills/`. The script rejects duplicate names and
won't overwrite existing content; unrelated links and real files are left alone.

Both harnesses can preload a skill by its leaf name: Pi subagent frontmatter uses
`skills: biolink-mapping`, and Claude subagent frontmatter uses `skills: [biolink-mapping]`.
After a change, run `/reload` in Pi or restart Claude Code, then check that the skill is found.
[The skills guide](.agents/skills/README.md) has a complete `SKILL.md` example, loader details
and pruning limitations.
