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

**Output:** `data/filter/<focus name>/` with `nodes.jsonl`, `edges.jsonl` (records unchanged)
and `focus_report.json`: the request, each seed rule with how many nodes it matched and
examples, the subgraph's size, and its edges by source and predicate. With no focus,
`data/filter/whole/` links to the input graph.

## How a subgraph is cut

Seeds are the union of the focus file's seed rules (exact ids; a word in node names, by category
and taxon; members or subclasses of given nodes; everything one source touches). From the seeds
it reaches out `hops` steps (default 1), not expanding through hubs with more than
`max_degree` edges (default 2000). The subgraph's edges are every edge between its nodes,
optionally only from some sources or with some predicates. The format is in the
`focus-subgraph` skill's reference.

## Status

Works and is tested (`tests/test_filter.py`). On a 3.1M-edge trial graph (cleaned DrugCentral,
HPA and orthologs from the Oct 30 run), "human kinases and the drugs that target them" gave 689
seed proteins and an 18,106-edge subgraph in 11 seconds. Each hop is a pass over the edges, so
expect minutes per request on the full graph.
