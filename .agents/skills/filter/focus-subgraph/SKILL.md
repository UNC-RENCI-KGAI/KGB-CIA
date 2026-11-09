---
name: focus-subgraph
description: Turn what a user asks for ("kinases and the drugs that hit them", "Alzheimer's disease", "my dataset") into a focus file of seed nodes, and cut that subgraph out of the full knowledge graph with kgb-filter. Use when a user wants to work with part of the graph; with no request, the whole graph is theirs.
---

# Focus subgraph

We build and clean one full graph. What a user works with is decided by what they ask, not by
us. You turn their request into a **focus file**: rules that pick the nodes the request is about
(the seeds), and how far to reach out from them. `kgb-filter` then cuts that subgraph out the
same way every time, and records the request with it.

If the user asks for nothing in particular, don't write a focus file: `kgb-filter` with no
`--focus` gives them the whole graph.

## 1. Find the seeds

Read the request for the things it names: genes or proteins, a family of them, a disease, a
drug, a pathway, or the user's own dataset. Find each in the graph with `find_nodes.py`:

```bash
S=.agents/skills/filter/focus-subgraph/scripts
python3 $S/find_nodes.py "kinase" --category biolink:Protein
python3 $S/find_nodes.py --around MONDO:0004975
```

- `find_nodes.py TEXT` lists nodes whose name contains TEXT as a word, counted by category. It's
  the same match a `name` rule makes, so what it finds is what the rule will seed. Use
  `--category` when the word is used for more than one kind of thing ("kinase" names proteins,
  genes and pathways), and `--limit` to see more than 25 examples.
- `find_nodes.py --around CURIE` shows the edges touching one node by predicate, direction and
  source. Use it to find how a family, pathway or disease links to its members or subclasses,
  for a `linked_to` rule.

Prefer, in this order:

1. **Structure in the graph.** Members of a family or pathway, or subclasses of a disease, with
   `linked_to`, `transitive` for a hierarchy. This follows the sources' own curation.
2. **Exact ids** with `ids`, when the user names a few specific things.
3. **Names** with `name`, when the graph has no structure for the request. A name rule is
   approximate: check its examples, and narrow it with `category`, `taxon` (names match other
   species' proteins too; add `"taxon": "NCBITaxon:9606"` unless the user wants them) or a
   longer phrase.

For the user's own data, seed with `{"source": "<its infores>"}`. If they also want "only what
connects", run the `describe-graph` skill with `--user` first, and put the sources it recommends
(plus their own) in `sources`.

## 2. Write the focus file

Write it to `data/filter/<name>.json`. The format is in
[reference/focus-format.md](reference/focus-format.md). Always put the user's words in `request`,
so the subgraph says what it was made for. The defaults are right unless the request says
otherwise:

- `hops` 1: the seeds and everything directly connected to them. Use 0 for "only these
  things", 2 for "and what those connect to". Two hops from many seeds can approach the whole
  graph; check the size in the report.
- `max_degree` 2000: hubs with more edges than this are kept but not reached through.
- `sources` and `predicates`: leave them out unless the user limits the kinds of evidence or
  relationships ("only curated drug targets").

## 3. Cut it and report back

```bash
uv run kgb-filter --focus data/filter/<name>.json
```

It reads the cleaned graph (`data/cleaning`), or the merged one if cleaning hasn't run, and
writes `nodes.jsonl`, `edges.jsonl` and `focus_report.json` to `data/filter/<name>/`. The report
lists each seed rule with how many nodes it matched and examples, the subgraph's size, and its
edges by source and predicate.

Tell the user, in their terms: what the request was taken to mean (the seed rules and a few
examples of what each matched), how big the subgraph is, and which sources it draws on. If a
rule matched nothing or far more than expected, say so and offer the fix (a different word, a
category, a `linked_to` rule). Then offer the next step: query it through the MCP interface, or
describe it.
