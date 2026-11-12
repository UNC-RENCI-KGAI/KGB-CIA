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
uv run python $S/find_nodes.py "kinase" --category biolink:Protein
uv run python $S/find_nodes.py --around MONDO:0004975
```

- `find_nodes.py TEXT` lists nodes with a word in their name that starts with TEXT, counted by
  category ("kinase" also finds "kinases", "ALK" also finds "ALKBH5"). It's the same match a
  `name` rule makes, so what it finds is what the rule will seed. Use `--category` when the word
  names more than one kind of thing ("kinase" names proteins, genes and pathways): it keeps nodes
  that have that Biolink class anywhere in their category list, so `biolink:Protein` selects
  proteins. The counts show each node's most specific category (a drug can have two, shown
  joined with " + "). To keep only human nodes use `taxon`. `--limit` shows more than 25
  examples. Run it with `uv run python`, so the project environment is available.
- `find_nodes.py --around CURIE` shows the edges touching one node by predicate, direction and
  source. Use it to find how a family, pathway or disease links to its members or subclasses,
  for a `linked_to` rule.

Prefer, in this order:

1. **Structure in the graph.** Members of a family or pathway, or subclasses of a disease, with
   `linked_to`, `transitive` for a hierarchy. This follows the sources' own curation.
   `linked_to` finds the nodes that are the *subject* of an edge to the targets (a subtype
   `subclass_of` its disease). If the group is the subject instead (a pathway `has_participant`
   its genes), seed the group by id, use `hops` 1, and limit `predicates` to that edge.
2. **Exact ids** with `ids`, when the user names specific things or gives you a list (a file
   of UniProt accessions, say a Pharos / IDG family download). Write them as
   `UniProtKB:<accession>`. Ids that are not in the graph are a gap to report to the user, not
   something to work around. Never make the user supply a list: most people don't know where to
   get one, and "go with your default" must always work. Without one, use a name rule (next).
3. **Names** with `name`, when the graph has no structure for the request and there is no list.
   A name rule is approximate: check its examples, and narrow it with `taxon` (names match other
   species' proteins too; add `"taxon": "NCBITaxon:9606"` unless the user wants them), a longer
   phrase, or `category` once you have read the counts. For a known family such as kinases,
   GPCRs or ion channels the graph has no family node, so a name search over-matches (for
   kinases, thousands of nodes against about 635 human kinases) and also misses members whose
   names don't contain the word. Deliver it anyway, but say plainly that it is approximate and
   in which direction, and offer once that an exact list would sharpen it.

**Seed a gene and its protein together.** The graph keeps genes (`NCBIGene:`, named by symbol)
and proteins (`UniProtKB:`, named `<SYMBOL>_HUMAN ...`) as separate nodes, and the sources
split between them: drug–target edges (ChEMBL, DrugCentral) and pathway participants reach the
protein; gene–disease, GWAS and expression edges reach the gene. One hop from ABL1's gene alone
gave 5,109 edges and no drugs; its protein has the drug edges. So when a request names genes
or proteins ("kinases and the drugs that target them"), seed both: put both ids in an `ids`
rule, or leave `category` off a name rule, or add a second rule for the other kind.

**A small known family without a list** (nuclear receptors, a class of ion channels: up to
about 100 members): a name search misses members whose names don't contain the family word
(ESR1, AR, GR and PPARG for "nuclear receptor"). Propose the members from your own knowledge
instead, and have the user confirm them:

1. List the members you know by gene symbol and resolve them in one pass over `nodes.jsonl`
   (human UniProt entries are named `<SYMBOL>_HUMAN ...`). Don't call `find_nodes.py` once per
   symbol: each call reads the whole file.
2. Show the user a table: gene symbol, protein name, graph id, and status (found, not in the
   graph, or several candidates). Say that the list is from your own knowledge, not an
   authoritative source, and may be incomplete or contain mistakes.
3. Ask them to confirm: use these, add or remove some, or "default", which uses the name search
   instead. Wait for the answer; "default" must always work.
4. Write an `ids` rule from the confirmed ids that were found. Ids not in the graph are a gap to
   report, not to work around.

Above about 100 members (kinases have 635) don't list from memory: use the name search and say
it is approximate.

For the user's own data, seed with `{"source": "<its infores>"}`, `hops` 0 and `"sources":
["<its infores>"]`. Both limits matter. One hop from a whole source reaches into the rest of
the graph (from DrugCentral it was millions of edges, almost none of them DrugCentral's). And
`hops` 0 alone keeps every edge between the seed nodes whatever its source: for DrugCentral
only 39% of those edges were its own, for the Dark Kinase Knowledgebase 52%. Add `sources` to
show only theirs, then offer "what else the graph says about these nodes" (drop `sources`, or
use one hop) as the next step. If they also want "only what connects", run the
`describe-graph` skill with `--user` first, and put the sources it recommends (plus their
own) in `sources`.

## 2. Write the focus file

Write it to `data/filter/<name>.json`. The format is in
[reference/focus-format.md](reference/focus-format.md). Always put the user's words in `request`,
so the subgraph says what it was made for. The defaults are right unless the request says
otherwise:

- `hops` 1: the seeds and everything directly connected to them. Use 0 for "only these
  things", 2 for "and what those connect to". Two hops from many seeds can approach the whole
  graph; check the size in the report. If you choose 0 and the result is a few dozen nodes,
  show it and offer one hop, saying what that would add.
- `max_degree` 2000: hubs with more edges than this are kept but not reached through. Seeds
  are never held back: a seed above the limit still expands, and the report lists it under
  `seed_hubs`, so check its size. `seed_hubs` is only filled when a seed expands (`hops` 1 or
  more), and degrees count only edges that pass your `sources` and `predicates`; at `hops` 0 an
  empty `seed_hubs` says nothing, so use `find_nodes.py --around` if size matters.
- `sources` and `predicates`: leave them out unless the user limits the kinds of evidence or
  relationships ("only curated drug targets").

## 3. Cut it and report back

```bash
uv run kgb-filter --focus data/filter/<name>.json
```

It reads the cleaned graph (`data/cleaning`), or the merged one if cleaning hasn't run, and
writes `nodes.jsonl`, `edges.jsonl` and `focus_report.json` to `data/filter/`, where augmentation
reads them, replacing the last subgraph. The report
lists each seed rule with how many nodes it matched and examples, the subgraph's size, and its
edges by source and predicate.

Wait for `kgb-filter` to finish and read `focus_report.json` before you reply: a reply without
the sizes is not a result. Then, before you tell the user it is done, check the report against
what you expect:

- **Seeds.** Compare the count with what the request should give. If you would expect hundreds
  and the rule matched thousands (or the reverse), narrow the rule as far as the graph allows.
  If it is still far off, deliver it and say so in numbers ("5,800 matches where a kinase list
  would have about 635"), never as if it were exact.
- **One source dominating.** If a single source or predicate is more than half the edges (for
  kinases, cell-location edges from COMPARTMENTS were 78%), the subgraph will bury what was
  asked for. Narrow with `predicates` or `sources` that match the request ("drugs that target
  them": `biolink:interacts_with` from the drug sources) and cut again.
- **Hub seeds.** If the report lists `seed_hubs`, they expanded anyway; say which, and how much
  they add.

Tell the user, in their terms: what the request was taken to mean (the seed rules and a few
examples of what each matched), how big the subgraph is, and which sources it draws on. If a
rule matched nothing or far more than expected, say so and offer the fix (a different word, a
category, a `linked_to` rule). Then offer the next step: query it through the MCP interface, or
describe it.
