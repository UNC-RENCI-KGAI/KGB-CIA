# Cleaning agent

**Team:** @UNC-RENCI-KGAI/cleaning

Prune the merged graph to what can be trusted and explained, and report what was removed and
why. Maps to the accuracy and pruning criterion.

## Run it alone

```bash
uv run kgb-clean                                # data/transformation/merged -> data/cleaning
uv run kgb-clean --input <dir> --output <dir>   # only when the graph lives somewhere else
```

There are no options to choose between: every rule always applies, so the pipeline's
no-argument run is the real run. To drop more edges, put a drop list in `<output>/drop_lists/`
(see below) rather than passing anything.

## Input / output

**Input:** a directory with `nodes.jsonl` and `edges.jsonl` in ORION KGX format
(`shared.schema.Node` / `Edge`), normally the transformation stage's `merged/`.

**Output**, in `--output`:

- `nodes.jsonl`, `edges.jsonl`: the cleaned graph. Records pass through unchanged, including
  `id` and `kgbcia_source_records`.
- `cleaning_report.json`: counts in and out; `attention`, the sources that lost at least half
  their edges, with the CURIE prefixes that didn't resolve; every dropped edge and node by
  reason and by primary knowledge source; unresolved nodes by prefix; flags on kept edges; and a
  few examples of each.

## Reading the result

Start with `attention` (also printed as "Needs attention"). A source there almost always has an
id problem in its metadata: a prefix NodeNorm doesn't know, native ids without a prefix, or
versioned ids. Fix the source's semantics plan with the croissant-semantics skill and rebuild
it. Keeping the unresolved nodes instead would add facts that can't join anything else in the
graph, so there is deliberately no option for that.

## What it removes

Garbage collection (`garbage.py`), in this order. An edge is dropped when it:

| Reason | Meaning |
|---|---|
| `malformed` | no subject, predicate or object |
| `no_primary_knowledge_source` | no provenance, so it can't be explained |
| `dangling` | names a node that isn't in `nodes.jsonl` |
| `unresolved_node` | touches a node NodeNorm couldn't resolve (empty `equivalent_identifiers`) |
| `drop_list:<name>` | listed in `<output>/drop_lists/<name>.jsonl` |

Then nodes are dropped when `unresolved`, or `orphan` (no kept edge touches them).

Kept edges are counted, not changed, as flags: things to fix upstream in metadata, not reasons
to lose a fact.

| Flag | Meaning |
|---|---|
| `self_loop` | subject and object are the same node |
| `knowledge_level_not_provided`, `agent_type_not_provided` | provenance slots left at `not_provided` |
| `type_violation` | the predicate's Biolink domain or range doesn't fit the node categories (`biolink_types.py`); `type_violations_by_predicate` in the report says where. A `treats` edge whose object normalized to a procedure is wrong; a gene `participates_in` a pathway is Biolink being stricter than the graph. After one run on real data, promote the predicates that only produce wrong edges to a drop |
| `rare_category_combination` | a (subject category, predicate, object category) triple under 0.1% of that predicate's edges, for predicates with at least 1,000 kept edges; listed with an example edge each |
| `opposing_predicate` | the pair also carries the opposite predicate (`treats` with `contraindicated_in`); the pairs are listed |

A drop list is a `.jsonl` file in `<output>/drop_lists/`, one edge per line, keyed by `subject`, `predicate`, `object` and
`primary_knowledge_source`. It is not keyed by `id`, because the merge assigns new edge uuids
every run. This is how an evidence check plugs in: a model judges each edge's supporting text
once, writes the edges it rejects to that folder, and every later cleaning run drops them, in the style of the
Translator TMKP filter (NCATSTranslator/translator-ingests#560).

## Status

Garbage collection works and is tested (`tests/test_cleaning.py`). Not yet: conflicting
predicates between the same pair of nodes, and the evidence check that writes a drop list.

Running it per source on the Oct 30 transformation outputs showed where the graph is weak.
All edges were dropped for ChEMBL (CHEMBL.TARGET ids), KEGG pathways (native `ko`/`hsa`/`path`
ids) and STRING ids (ids with no prefix). HPA lost 93% (ENSEMBL ids that don't resolve). Those
are metadata problems, and the report names the prefixes to fix.
