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

There are no rules to switch on or off: every rule always applies. To drop more edges, put a
drop list in `<output>/drop_lists/` (see [Drop and flag lists](#drop-and-flag-lists)). The third
line is the final pass. `--drop-lists` points it at the lists the evidence check wrote for the
first pass, and the flag lists in `data/cleaning/flag_lists` beside them.

The evidence check runs between augmentation and the final pass as its own pipeline stage:

```bash
uv run kgb-evidence --graph data/augmentation          # every question; what the pipeline runs
uv run kgb-evidence --question data/cleaning/evidence/my_question.json   # one question
```

It runs the evidence-check skill's three scripts (gather, judge with Jev, build the lists) for
each question: the ones the skill ships in its `questions/` folder, and any an agent wrote in
`data/cleaning/evidence/*.json` (same `name` replaces the shipped one). Without
`TYPESAFE_API_KEY` it asks Jev nothing, writes each question's requests, leaves its lists as
they were, and the pipeline carries on. A question whose step fails (NCBI unreachable, TypeSafe
down, `uv` missing) is reported and its lists left as they were; the stage still returns 0 so the
final pass runs. `--strict` returns nonzero instead, for running a question by hand.

## Input / output

**Input:** a directory with `nodes.jsonl` and `edges.jsonl` in KGX format
(`shared.schema.Node` / `Edge`), normally the transformation stage's `merged/`.

**Output**, in `--output`:

| File | What it holds |
| --- | --- |
| `nodes.jsonl`, `edges.jsonl` | The cleaned graph. Records pass through unchanged, except that a flagged edge gains two lists (see [Flags on the edges](#flags-on-the-edges)). |
| `cleaning_report.json` | Counts in and out. `attention`: the sources that lost at least half their edges, with the prefixes that didn't resolve. Every dropped edge and node by reason and by source. Unresolved nodes by prefix, flags on kept edges, and a few examples of each. |
| `flagged_edges.jsonl` | The kept edges worth a look, one per line, keyed like a drop list. |
| `unresolved_nodes.tsv` | Every node NodeNorm couldn't resolve: its prefix and name, the edges it took down or that were kept with a flag, and their sources and predicates, most costly first. |
| `pruned_edges.tsv` | The record of what cleaning removed and why: one KGX TSV row per dropped edge (`subject`, `predicate`, `object`, `primary_knowledge_source`, `statement_id`, `publications`, `reason`, a plain-words `explanation`, and a `confidence` where an evidence-check drop line carries one). It is not a gold set: a rule that drops an edge because its ids don't resolve says nothing about whether the claim is true. The scored gold set is the evidence check's `gold_set.tsv`. `malformed` and `no_primary_knowledge_source` edges are counted in the report but not listed, since their rows wouldn't be valid KGX. |

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
| `broken_id` | It touches an ID that is wrong, not just unknown to NodeNorm (`BROKEN_ID_PREFIXES`). Examples: `OBA:VT...` trait IDs written with the wrong prefix (TIGA, GWAS Catalog), and `phenopacket.store:` patients from case reports. Dropped even when the other end resolves. |
| `unresolved_both_ends` | Neither of its nodes resolved in NodeNorm, so it can't join the rest of the graph. |
| `drop_list:<name>` | It's listed in `<output>/drop_lists/<name>.jsonl`. |
| `type_violation:<predicate>-><category>` | A type violation serious enough to drop (`DROP_TYPE_VIOLATIONS`). Today that's `treats` whose object is a Procedure, ClinicalIntervention, BiologicalProcess, ChemicalEntity, OrganismTaxon, Protein or Drug. |

Then any node no kept edge touches is dropped: as `unresolved` if NodeNorm couldn't resolve it,
otherwise as `orphan`.

**Why an edge with one unresolved end is kept.** An edge between a resolved gene and a dbSNP
variant, or a resolved protein and an obsolete GO term, is still a real fact about the resolved
end. So it's kept, with an `unresolved_node` flag. In the submitted graph, 1.96M of the 2.21M
edges with an unresolved end had their other end resolved.

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
| `knowledge_level_not_provided`, `agent_type_not_provided` | A provenance field was left at `not_provided`. |
| `type_violation` | The node categories don't fit the predicate's Biolink domain or range (`biolink_types.py`). The report breaks these down by predicate and category. |
| `rare_category_combination` | A (subject categories, predicate, object categories) combination that makes up under 0.1% of a predicate's edges, for predicates with at least 1,000 kept edges. |
| `opposing_predicate` | The same pair also carries the opposite predicate, such as `treats` and `contraindicated_in`. |

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
