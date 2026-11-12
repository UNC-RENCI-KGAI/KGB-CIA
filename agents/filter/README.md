# Filter agent

**Team:** @UNC-RENCI-KGAI/filter

We build and clean one full graph. The part a user works with comes from what they ask, not
from a scope we chose in advance. The agent turns a request ("human kinases and the drugs that
target them", "Alzheimer's disease", "my screening data") into a focus file: seed rules for the
nodes the request is about, and how far to reach out from them. `kgb-filter` then cuts that
subgraph out of the full graph reproducibly. With no request, the user's graph is the whole
graph.

The agent side is two skills in `.agents/skills/filter/`: `focus-subgraph` (request to focus
file, with `find_nodes.py` to look up seed ids) and `describe-graph` (what is in the graph, and
with a user's own source, which other sources connect to it).

## Run it alone

```bash
uv run kgb-filter                                   # no request: the whole graph
uv run kgb-filter --focus data/filter/<name>.json   # the subgraph for one request
```

`--input` and `--output` are only needed for a graph outside the standard layout.

## Input / output

**Input:** a directory with `nodes.jsonl` and `edges.jsonl` in ORION KGX format: the cleaned
graph (`data/cleaning`) when cleaning has run, else the merged one
(`data/transformation/merged`). So the filter runs on the cleaned full graph, once per request.

**Output:** `data/filter/`, where augmentation reads it, with `nodes.jsonl`, `edges.jsonl`
(records unchanged) and `focus_report.json`: the request, each seed rule with how many nodes it
matched and examples, the subgraph's size, and its edges by source and predicate. With no focus,
`nodes.jsonl` and `edges.jsonl` link to the input graph. Each run replaces the last; to keep a
cut beside the current one (a demo subgraph, say), pass `--output data/filter/<name>` and give
augmentation the same folder with `--input`.

## How a subgraph is cut

Seeds are the union of the focus file's seed rules (exact ids; a word in node names, by category
and taxon; members or subclasses of given nodes; everything one source touches). From the seeds
it reaches out `hops` steps (default 1), not expanding through hubs with more than
`max_degree` edges (default 2000), except seeds, which always expand. The subgraph's edges
are every edge between its nodes, optionally only from some sources or with some
predicates. The format is in the `focus-subgraph` skill's reference.

## Status

`kgb-filter` works and is tested (`tests/test_filter.py`). On the frozen merged graph (1.2M
nodes, 12M edges) a request takes 2 to 3 minutes: each hop is a pass over the edges. For
example, the 635 Pharos kinases and their drugs gave 633 seed proteins and 4,675 drug-target
edges. The `describe-graph` skill (`.agents/skills/filter/describe-graph/`) does the first half
of the filter's job: given the user's own source, it measures how much of it the pre-built
sources already share and recommends which sources to focus on, with that list as the default.
