# Metadata agent

**Team:** @UNC-RENCI-KGAI/metadata · **Existing asset:** RoboCroissant

Describe each source file and its columns as Croissant metadata. Purely descriptive; builds no graph.

## Semantics

The `croissant-semantics` skill in [`.agents/skills/metadata/croissant-semantics/`](../../.agents/skills/metadata/croissant-semantics/)
adds what the data means (Biolink classes and associations, identifier prefixes, units, code
values, file dialects) to the Croissant files RoboCroissant produces, with checking scripts. See
its README.

## Run it alone

Run the `croissant-semantics` skill per source; its README says how. `kgb-metadata` is still a
stub that prints "not implemented yet" ([#41](https://github.com/UNC-RENCI-KGAI/KGB-CIA/pull/41)
implements it).

## Input / output

**Input:** a RoboCroissant `croissant.json` per source, kept in `data/cache/croissant/`.

**Output:** per source, `croissant.semantic.json` with `semantics_plan.json` and the check
reports, which `kgb-transform` reads (see `agents/transformation/README.md`).

## Status

The skill works and has been run on the Pharos sources. The graph is frozen at the Nov 9
transformation run, so metadata fixes now go into a report (what, which source, why) rather than
into new bundles.
