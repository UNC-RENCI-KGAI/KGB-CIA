# Filter agent

Stage 4, and the one place the pipeline asks the user anything. We build and clean one full
graph; the part a user works with comes from what they ask for, not from a scope chosen in
advance. With no request, the user gets the whole graph.

## How it works

Two skills in [`.agents/skills/filter/`](../../.agents/skills/filter/) do the agent's part:

- **`describe-graph`** gives a plain-language overview of what's in the graph. Given the user's
  own data source, it also measures how much of it the graph's sources already cover, and
  recommends which sources to focus on. That recommendation is the default.
- **`focus-subgraph`** turns a request ("human kinases and the drugs that target them",
  "Alzheimer's disease", "my screening data") into a focus file: rules for the nodes the request
  is about, and how far to reach out from them. `find_nodes.py` looks up the IDs.

`kgb-filter` then cuts that subgraph from the full graph. The cut is plain code, so the same
focus file always gives the same subgraph.

## Run it

```bash
uv run kgb-filter                                   # no request: the whole graph
uv run kgb-filter --focus data/filter/<name>.json   # the subgraph for one request
```

`--input` and `--output` are only needed for a graph outside the standard layout.

## Input / output

**Input:** a directory with `nodes.jsonl` and `edges.jsonl` in KGX format. That's the cleaned
graph (`data/cleaning`) if cleaning has run, otherwise the merged one
(`data/transformation/merged`).

**Output:** `data/filter/`, where augmentation reads it:

- `nodes.jsonl` and `edges.jsonl`, with records unchanged.
- `focus_report.json`: the request, each seed rule with how many nodes it matched and examples,
  the subgraph's size, and its edges by source and predicate.

With no focus file, `nodes.jsonl` and `edges.jsonl` link to the input graph. Each run replaces
the last. To keep a cut beside the current one, pass `--output data/filter/<name>` and give
augmentation the same folder with `--input`.

## How a subgraph is cut

The seeds are the union of the focus file's seed rules:
- exact IDs;
- a word in node names, by category and taxon;
- members or subclasses of given nodes;
- everything one source touches.

From the seeds the cut reaches out `hops` steps (default 1). It doesn't expand through hubs with
more than `max_degree` edges (default 2,000), except for the seeds themselves, which always
expand. The subgraph keeps every edge between its nodes, optionally only from some sources or
with some predicates. The focus file format is in the `focus-subgraph` skill's reference.

For example, on the full graph (1.2M nodes, 12M edges), "the 635 Pharos kinases and their drugs"
matched 633 seed proteins and gave 4,675 drug–target edges.

## Limitations

- A request on the full graph takes 2 to 3 minutes, because each hop is a pass over every edge.
