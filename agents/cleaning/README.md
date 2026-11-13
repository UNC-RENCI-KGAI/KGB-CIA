# Cleaning agent

**Team:** @UNC-RENCI-KGAI/cleaning

Prune the merged graph to what can be trusted and explained, and report what was removed and
why. Maps to the accuracy and pruning criterion.

## Run it alone

```bash
uv run kgb-clean                                # data/transformation/merged -> data/cleaning
uv run kgb-clean --input <dir> --output <dir>   # only when the graph lives somewhere else
uv run kgb-clean --input data/augmentation --output data/final --drop-lists data/cleaning/drop_lists
```

There are no rules to choose between: every rule always applies. To drop more edges, put a drop
list in `<output>/drop_lists/` (see below) rather than passing anything. The pipeline runs
cleaning twice: first on the merged graph with no arguments, then as `final_cleaning` on
augmentation's output (the third line). `--drop-lists` is only for that second pass, which
reads the drop lists the evidence check wrote for the first pass's output directory, and the
flag lists beside them (`data/cleaning/flag_lists`), since the two folders travel together.

## Input / output

**Input:** a directory with `nodes.jsonl` and `edges.jsonl` in ORION KGX format
(`shared.schema.Node` / `Edge`), normally the transformation stage's `merged/`.

**Output**, in `--output`:

- `nodes.jsonl`, `edges.jsonl`: the cleaned graph. Records pass through unchanged, including
  `id` and `kgbcia_source_records`, except that a flagged edge gains two lists (see "Flags on
  the edges" below).
- `cleaning_report.json`: counts in and out; `attention`, the sources that lost at least half
  their edges, with the CURIE prefixes that didn't resolve; every dropped edge and node by
  reason and by primary knowledge source; unresolved nodes by prefix; flags on kept edges; and a
  few examples of each.
- `flagged_edges.jsonl`: the kept edges worth a look, one per line, keyed like a drop list (see
  below).
- `unresolved_nodes.tsv`: every node NodeNorm couldn't resolve, with its prefix, name, the
  edges it took down, the edges kept with an `unresolved_node` flag, and the sources and
  predicates of the dropped edges, most costly first. This is what a metadata fix needs: the
  identifiers, not just the prefix. Written on every run.

## Reading the result

Start with `attention` (also printed as "Needs attention"). A source there almost always has an
id problem in its metadata: a prefix NodeNorm doesn't know, native ids without a prefix, or
versioned ids. Fix the source's semantics plan with the croissant-semantics skill and rebuild
it.

Unresolved nodes are kept where they still connect to the graph. An edge with one unresolved
end (a resolved gene and a dbSNP variant, a resolved protein and a Reactome reaction, a
resolved protein and an obsolete GO term) is a real fact about the resolved end, so it is kept
and flagged `unresolved_node`. An edge with both ends unresolved can't join anything else, so
it is dropped, as is an edge touching an id that is wrong rather than unknown to NodeNorm
(`BROKEN_ID_PREFIXES`). On the Nov 9 graph, 1.96M of the 2.21M edges with an unresolved end
had the other end resolved.

## What it removes

Garbage collection (`garbage.py`), in this order. An edge is dropped when it:

| Reason | Meaning |
|---|---|
| `malformed` | no subject, predicate or object |
| `no_primary_knowledge_source` | no provenance, so it can't be explained |
| `dangling` | names a node that isn't in `nodes.jsonl` |
| `broken_id` | touches an unresolved id that is wrong, not just unknown to NodeNorm (`BROKEN_ID_PREFIXES`): `OBA:VT...` trait ids written with the wrong prefix (TIGA, GWAS Catalog), and `phenopacket.store:` patients from case reports. Dropped even when the other end resolves |
| `unresolved_both_ends` | both its nodes are ones NodeNorm couldn't resolve (empty `equivalent_identifiers`), so it can't join the rest of the graph |
| `drop_list:<name>` | listed in `<output>/drop_lists/<name>.jsonl` |
| `type_violation:<predicate>-><category>` | a type violation promoted to a drop in `DROP_TYPE_VIOLATIONS`: today, `treats` whose object is a Procedure, ClinicalIntervention, BiologicalProcess, ChemicalEntity, OrganismTaxon, Protein or Drug |

Then nodes are dropped when no kept edge touches them: `unresolved` when NodeNorm couldn't
resolve them either, else `orphan`. An unresolved node with a kept edge stays.

The promoted type violations are the drops a reviewer made by hand on the Nov 9 graph, so an
unattended run reaches the same result in one pass: DrugCentral `treats` edges to "General
Anesthesia" (Procedure), "Asthma management" (ClinicalIntervention), "relaxation of muscle"
(BiologicalProcess), "Diagnostic agents" (ChemicalEntity), "Vancomycin-Resistant Enterococci"
(OrganismTaxon) or the drug's own synonym (Protein, Drug). `treats` to a Phenomenon stays a
flag on purpose ("severe pain" is a real indication). To promote another pair, add it to the
table with the names that justify it; every other violation stays a flag.

Kept edges are counted, not changed, as flags: things to fix upstream in metadata, not reasons
to lose a fact.

| Flag | Meaning |
|---|---|
| `unresolved_node` | one end is a node NodeNorm couldn't resolve; the other end resolved |
| `self_loop` | subject and object are the same node |
| `knowledge_level_not_provided`, `agent_type_not_provided` | provenance slots left at `not_provided` |
| `type_violation` | the predicate's Biolink domain or range doesn't fit the node categories (`biolink_types.py`); `type_violations_by_predicate` and `type_violations_by_category` in the report say where |
| `rare_category_combination` | a (subject categories, predicate, object categories) combination, using each node's most specific categories, under 0.1% of that predicate's edges, for predicates with at least 1,000 kept edges; listed with an example edge each. A flag the input already carried is kept (see below) |
| `opposing_predicate` | the pair also carries the opposite predicate (`treats` or a predicate under it, such as `ameliorates_condition`, with `contraindicated_in`); the pairs are listed |

### Flags on the edges

Every kept edge with an `unresolved_node`, `self_loop`, `type_violation`,
`rare_category_combination` or `opposing_predicate` flag, or a flag from a flag list, carries
them on the edge, so the MCP interface can tell a user the edge is in question and why:

```json
"kgbcia_flags": ["type_violation"],
"kgbcia_flag_reasons": ["Biolink expects the subject of enables to be a physical entity, but it is Gene"]
```

The two lists line up, one plain sentence per flag. They are lists of strings so Neo4j stores
them as they are. A cleaning pass replaces any flags its input already had, so the final pass
after augmentation doesn't stack them. The exception is `rare_category_combination`, which is
a statistic of the graph it was computed on. The final pass runs on one request's subgraph, a
biased sample of that graph (a cut around a gene holds a few thousand edges, and whatever it
holds of a predicate says nothing about how rare a combination is across the graph), so a
pass keeps the flag and reason its input carries, adds any it finds in its own input, and
counts the kept ones under `flags_inherited` in the report. On the Nov 12 cleaned graph, a
21,732-edge cut around its 1,325 rare-flagged edges came out of a second pass with 7 of them
before this rule and all 1,325 after.

A flag list is how a check outside `kgb-clean` marks edges without dropping them: a `.jsonl`
file in `flag_lists/`, beside `drop_lists/` (`<output>/flag_lists/` by default), one edge per
line, keyed like a drop list, with a `flag` and a `reason`:

```json
{"subject": "CHEBI:82700", "predicate": "biolink:interacts_with", "object": "UniProtKB:P07550", "primary_knowledge_source": "infores:drugcentral", "flag": "wrong_mechanism", "reason": "PMID:20371707 reports a different action than antagonism"}
```

The evidence check writes one for edges whose paper shows the drug acting on the target, but
not the way the edge's qualifier says.

The same edges are also written to `flagged_edges.jsonl`, keyed like a drop list,
with its flags, the endpoints' most specific categories and, for a type violation, whether
the domain or the range failed. A node can have more than one most specific category (a drug
that is both `Drug` and `SmallMolecule`, neither under the other), so these are lists, and the
category combinations behind `rare_category_combination` use all of them. The order of a
node's `category` list is never relied on. To drop some of them, copy those lines into a drop list.

Decide drops by predicate and category, not by predicate alone. On the Oct 30 DrugCentral
indication graph, `treats` had 411 range violations: about 240 objects were a Procedure
(imaging, contraception), which are wrong as `treats`, but about 100 were a Phenomenon (severe
pain, skin irritation), which are real indications that NodeNorm categorizes as Phenomenon.
Some violations are Biolink's, not the graph's: the participation predicates (`has_participant`,
`participates_in`, `catalyzes`, `has_catalyst`) put `occurrent` on the participant's side, so
every gene or protein in a Reactome pathway or reaction would be flagged (262K edges on the
Nov 9 graph). `biolink_types.NOT_ENFORCED` turns off that side for them; the process side is
still checked.

A drop list is a `.jsonl` file in `<output>/drop_lists/`, one edge per line, keyed by `subject`, `predicate`, `object` and
`primary_knowledge_source`. It is not keyed by `id`, because the merge assigns new edge uuids
every run. This is how an evidence check plugs in: a model judges each edge's supporting text
once, writes the edges it rejects to that folder, and every later cleaning run drops them (the
`evidence-check` skill in `.agents/skills/cleaning/` does this with Jev), in the style of the
Translator TMKP filter (NCATSTranslator/translator-ingests#560).

## Status

Garbage collection, the type, rare-combination and opposing-predicate flags, and
`flagged_edges.jsonl` work and are tested (`tests/test_cleaning.py`). The `evidence-check` skill
writes drop lists; it runs up to the Jev step without a key and needs `TYPESAFE_API_KEY` to
finish (`tests/test_evidence_check.py`).

Running it per source on the Oct 30 transformation outputs showed where the graph is weak.
All edges were dropped for ChEMBL (CHEMBL.TARGET ids), KEGG pathways (native `ko`/`hsa`/`path`
ids) and STRING ids (ids with no prefix). HPA lost 93% (ENSEMBL ids that don't resolve). Those
are metadata problems, and the report names the prefixes to fix.
