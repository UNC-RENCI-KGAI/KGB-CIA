# Focus file format

One JSON file per request. `kgb-filter` checks it before reading the graph and names every
problem it finds.

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | Lowercase letters, digits and `_`. Names the output, `data/filter/<name>/`. |
| `request` | yes | The user's request, in their words. Kept in the report. |
| `seeds` | yes | One or more seed rules (below). The seeds are the union of what they match. |
| `hops` | no | How many steps out from the seeds, 0 to 3. Default 1. |
| `max_degree` | no | Nodes with more allowed edges than this are kept but not expanded through. Default 2000. |
| `sources` | no | Only edges from these primary knowledge sources (infores ids). Default: all. |
| `predicates` | no | Only edges with these predicates. Default: all. |

The subgraph's edges are every allowed edge between its nodes.

## Seed rules

Each rule has exactly one of these keys.

| Rule | Matches |
|---|---|
| `{"ids": ["NCBIGene:1017", ...]}` | These nodes. |
| `{"name": "kinase", "category": "biolink:Protein", "taxon": "NCBITaxon:9606"}` | Nodes whose name contains the text as a word, any case. `category` (optional, can be a list) and `taxon` (optional) narrow it; `find_nodes.py` shows each node's taxon. |
| `{"linked_to": ["MONDO:0004975"], "predicate": "biolink:subclass_of", "transitive": true}` | Nodes that are the subject of such an edge to one of the listed nodes: members, parts or subclasses. With `transitive`, their own members or subclasses too. `predicate` can be a list. |
| `{"source": "infores:my-lab-data"}` | Every node an edge from that primary knowledge source touches: a whole dataset. |

## Examples

"Alzheimer's disease and its subtypes, with the genes and drugs connected to them":

```json
{
  "name": "alzheimers",
  "request": "Alzheimer's disease and its subtypes, with the genes and drugs connected to them",
  "seeds": [
    {"ids": ["MONDO:0004975"]},
    {"linked_to": ["MONDO:0004975"], "predicate": "biolink:subclass_of", "transitive": true}
  ],
  "hops": 1
}
```

"My screening hits, and only what the curated drug-target sources say about them":

```json
{
  "name": "screen_hits",
  "request": "my screening hits, and what curated drug-target sources say about them",
  "seeds": [{"source": "infores:my-lab-screen"}],
  "sources": ["infores:my-lab-screen", "infores:drugcentral", "infores:chembl"],
  "hops": 1
}
```
