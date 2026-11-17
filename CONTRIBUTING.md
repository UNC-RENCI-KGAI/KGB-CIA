# Contributing

How changes get into this repo. Coding agents should also read [`AGENTS.md`](AGENTS.md), which
has the commands, the architecture and the rules for each directory.

## Design rules

- Agents decide, code transforms: an LLM may choose a mapping, but the transform itself is code
  that can be re-run to reproduce the graph.
- Every node ID goes through NodeNorm (ORION does it in transformation; other code uses
  `shared/nodenorm.py`).
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

**Nothing lands on `main` except through a pull request**, not even a typo fix. The pull
request is what asks the right team for review and runs CI before the merge.

```bash
git checkout -b <team>/<short-description>    # e.g. filter/affinity-defaults
# ... edit, commit ...
git push -u origin <team>/<short-description>
gh pr create
```

- Open a pull request early, as a draft if it isn't ready.
- One approval from a code owner merges it. Squash merge; the branch is deleted automatically.
- Touching two directories? Keep a change inside one where you can; if it must touch two, add
  the `integration` label and request review from both teams.
- `main` must always pass `make test` and `make demo`. CI runs both on every pull request.
- The PR template's checklist: `make test` passes, new edges carry provenance, and the stage's
  `README.md` is updated if its CLI changed (in the same pull request).
- Fixes are welcome on `main` at any stage, metadata and transformation included, but the
  submission graph stays the Nov 9, 2026 transformation run. A fix that would change the graph
  is still recorded in a report (what, which source, why), so the submission can say what it
  doesn't contain; [`docs/known_limitations.md`](docs/known_limitations.md) lists them.

## Issues

- Every piece of work is an issue with one `agent: *` or `evaluation` label.
- Something you can't resolve? Label it `open question`, and `blocked` if it stops you.
- Larger pieces of work have a tracking issue. It lists its children as a task list, one per
  line (`- [ ] #123`), which gives it a progress bar and each child a "Tracked by" link. Attach
  each child as a sub-issue too, so the tree shows in the sidebar and on the Project board. From
  the CLI, with the child's id (not its number):

  ```sh
  id=$(gh api repos/UNC-RENCI-KGAI/KGB-CIA/issues/<child> --jq .id)
  gh api -X POST repos/UNC-RENCI-KGAI/KGB-CIA/issues/<parent>/sub_issues -F sub_issue_id="$id"
  ```

- A pull request's body carries `Closes #<child>` (the child closes on merge) and
  `Part of #<tracking>` (the PR shows in the tracking issue's timeline). A finding with no code
  change yet, such as a defect from a run, stays a row in the tracking issue until someone opens
  a child for it.
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
- No data in git. Source dumps and generated graphs go in `data/` (ignored except for its
  README) or on a shared drive; record each source's version and download date in
  `data/README.md`. `.gitignore` also blocks `*.jsonl`, `*.parquet`, `*.db`, `outputs/` and
  `runs/` anywhere in the tree.

## Agent skills

Skills live in `.agents/skills/<category>/<skill-name>/SKILL.md`, one category per agent
package, with each skill's scripts, references and tests inside its own folder. The `name` in
`SKILL.md`'s frontmatter must match the folder and be unique across categories.

Pi finds them directly. For Claude Code, run `python3 scripts/sync_claude_skills.py --prune`
after adding, moving or removing a skill; it links them into `.claude/skills/`. Edit the
originals, not the links. [The skills guide](.agents/skills/README.md) has a full example and
the details.
