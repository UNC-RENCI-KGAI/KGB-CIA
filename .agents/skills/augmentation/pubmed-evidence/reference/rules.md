# Scope rules (step 1)

`augment.py select` (`kgb-augment select`) keeps the edges worth checking in the literature:
those whose relationship abstracts state as a claim that a paper could contradict. Rules choose them
by (subject category, predicate, object category), and by how the edge was made (its
`knowledge_level` and `agent_type`). The rules are data: the built-in ones are `DEFAULT_RULES`
in `scripts/pubmed_evidence/scope.py`, and `--rules FILE` replaces them.

## Two kinds of rule

- A **statement rule** names `predicates`. It picks edges by subject category, predicate and
  object category, and by `knowledge_level` and `agent_type` too if it lists them (below).
- A **provenance rule** names a `knowledge_level` or an `agent_type`, or both, and no
  `predicates`; it can't set `subject`, `object` or `symmetric`. It picks no edges by itself:
  it lowers the tier of the edges a statement rule picked.

Each edge is decided in three steps:

1. Its **statement rule** is the first statement rule that matches it. If none does, the edge
   is `unmatched`. If it is a skip rule, the edge is skipped as `skip:<statement rule>`,
   whatever its provenance: a text-mined GO `located_in` edge is `skip:pathway-go`.
2. Otherwise, if the first provenance rule whose knowledge levels and agent types the edge's
   are among has a tier no higher than the statement rule's (skip < capped < query), the edge
   takes that tier: a `skip` provenance rule skips it as `skip:<provenance rule>`, and a
   `capped` one puts it in the provenance rule's own cap (below). A provenance rule never
   raises a tier.
3. Otherwise the statement rule's tier applies.

So order matters among statement rules, and among provenance rules (the first that matches
wins in each), but not between the two: a provenance rule may sit anywhere in the list.

The edge's statement keeps its statement rule (`rule` in `statements.jsonl`), and its pair
lists only statement rules (`rules` in `pairs.jsonl`), so the label step offers the statement
rule's predicates whatever the edge's provenance (below). The statement's `provenance_rule`
names the provenance rule that set its tier. A statement made of several edges, some in its
statement rule's own tier and some lowered by a provenance rule, takes the higher tier and no
provenance rule; one whose kept edges were all lowered takes the highest tier among their
provenance rules, then the first of them in rule order. Every kept edge, lowered or not,
counts in the statement's `edge_count`, `sources`, `source_pmids` and `score`.

## How a statement rule matches an edge

- **First match wins.** Statement rules are tried in order; the first that matches is the
  edge's statement rule, and the edge's statement records its id. So a narrow skip rule placed
  before a broad query rule carves its predicate out of it.
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
- **Provenance.** A statement rule that lists `knowledge_level` or `agent_type` values matches
  only an edge whose own is among them; one that lists both needs both, as it needs its
  subject and object (a provenance rule matches the same way). A skip rule restricted by
  provenance drops nothing from the predicates a claim may use (below): a text-mined `treats`
  edge is skipped, but a paper can still claim `treats`.
- **Negated edges** (`negated: true`) are matched like any other, as their own statement. Step
  6 checks them, and never supports them: nothing is narrower than a negated statement
  (DESIGN.md, step 6).
- **Not matched at all:** the edges step 6 added on an earlier run (`id` starting
  `kgbcia-augmentation:`; step 6 proposes them again from the edges they refine) and edges with
  a node missing from `nodes.jsonl` are skipped before any rule and counted in the report.

Tiers: `query` keeps every matching edge; `capped` keeps only the best-scored pairs (below),
and a capped provenance rule's edges have a cap of their own; `skip` keeps nothing, and exists
so the report can say which skip rule covered an edge type, while an edge type no rule covers
shows as `unmatched`.

## Built-in rules, in order

| id | tier | subject | predicates (with descendants) | object | sym. | provenance |
| --- | --- | --- | --- | --- | --- | --- |
| `text-co-occurrence` | skip | any | any | any | | `knowledge_level: text_co_occurrence` |
| `text-mined` | skip | any | any | any | | `agent_type: text_mining_agent` |
| `literature-co-occurrence` | skip | any | `occurs_together_in_literature_with` | any | yes | |
| `chemical-treats-disease` | query | ChemicalEntity | `treats`, `contraindicated_in`, `treats_or_applied_or_studied_to_treat` | Disease | | |
| `chemical-affects-gene` | query | ChemicalEntity | `affects`, `directly_physically_interacts_with`, `binds` | GeneOrGeneProduct | | |
| `gene-disease` | query | GeneOrGeneProduct | `associated_with`, `contributes_to` | Disease | | |
| `gene-gene` | capped | GeneOrGeneProduct | `physically_interacts_with`, `interacts_with` | GeneOrGeneProduct | yes | |
| `gene-phenotype` | capped | GeneOrGeneProduct | `has_phenotype` | PhenotypicFeature | | |
| `pathway-go` | skip | GeneOrGeneProduct | `participates_in`, `enables`, `located_in` | BiologicalProcessOrActivity, CellularComponent | | |
| `expression` | skip | GeneOrGeneProduct | `expressed_in` | AnatomicalEntity | | |
| `by-construction` | skip | any | `orthologous_to`, `has_part`, `part_of`, `subclass_of`, `superclass_of`, `member_of`, `has_member` | any | yes | |

All CURIEs are `biolink:`. Notes:

- **`gene-disease`** names two roots. `associated_with` covers `gene_associated_with_condition`,
  `genetically_associated_with`, `genetic_association`, the likelihood predicates
  (`associated_with_increased_likelihood_of` is how Monarch writes ClinGen's "Likely
  Pathogenic"; "Pathogenic" is `causes`), `correlated_with` and its positive and negative
  forms, and `biomarker_for`. `contributes_to` covers `causes`.
- **`text-co-occurrence`** and **`text-mined`** come first, before any rule on the statement:
  an edge mined from the literature (a co-mention, or a text-mining pipeline's reading of one)
  isn't a claim a paper could contradict, whatever predicate its source writes it as (eRAM
  writes `gene_associated_with_condition`, under `gene-disease`'s `associated_with`, on 113,706
  text co-occurrence edges), and checking it against PubMed would be circular. The Nov 12 2026
  run, without these rules, put 127,774 of its 131,195 pairs in the query tier that way.
- **`literature-co-occurrence`** comes next because its predicate is also below
  `associated_with`: two entities mentioned together is not a claim, and checking co-mention
  against PubMed would be circular.
- **Skips:** pathway and GO names rarely appear verbatim in abstracts, and GO annotations
  carry their own references and evidence codes. Expression edges are measurements rather than
  claims and are very numerous. The rest are true by construction.

## Capped rules

Gene–gene interactions are numerous and symmetric, and many come from one high-throughput
screen; phenotype names are long and rarely found. So a `capped` rule keeps at most
`--capped-limit` pairs (default 10,000, `config.CAPPED_LIMIT`), per rule. A statement is in
the cap of its provenance rule if it has one, else in its statement rule's; a query statement
is never capped, and a statement a provenance rule took is never in its statement rule's cap.

1. **`--min-score X`** (default: none), if given, first drops the rule's statements whose score
   is below X **or that have no score**.
2. Pairs are ranked by the best score of the rule's statements in them, highest first;
   unscored pairs come last; ties go by pair id.
3. The rule's statements in pairs beyond the limit are dropped. A pair stays if it still has
   statements from another rule. Unordered pairs are deduplicated by construction (one pair id
   for A–B and B–A).

Every pair with the rule's statements counts against its cap, a pair another rule keeps too:
the rule's statements in it widen what the review step sends, which costs money.

A statement's score is the highest numeric value in any of these slots on any of the edges
that make it: `score`, `confidence_score`, `has_confidence_score`, `combined_score`,
`stringdb_combined_score` (`SCORE_SLOTS`); not the `lit_support_score` step 6 writes. Booleans
and NaN don't count.
Scales differ between sources (STRING's raw combined score is 0–1000), and
`--min-score` compares against the raw value.

## `--rules FILE`

A JSON list of rule objects, or `{"rules": [...]}`, in order of precedence. It replaces the
built-in rules entirely, so copy the ones you keep.

```json
{"rules": [
  {"id": "text-co-occurrence", "tier": "skip", "knowledge_level": ["text_co_occurrence"]},
  {"id": "text-mined", "tier": "skip", "agent_type": ["text_mining_agent"]},
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
| `id` | yes | Unique, non-empty; recorded on every statement it keeps (`rule` for a statement rule, `provenance_rule` for a provenance rule) |
| `tier` | yes | `query`, `capped` or `skip` |
| `predicates` | in a statement rule | Biolink predicate CURIEs; left out (or empty): a provenance rule, which needs a `knowledge_level` or an `agent_type` |
| `subject`, `object` | no; statement rules only | Biolink class CURIEs; empty or left out: any node |
| `symmetric` | no; statement rules only | Also match reversed edges; default false |
| `knowledge_level` | no | Biolink `KnowledgeLevelEnum` values the edge's `knowledge_level` may have; empty or left out: any |
| `agent_type` | no | Biolink `AgentTypeEnum` values the edge's `agent_type` may have; empty or left out: any |

The file is checked before anything runs: valid JSON, a non-empty list, no unknown fields,
unique ids, every CURIE a Biolink element (`bmt`, Biolink 4.4.6), every knowledge level and
agent type a value of its enum (`shared.schema`), each rule naming predicates, a knowledge
level or an agent type, and no provenance rule setting `subject`, `object` or `symmetric`. A
bad file stops the step with a one-line message. Record why you changed the scope in
`notes.md`.

The rules also set what the label step asks for. A pair's claims may use the predicates of the
statement rules its statements matched (`pairs.jsonl`'s `rules`, which never name a
provenance rule), their descendants and their `opposite_of` partners, each read from the
rule's subject to its object (DESIGN.md, step 3). The label step reads the rules file that
`scope_report.json` names, and each statement rule's reply schema is part of the label prompt
version, so a rules file that changes a predicate list starts fresh claims. Skip and
provenance rules have no reply schema, so adding, removing or changing a provenance rule
leaves the version, and the claims already cached, as they were.

## `scope_report.json`

Written next to the step's other files:

| Key | Content |
| --- | --- |
| `input`, `rules_file`, `capped_limit`, `min_score` | What the step ran on |
| `edges_read`, `earlier_run_skipped`, `missing_node_skipped` | Edge counts before any rule |
| `invalid_edges` | Edges that aren't valid `Edge` records, skipped: `edges`, `by_reason` (count per reason, most common first), `examples` (the first 10, `line` and `reason`) |
| `kept` | `edges`, `statements`, `pairs`, `entities` kept in all; `negated_edges` among the edges; `misdirected_edges` (below) |
| `rules` | Per rule id, statement and provenance rules alike. Query and capped: `tier`, `matched_edges`, `matched_statements`, and the `edges`, `statements` and `pairs` kept; capped rules add `dropped_by_min_score` (`statements`, `edges`) and `dropped_by_cap` (`pairs`, `statements`, `edges`). Skip: `tier`, `skipped_edges`. A statement counts under its provenance rule if it has one, else under its statement rule, with all its edges: so a statement rule's counts leave out the statements a provenance rule took, and include the lowered edges of the statements it kept in its own tier |
| `skipped` | One entry per (subject category, predicate, object category) not kept, using each node's first (most specific) category: `edges` and `rule` (`skip:<id>` or `unmatched`), most edges first |

Read the largest `unmatched` entries: one that states a claim a paper could contradict (a
predicate or category no rule lists) may deserve a rule.

`kept.misdirected_edges` counts the kept edges whose predicate doesn't read from their subject
to their object the way Biolink's domain, range and canonical direction put it
(`biolink.oriented`), such as 'gene condition_associated_with_gene disease'; the step warns
when there are any. They're checked as written, but the label step reads claims Biolink's way
round ('gene gene_associated_with_condition disease'), so claims rarely reach them. Report them
to the transformation team.
