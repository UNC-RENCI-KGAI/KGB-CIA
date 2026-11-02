# Scope rules (step 2)

`select_edges.py` (`kgb-augment select`) keeps the edges worth checking in the literature: those
whose relationship abstracts state as a claim that a paper could contradict. Rules choose them
by (subject category, predicate, object category). The rules are data: the built-in ones are
`DEFAULT_RULES` in `scripts/pubmed_evidence/scope.py`, and `--rules FILE` replaces them.

## How a rule matches an edge

- **First match wins.** Rules are tried in order; the first that matches decides the edge, and
  the edge's statement records that rule's id. So a narrow skip rule placed before a broad
  query rule carves its predicate out of it.
- **Ancestry.** A rule's predicate matches the predicate and everything below it in Biolink
  (`biolink:associated_with` matches `associated_with_increased_likelihood_of`). A rule's
  category matches a node if any of the node's categories is that class or below it, mixins
  included: `biolink:GeneOrGeneProduct` matches Gene, Protein, ProteinIsoform and RNAProduct
  (not Polypeptide, GeneFamily or MacromolecularComplex); `biolink:ChemicalEntity` matches
  SmallMolecule and Drug. An empty `subject` or `object` matches any node.
- **Direction.** A rule matches in its own direction (subject to object), and also reversed
  when the rule is `symmetric` or Biolink marks the edge's predicate symmetric (`binds`,
  `interacts_with`, `physically_interacts_with`, ...). So Protein `binds` chemical is kept by
  `chemical-affects-gene`.
- **Not matched at all:** negated edges (`negated: true`) and edges with a node missing from
  `nodes.jsonl` are skipped before any rule and counted in the report.

Tiers: `query` keeps every matching edge; `capped` keeps only the best-scored pairs (below);
`skip` keeps nothing, and exists so the report can say which skip rule covered an edge type,
while an edge type no rule covers shows as `unmatched`.

## Built-in rules, in order

| id | tier | subject | predicates (with descendants) | object | sym. |
| --- | --- | --- | --- | --- | --- |
| `literature-co-occurrence` | skip | any | `occurs_together_in_literature_with` | any | yes |
| `chemical-treats-disease` | query | ChemicalEntity | `treats`, `contraindicated_in`, `treats_or_applied_or_studied_to_treat` | Disease | |
| `chemical-affects-gene` | query | ChemicalEntity | `affects`, `directly_physically_interacts_with`, `binds` | GeneOrGeneProduct | |
| `gene-disease` | query | GeneOrGeneProduct | `associated_with`, `contributes_to` | Disease | |
| `gene-gene` | capped | GeneOrGeneProduct | `physically_interacts_with`, `interacts_with` | GeneOrGeneProduct | yes |
| `gene-phenotype` | capped | GeneOrGeneProduct | `has_phenotype` | PhenotypicFeature | |
| `pathway-go` | skip | GeneOrGeneProduct | `participates_in`, `enables`, `located_in` | BiologicalProcessOrActivity, CellularComponent | |
| `expression` | skip | GeneOrGeneProduct | `expressed_in` | AnatomicalEntity | |
| `by-construction` | skip | any | `orthologous_to`, `has_part`, `part_of`, `subclass_of`, `superclass_of`, `member_of`, `has_member` | any | yes |

All CURIEs are `biolink:`. Notes:

- **`gene-disease`** names two roots. `associated_with` covers `gene_associated_with_condition`,
  `genetically_associated_with`, `genetic_association`, the likelihood predicates
  (`associated_with_increased_likelihood_of` is how Monarch writes ClinGen's "Likely
  Pathogenic"; "Pathogenic" is `causes`), `correlated_with` and its positive and negative
  forms, and `biomarker_for`. `contributes_to` covers `causes`.
- **`literature-co-occurrence`** comes first because its predicate is also below
  `associated_with`: two entities mentioned together is not a claim, and checking co-mention
  against PubMed would be circular.
- **Skips:** pathway and GO names rarely appear verbatim in abstracts, and GO annotations
  carry their own references and evidence codes. Expression edges are measurements rather than
  claims and are very numerous. The rest are true by construction.

## Capped rules

Gene–gene interactions are numerous and symmetric, and many come from one high-throughput
screen; phenotype names are long and rarely found. So a `capped` rule keeps at most
`--capped-limit` pairs (default 10,000, `config.CAPPED_LIMIT`), per rule:

1. **`--min-score X`** (default: none), if given, first drops the rule's statements whose score
   is below X **or that have no score**.
2. Pairs are ranked by the best score of the rule's statements in them, highest first;
   unscored pairs come last; ties go by pair id.
3. The rule's statements in pairs beyond the limit are dropped. A pair stays if it still has
   statements from another rule. Unordered pairs are deduplicated by construction (one pair id
   for A–B and B–A).

A statement's score is the highest numeric value in any of these slots on any of the edges
that make it: `score`, `confidence_score`, `has_confidence_score`, `combined_score`,
`stringdb_combined_score`, `evidence_score` (`SCORE_SLOTS`). Booleans and NaN don't count.
Scales differ between sources (STRING's raw combined score is 0–1000), and
`--min-score` compares against the raw value.

## `--rules FILE`

A JSON list of rule objects, or `{"rules": [...]}`, in order of precedence. It replaces the
built-in rules entirely, so copy the ones you keep.

```json
{"rules": [
  {"id": "literature-co-occurrence", "tier": "skip",
   "predicates": ["biolink:occurs_together_in_literature_with"], "symmetric": true},
  {"id": "gene-disease", "tier": "query",
   "subject": ["biolink:GeneOrGeneProduct"],
   "predicates": ["biolink:associated_with", "biolink:contributes_to"],
   "object": ["biolink:Disease"]},
  {"id": "gene-phenotype", "tier": "capped",
   "subject": ["biolink:GeneOrGeneProduct"], "predicates": ["biolink:has_phenotype"],
   "object": ["biolink:PhenotypicFeature"]}
]}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `id` | yes | Unique, non-empty; recorded on every statement it keeps (`Statement.rule`) |
| `tier` | yes | `query`, `capped` or `skip` |
| `predicates` | yes | One or more Biolink predicate CURIEs |
| `subject`, `object` | no | Biolink class CURIEs; empty or left out: any node |
| `symmetric` | no | Also match reversed edges; default false |

The file is checked before anything runs: valid JSON, a non-empty list, no unknown fields,
unique ids, and every CURIE a Biolink element (`bmt`, Biolink 4.4.6). A bad file stops the
step with a one-line message. Record why you changed the scope in `notes.md`.

## `scope_report.json`

Written next to the step's other files:

| Key | Content |
| --- | --- |
| `input`, `rules_file`, `capped_limit`, `min_score` | What the step ran on |
| `edges_read`, `negated_skipped`, `missing_node_skipped` | Edge counts before any rule |
| `kept` | `edges`, `statements`, `pairs`, `entities` kept in all |
| `rules` | Per rule id. Query and capped: `tier`, `matched_edges`, `matched_statements`, and the `edges`, `statements` and `pairs` kept; capped rules add `dropped_by_min_score` (`statements`, `edges`) and `dropped_by_cap` (`pairs`, `statements`, `edges`). Skip: `tier`, `skipped_edges` |
| `skipped` | One entry per (subject category, predicate, object category) not kept, using each node's first (most specific) category: `edges` and `rule` (`skip:<id>` or `unmatched`), most edges first |

Read the largest `unmatched` entries: one that states a claim a paper could contradict (a
predicate or category no rule lists) may deserve a rule.
