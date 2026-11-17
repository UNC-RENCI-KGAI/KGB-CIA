# Transformation agent

Stage 2. Turns each source into graph nodes and edges, then merges every source into one graph.
For each source it generates an [ORION](https://github.com/RobokopU24/ORION) parser from the
metadata stage's plan and runs it. ORION normalizes every identifier with NodeNorm and writes
[KGX](https://github.com/biolink/kgx) files.

Transformation only does what the metadata says. It never guesses a value the metadata doesn't
give, including the source's own identity (its `infores` id): that comes from the metadata, or,
when one source is built on its own with the generate-graph skill's scripts, from their
`--infores` option (`kgb-transform` has no such flag). A source with no identity is reported as `needs_infores` and
skipped, not run under an invented id. An id the metadata marks as proposed (not yet in the
infores registry) is used, and listed so it can be registered.

## Run it

```bash
uv run kgb-transform                                  # finds the metadata under data/
uv run kgb-transform --input data/metadata --output data/transformation
```

This generates a parser for every source under `--input`, runs each through ORION, merges the
results, and writes `transformation_report.json`. With no `--input`, it uses `data/metadata` if
there is a `croissant.semantic.json*` anywhere under it, otherwise the one directory under
`data/` that holds such files. If more than one does, it stops and asks for `--input` rather than guess.

To add a single new source, or to test parsing on its own, use the
[generate-graph skill](../../.agents/skills/transformation/generate-graph/README.md) directly.
Its [workflow reference](../../.agents/skills/transformation/generate-graph/reference/orion_workflow.md)
covers the commands, local file bindings and runtime requirements. `kgb-transform` runs the same
workflow for every source, unattended.

## Input / output

**Input:** per source, a directory holding `croissant.semantic.json` (or `.gz`) and
`semantics_plan.json`, which is required because it carries the Biolink version ORION
normalizes against. The metadata stage's other reports aren't read. An optional
`source_files.json` beside them binds the record sets to local files instead of downloading
them (see the [workflow reference](../../.agents/skills/transformation/generate-graph/reference/orion_workflow.md)).

**Output**, under `--output` (default `data/transformation`):

- `parsers/<id>/`: the ORION parser generated for each source.
- `graphs/<id>/`: ORION's workspace for each source. It holds the KGX `normalized_nodes.jsonl`
  and `normalized_edges.jsonl` (in the latest stable `parsed_*/normalized_*/`), a
  `transform_report.json` saying which file, record set and columns produced each node and
  edge, and `diagnostics.jsonl` with the rows it couldn't use and why (the first 100 examples of each field and reason, then a count).
- `merged/`: one graph built from every source, using ORION's merge rules, as `nodes.jsonl` and
  `edges.jsonl`.
- `transformation_report.json`: for whoever maintains the metadata. Every source's status, the
  parser generation errors, each ORION run's exit code (and error, if it failed), and the merge
  result.

Every edge carries Biolink provenance (`primary_knowledge_source`, `knowledge_level`,
`agent_type`) and `kgbcia_source_records`, which points into the `transform_report.json`. That's
how the MCP server traces an edge back to the row it came from.

## Limitations

- ORION needs Python 3.12+. Each source runs in its own `uv run --script` process.
- Not every file format has a reader yet. Each source's report says which record sets it
  couldn't read.
- The merge needs about 500 bytes of disk per merged edge, for the output and again for temporary
  files (about 12 GB for the Nov 9 graph's 11.7M edges), on top of the per-source graphs it reads
  and several GB of memory.
- Rerunning with changed local file bindings or a different `--infores` needs a fresh
  `--output` directory, because ORION's cache doesn't track those inputs.
- An identifier NodeNorm doesn't know isn't a parser failure. Cleaning reports those nodes.
