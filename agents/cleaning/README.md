# Cleaning agent

**Team:** @UNC-RENCI-KGAI/cleaning

Prune the merged graph to what can be trusted and explained, and report what was removed and
why. Maps to the accuracy and pruning criterion.

## Run it alone

```bash
uv run kgb-clean                                   # data/transformation/merged -> data/cleaning
uv run kgb-clean --input <dir> --output <dir>
uv run kgb-clean --keep-unresolved                 # keep nodes NodeNorm couldn't resolve
uv run kgb-clean --drop-list evidence_check.jsonl  # also drop these edges; repeatable
```

## Input / output

**Input:** a directory with `nodes.jsonl` and `edges.jsonl` in ORION KGX format
(`shared.schema.Node` / `Edge`), normally the transformation stage's `merged/`.

**Output**, in `--output`:

- `nodes.jsonl`, `edges.jsonl`: the cleaned graph. Records pass through unchanged, including
  `id` and `kgbcia_source_records`.
- `cleaning_report.json`: counts in and out, every dropped edge and node by reason and by
  primary knowledge source, unresolved nodes by CURIE prefix, flags on kept edges, and a few
  examples of each.

## What it removes

Garbage collection (`garbage.py`), in this order. An edge is dropped when it:

| Reason | Meaning |
|---|---|
| `malformed` | no subject, predicate or object |
| `no_primary_knowledge_source` | no provenance, so it can't be explained |
| `dangling` | names a node that isn't in `nodes.jsonl` |
| `unresolved_node` | touches a node NodeNorm couldn't resolve (empty `equivalent_identifiers`) |
| `drop_list:<file>` | listed in a `--drop-list` file |

Then nodes are dropped when `unresolved`, or `orphan` (no kept edge touches them).

Kept edges are counted, not changed, as `self_loop`, `knowledge_level_not_provided` and
`agent_type_not_provided`: things to fix upstream in metadata, not reasons to lose a fact.

A drop list is JSON lines, one edge per line, keyed by `subject`, `predicate`, `object` and
`primary_knowledge_source`. It is not keyed by `id`, because the merge assigns new edge uuids
every run. This is how an evidence check plugs in: a model judges each edge's supporting text
once, writes the edges it rejects, and cleaning drops them reproducibly, in the style of the
Translator TMKP filter (NCATSTranslator/translator-ingests#560).

## Status

Garbage collection works and is tested (`tests/test_cleaning.py`). Not yet: conflicting
predicates between the same pair of nodes, and the evidence check that writes a drop list.

Running it per source on the Oct 30 transformation outputs showed where the graph is weak.
All edges were dropped for ChEMBL (CHEMBL.TARGET ids), KEGG pathways (native `ko`/`hsa`/`path`
ids) and STRING ids (ids with no prefix). HPA lost 93% (ENSEMBL ids that don't resolve). Those
are metadata problems, and the report names the prefixes to fix.
