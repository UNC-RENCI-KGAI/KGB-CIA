---
name: describe-graph
description: Describe a built knowledge graph in plain terms (nodes, edges, sources, kinds of things and connections, what didn't build) from ORION's metadata, with an approachable HTML overview; and when a user brings their own source, show how much of it connects to the pre-built graph and recommend which sources to focus on. Use after transformation, merging or cleaning, or whenever someone asks what is in the graph.
---

# Describe graph

Tell a scientist what is in their knowledge graph without making them read KGX files. The
numbers come from ORION's own metadata (each source's `*.meta.json` and build QC file, the merge
metadata, the cleaning report), so describing even a 100M-edge graph takes seconds.

## Run it

```bash
python3 .agents/skills/filter/describe-graph/scripts/describe_graph.py data/transformation/graphs \
    --merged data/transformation/merged --cleaned data/cleaning --output data/describe
```

`GRAPHS` is the directory of per-source ORION workspaces (`kgb-transform`'s `OUTPUT/graphs`).
`--merged` and `--cleaned` are optional; give whichever exist. Totals come from the most finished
one (cleaned, then merged, then the sum over sources, which counts shared nodes twice and says
so).

When the user has added their own source, built through the same metadata and transformation
steps, name it:

```bash
python3 .agents/skills/filter/describe-graph/scripts/describe_graph.py data/transformation/graphs \
    --user Kgbcia_<their_source> --output data/describe
```

This reads each source's node ids (only the ones NodeNorm resolved) to find which sources share
the user's nodes. `--overlap` does the same without a user source. Expect a minute or so on the
full graph.

Outputs in `--output`:

- `description.md`: read this, then retell it.
- `description.json`: every number, for other tools.
- `overview.html`: a self-contained page with headline numbers, a map of the kinds of things
  and how they connect, edges per source, and with `--user`, how the user's data connects and
  how the sources overlap. Point the user to it; it works offline and in dark mode.

## Retell it for a non-specialist

Lead with three or four sentences, not the tables: how big the graph is, what kinds of things
it holds and the most common connections between them, and where it came from. Use the words
the page uses (things, connections, sources) and keep Biolink names for when they're asked
for. Then say what needs attention, in order of impact:

- sources that didn't build, and where each stopped (download, parsing, normalization);
- sources with many unresolved nodes: those nodes can't join anything else and are dropped
  by cleaning;
- a low share of connections citing a publication, if explainability matters to them.

With a user source, give the connected share first ("53% of your 136,235 genes are already in
the graph"), then the recommended sources in order, with what each adds. The recommendation is
a greedy cover over shared nodes: each step picks the source that connects the most of the
user's not-yet-connected nodes, up to five, stopping when a source would add under 1%. It is
the default focus for the user's graph; carry on with it unless the user picks otherwise. If
the connected share is low (under about 20%), say so plainly and suggest why: often the user's
ids are in a namespace NodeNorm doesn't resolve, which the metadata step can fix.

## Limits

- Overlap is shared nodes, not paths: two sources that connect only through a third aren't
  counted as overlapping.
- Per-source counts are before merging. Category and predicate totals add up sources, so an
  edge two sources both assert is counted twice there; graph totals from the merge or cleaning
  are exact.
- A source whose `normalized_nodes.jsonl` is missing is left out of the overlap, with a warning.
