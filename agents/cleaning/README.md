# Cleaning agent

Stages 3, 6 and 7. Cleaning cuts the merged graph down to edges that can be trusted and explained,
and reports what it removed and why. It drops edges that can't be right. It keeps edges that
are only suspicious, but flags each one and writes the reason onto the edge, so anyone querying
the graph can see it.

The pipeline runs cleaning twice:
1. On the merged graph, before the filter and augmentation, so the paid steps never spend money
   on edges cleaning would drop.
2. At the end, on augmentation's output. This pass also applies the evidence check's drop and
   flag lists.

## Run it

```bash
uv run kgb-clean                                # data/transformation/merged -> data/cleaning
uv run kgb-clean --input <dir> --output <dir>   # a graph somewhere else
uv run kgb-clean --input data/augmentation --output data/final --drop-lists data/cleaning/drop_lists
```

Every rule always applies; there's nothing to switch on or off. The third line is the final
pass: `--drop-lists` points it at `data/cleaning/drop_lists`, where the evidence check writes,
and it reads the flag lists in `data/cleaning/flag_lists` beside them.

The evidence check runs between augmentation and the final pass, as its own stage:

```bash
uv run kgb-evidence --graph data/augmentation          # every question; what the pipeline runs
uv run kgb-evidence --question data/cleaning/evidence/my_question.json   # one question
```

For each question (those the skill ships, plus any an agent wrote in
`data/cleaning/evidence/`), it gathers the cited abstracts, asks Jev about each edge, and writes
the lists. Without `TYPESAFE_API_KEY`, a question whose Jev answers are already cached from an
earlier run with the key has its lists rebuilt from those answers; any other question's lists are
left as they were. If a step fails, it reports why, leaves the lists as they were, and the
pipeline carries on; `--strict` makes a failure stop it instead.

## Input / output

**Input:** a directory with `nodes.jsonl` and `edges.jsonl` in KGX format
(`shared.schema.Node` / `Edge`), normally the transformation stage's `merged/`.

**Output**, in `--output`:

| File | What it holds |
| --- | --- |
| `nodes.jsonl`, `edges.jsonl` | The cleaned graph. Records pass through unchanged, except that a flagged edge gains two lists (see [Flags on the edges](#flags-on-the-edges)). |
| `cleaning_report.json` | Counts in and out. `attention`: the sources that lost at least half their edges, with the prefixes that didn't resolve. Dropped edges by reason and by source; dropped nodes by reason. Unresolved nodes by prefix, flags on kept edges, and a few examples of each. |
| `flagged_edges.jsonl` | The kept edges worth a look, one per line, keyed like a drop list. |
| `unresolved_nodes.tsv` | Every node NodeNorm couldn't resolve: its prefix and name, how many edges it took down and how many were kept with a flag, and the sources and predicates of the edges it took down, most costly first. |
| `pruned_edges.tsv` | Every dropped edge except `malformed` and `no_primary_knowledge_source` ones (counted in the report only, since their rows would have no predicate or source), one KGX TSV row each, with its `reason` and a plain-words `explanation` (and the evidence check's `confidence`, where it gave one). It records what was removed, not whether the claim is false: the scored gold set is the evidence check's `gold_set.tsv`. |

### Reading the result

Start with `attention`, which the run also prints as "Needs attention". A source listed there
almost always has an ID problem in its metadata: a prefix NodeNorm doesn't know, native IDs
without a prefix, or versioned IDs. Fix the source's plan with the croissant-semantics skill and
rebuild it. `unresolved_nodes.tsv` gives the identifiers a fix needs.

## What it drops

An edge is dropped when:

| Reason | Meaning |
| --- | --- |
| `malformed` | It has no subject, predicate or object. |
| `no_primary_knowledge_source` | It has no provenance, so it can't be explained. |
| `dangling` | It names a node that isn't in `nodes.jsonl`. |
| `broken_id` | An end that didn't resolve has an ID that is wrong, not just unknown to NodeNorm (`BROKEN_ID_PREFIXES`). Examples: `OBA:VT...` trait IDs written with the wrong prefix (TIGA, GWAS Catalog), and `phenopacket.store:` patients from case reports. Dropped even when the other end resolves. |
| `unresolved_both_ends` | Neither of its nodes resolved in NodeNorm, so it can't join the rest of the graph. |
| `drop_list:<name>` | It's listed in `<output>/drop_lists/<name>.jsonl`. |
| `type_violation:<predicate>-><category>` | A type violation serious enough to drop (`DROP_TYPE_VIOLATIONS`). Today that's `treats` whose object is a Procedure, ClinicalIntervention, BiologicalProcess, ChemicalEntity, OrganismTaxon, Protein or Drug. |

Then any node no kept edge touches is dropped: as `unresolved` if NodeNorm couldn't resolve it,
otherwise as `orphan`. A node with no `id` is dropped as `malformed`.

**Why an edge with one unresolved end is kept.** An edge between a resolved gene and a dbSNP
variant, or a resolved protein and an obsolete GO term, is still a real fact about the resolved
end. So it's kept, with an `unresolved_node` flag. In the submitted graph, 1.91M of the 2.21M
edges with an unresolved end were kept, flagged `unresolved_node`.

**Why only some type violations are drops.** The `treats` drops are ones a reviewer made by hand
on real data, such as DrugCentral `treats` edges to "General Anesthesia" (Procedure) or
"relaxation of muscle" (BiologicalProcess). `treats` to a Phenomenon stays a flag on purpose,
because "severe pain" is a real indication. Decide drops by predicate *and* category, never by
predicate alone. To make another pair a drop, add it to `DROP_TYPE_VIOLATIONS` with the examples
that justify it.

## What it flags

Flagged edges are kept. A flag points at something to fix upstream in the metadata; it isn't a
reason to lose a fact.

| Flag | Meaning |
| --- | --- |
| `unresolved_node` | One end didn't resolve in NodeNorm; the other did. |
| `self_loop` | The subject and object are the same node. |
| `type_violation` | The node categories don't fit the predicate's Biolink domain or range (`biolink_types.py`). The report breaks these down by predicate and category. |
| `rare_category_combination` | A (subject categories, predicate, object categories) combination that makes up under 0.1% of a predicate's edges, for predicates with at least 1,000 kept edges. |
| `opposing_predicate` | The same pair also carries the opposite predicate. Today the one pair is `treats` and `contraindicated_in` (each with the predicates under it). |

The report also counts `knowledge_level_not_provided` and `agent_type_not_provided` (a
provenance field left at `not_provided`), but these are report counts only: they aren't written
on the edge or to `flagged_edges.jsonl`. Flags from a flag list (such as the evidence check's
`wrong_mechanism`) are written on the edge like the ones above.

Some violations are Biolink's quirks, not the graph's errors. The participation predicates
(`has_participant`, `participates_in`, `catalyzes`, `has_catalyst`) would flag every gene in a
Reactome pathway, so `biolink_types.NOT_ENFORCED` turns off that side of the check for them.

Categories always use each node's *most specific* categories, never the first in its list. A
node can have more than one (a drug that is both `Drug` and `SmallMolecule`).

### Flags on the edges

A flagged edge carries its flags and a plain-language reason for each, so the MCP server can tell
a user that the edge is in question and why:

```json
"kgbcia_flags": ["type_violation"],
"kgbcia_flag_reasons": ["Biolink expects the subject of enables to be a physical entity, but it is Gene"]
```

The two lists line up, one sentence per flag. A cleaning pass replaces the flags its input
carried, so the final pass doesn't stack them. The exception is `rare_category_combination`: it's
a statistic of the whole graph, and the final pass only sees one request's subgraph, so a pass
keeps the rare flags its input already had.

## Drop and flag lists

These let a check outside `kgb-clean` drop or flag specific edges. The evidence check is one
such check.

A **drop list** is a `.jsonl` file in `<output>/drop_lists/`, one edge per line, keyed by
`subject`, `predicate`, `object` and `primary_knowledge_source`. Edge `id`s can't be used as the
key, because the merge assigns new ones on every run. To match only one statement, and not
another edge that differs from it only in qualifiers, a line can also give the edge's
`statement_id` (`shared.schema.edge_statement_id`), or its qualifiers and `negated`.

A **flag list** is the same, in `<output>/flag_lists/`, with a `flag` and a `reason` on each
line:

```json
{"subject": "CHEBI:82700", "predicate": "biolink:interacts_with", "object": "UniProtKB:P07550", "primary_knowledge_source": "infores:drugcentral", "statement_id": "<16 hex digits>", "causal_mechanism_qualifier": "antagonism", "flag": "wrong_mechanism", "reason": "PMID:20371707 reports a different action than antagonism"}
```

To drop some flagged edges, copy their lines from `flagged_edges.jsonl` into a drop list.

### The evidence check

The [`evidence-check`](../../.agents/skills/cleaning/evidence-check/) skill judges edges against
the PubMed abstracts they cite, using the Jev model. An edge an abstract contradicts goes in a
drop list. An edge whose paper gets a detail wrong, such as the mechanism, goes in a flag list.
So does a text-mined edge that no abstract supports (`unsupported_by_abstract`). Every later
cleaning run applies the lists. The check is modeled on the Translator TMKP filter
([NCATSTranslator/translator-ingests#560](https://github.com/NCATSTranslator/translator-ingests/issues/560)).
It runs up to the Jev step without a key, and needs `TYPESAFE_API_KEY` to finish.

## Limitations

- The evidence check covers only edges that cite an abstract, and few sources cite them.
- A drop- or flag-list line that matches no edge is skipped without a warning.
