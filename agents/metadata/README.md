# Metadata agent

**Team:** @UNC-RENCI-KGAI/metadata · **Existing asset:** RoboCroissant

Describe each source file and its columns as Croissant metadata. Purely descriptive; builds no graph.

## Semantics

The `croissant-semantics` skill in [`.agents/skills/metadata/croissant-semantics/`](../../.agents/skills/metadata/croissant-semantics/)
adds what the data means (Biolink classes and associations, identifier prefixes, units, code
values, file dialects) to the Croissant files RoboCroissant produces, with checking scripts. See
its README.

## Run it alone

```bash
uv run kgb-metadata                                   # data/croissants -> data/metadata
uv run kgb-metadata --input <croissants> --output <bundles> --plans <earlier bundles>
```

Rerun it with the same `--output`: a source is skipped when its croissant, plan, maintainer
answers and the skill's scripts all match its last passing run. Change any of them and only that
source runs again. `--force` runs every source.

## Input / output

`--input` holds one directory per source with `croissant.json` or `croissant.json.gz`, and
optionally `semantics_answers.json`. RoboCroissant exports may need `prepare.py` first.

Each source's bundle in `--output` has `croissant.semantic.json`, `semantics_plan.json`,
`check_report.md` and `semantics_state.json`, the inputs of its last run. `kgb-transform --input`
reads this directory. A source with no plan takes one from `--plans`.

`metadata_report.json` gives each source a status:

| Status | Meaning |
|---|---|
| `unchanged` | Nothing ran |
| `passed` | Plan applied, check passed, no agent needed |
| `check_errors` | Plan applied and bundle kept, but the check found errors; it runs again next time |
| `needs_agent` | No plan, or the plan no longer fits; follow the `croissant-semantics` skill for this source, writing the plan to its bundle, then rerun |

`unplanned_layouts` lists layouts the plan neither matches nor names in `skipped`. They don't stop
a run. `not_in_input` lists bundles with no croissant in `--input`, which transformation would
still build.

This stage doesn't start an agent itself, so writing a plan is still a separate step.

## Status

See the tracking issue for this team on GitHub. Replace this section with what works, what
doesn't, and how to try it.
