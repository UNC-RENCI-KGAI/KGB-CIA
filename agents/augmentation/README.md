# Augmentation agent

**Team:** @UNC-RENCI-KGAI/augmentation · **Existing asset:** PubMed; Bagel / LitCoin for text

Enrich the baseline graph with extra datasets and PubMed evidence for or against edges.

## Plan: PubMed evidence for existing edges

1. Read the filtered baseline graph: the filter stage's `nodes.jsonl` and `edges.jsonl`.
2. Keep the edges whose types are worth checking in the literature, and group them by node pair.
3. Query PubMed for each pair and fetch the matching abstracts.
4. Write each distinct edge between the pair as an English sentence.
5. Give an LLM the pair's edge sentences and abstracts. It labels every abstract against every
   edge and proposes qualifiers the abstracts support.
6. Apply the labels to the graph in code: add supporting PMIDs to the original edges, and add new
   edges for refutations and qualifiers.

The LLM only labels; code does everything else. Its labels are cached, so step 6 can rebuild the
graph from them without calling a model, as the team rule "agents decide, code transforms" asks.

## How the agent runs

The agent is a Pi skill in `.agents/skills/augmentation/` that drives Python scripts, one per
step. It runs step 5 on a sample, reads the results, tunes the prompt, then runs the full set.
It uses the RENCI Azure models in `.pi/models.json` for now.

- **Bulk labeling:** the step 5 script calls a cheap model directly rather than through
  subagents. A subagent carries the harness's system prompt and tool definitions and re-sends
  its context every turn, which costs far more per label. `kgb-augment` also has to run from
  `shared/pipeline.py`, which can't start subagents.
- **Second pass:** a stronger model reviews only the `contradicted` labels and proposed
  qualifiers, the labels that add edges. They are a small share of the labels. The agent can run
  this pass as a Pi subagent; the script also calls the model directly, so `kgb-augment` runs
  without Pi.
- **Choosing the models:** a gold set of 50–100 hand-labeled (edge, abstract) pairs decides
  which models are good enough. Candidates for the bulk pass are gpt-6-luna, gpt-5.6-luna,
  DeepSeek-V4-Flash and gpt-oss-120b, roughly $14–30 per 10,000 pairs at about 9k input and 1k
  output tokens per pair. Candidates for the second pass are gpt-5.6-terra and gpt-6.1-sol.

## Step 2: Which edges to query

Choose edges by (subject category, predicate, object category), matching on predicate ancestry
so that child predicates the transform picks are covered. A relationship qualifies when
abstracts state it as a claim and a paper could contradict it.

| Tier | Subject → predicate → object | Main TCRD sources |
| --- | --- | --- |
| Query | ChemicalEntity/Drug → `treats`, `contraindicated_in`, `treats_or_applied_or_studied_to_treat` → Disease | DrugCentral |
| Query | ChemicalEntity → `affects`, `directly_physically_interacts_with`, `binds` → Gene/Protein | DrugCentral, ChEMBL, IUPHAR |
| Query | Gene → `gene_associated_with_condition`, `genetically_associated_with`, `causes`, `contributes_to` → Disease | DisGeNET, JensenLab, OMIM, Monarch, GWAS Catalog |
| Query, capped | Gene ↔ `physically_interacts_with`, `interacts_with` ↔ Gene | STRING, BioPlex, Reactome |
| Query, capped | Gene → `has_phenotype` → PhenotypicFeature | IMPC, MGI, HPO via Monarch |
| Skip | Gene → `participates_in` → Pathway; Gene → `enables`, `participates_in`, `located_in` → GO terms | Reactome, KEGG, WikiPathways, GO |
| Skip | Gene → `expressed_in` → AnatomicalEntity/Cell | GTEx, HPA, Jensen TISSUES |
| Skip | `orthologous_to`, `has_part`, `subclass_of`, `member_of` | Orthologs, Pfam, PANTHER, DTO |

- **Capped:** gene–gene interactions are numerous and symmetric, and many come from a single
  high-throughput screen. Query only pairs above a confidence cutoff, and dedupe unordered
  pairs. Phenotype names are long and varied, so expect low recall.
- **Skipped:** pathway and GO names rarely appear verbatim in abstracts, and GO annotations
  already carry their own references and evidence codes. Expression edges are measurements
  rather than claims and are very numerous. The rest are true by construction.

Group the kept edges by unordered node pair, since one search serves edges in both directions.

## Step 3: PubMed query

### Source PMIDs

Every in-scope pair is searched, even when its edges arrive with PMIDs in `publications`
(likely GeneRIF, ChEMBL activity references and DrugCentral references; the TCRD columns still
need checking). Those PMIDs are always fetched and labeled as well, even when they fall outside
the search's top results. This matters most for ChEMBL compound–target edges, where a name
search finds little.

### What step 3 records for each pair

- The query string, retrieval date, and ESearch total hit count.
- Each fetched PMID with its origin (`search`, `source` or `source+search`, so it is labeled
  once) and its relevance rank.
- The title and abstract of each fetched PMID.

The cap on abstracts per pair (default 20 by relevance, configurable) limits how many supporting
PMIDs an edge can collect. The total hit count is kept as a separate signal.

### API: NCBI E-utilities

ESearch finds the PMIDs and EFetch retrieves the abstracts.

- **Complete:** it searches all of current PubMed, and its `[tiab]` and `[mh]` field tags let a
  query combine synonyms from our ID libraries with MeSH terms where NodeNorm supplies MESH IDs.
- **Covers every entity type above.** PubTator3 indexes only genes, chemicals, diseases,
  variants, species and cell lines.
- **Favors recall:** step 5 labels any abstract that doesn't bear on an edge `no evidence`, so
  a broad search costs only tokens. PubTator3's main advantage, matching entities by ID, matters
  less when the LLM is given each entity's synonyms.

Query shape:

```text
("<A synonym>"[tiab] OR "<A synonym>"[tiab] OR <A MeSH>[mh])
  AND ("<B synonym>"[tiab] OR <B MeSH>[mh])
  AND hasabstract
sort=relevance, retmax=20
```

- **Rate limits:** 3 requests a second without an API key and 10 with one (one key per NCBI
  account). Send `tool` and `email` with every request; read them from `NCBI_API_KEY` and
  `NCBI_EMAIL`.
- **Throughput:** one ESearch per pair plus one EFetch per ~200 deduplicated PMIDs, so roughly
  30,000 pairs an hour with a key. ESearch returns at most 10,000 IDs per query.
- **Off-peak:** NCBI asks that large jobs run on weekends or between 9pm and 5am ET on weekdays.

Kept in reserve:

- **Europe PMC REST:** the same abstracts plus open-access full text, with abstracts returned by
  the search itself. Switch if we need full text or the NCBI limits start to hurt.
- **A local PubMed baseline index:** no rate limits and a fixed snapshot, but worth building only
  if the pair count reaches the millions.
- **PubTator3:** a possible cross-check for gene, chemical and disease pairs. Its precomputed
  relation types (treat, inhibit, cause, associate and others) could check step 5's labels.

## Step 4: Edge sentences

Code writes each edge as a sentence, following Biolink's
[reading order for qualifier-based statements](https://biolink.github.io/biolink-model/reading-a-qualifier-based-statement/):
the subject with its qualifiers, then `qualified_predicate` (or `predicate` when there is none),
then the object with its part, form or variant, aspect and direction, then any context. An
`affects` edge with `qualified_predicate: causes`, `object_aspect_qualifier: expression` and
`object_direction_qualifier: increased` reads "A causes increased expression of B".

- **Predicate phrases** are written by hand, one per in-scope predicate. There are about 15, and
  Biolink's slot names don't always read well (`treats_or_applied_or_studied_to_treat`).
- **Names:** nodes are named by their `name`. The LLM also gets the structured edge and the
  synonyms used in the step 3 query, so it can tell that "STI571" is imatinib.
- **Duplicates** are labeled once. Sources often give the same edge (DisGeNET and JensenLab both
  give `gene_associated_with_condition`), so each distinct statement (subject, predicate,
  qualifiers, object) is labeled once and the result applies to every edge that makes it.

## Step 5: LLM labels

One call per pair, not per edge: abstracts are most of the tokens, so each is sent once for all
of the pair's edges. The call answers two questions.

### Does each abstract support or refute each edge?

Each (edge, PMID) gets one of four labels:

| Label | Meaning |
| --- | --- |
| confirmed | The abstract explicitly supports the edge |
| contradicted | The abstract explicitly refutes the edge |
| ambiguous | The abstract discusses the pair, but its evidence is unclear or mixed |
| no evidence | The abstract does not bear on the edge |

- **Quotes:** every `confirmed` or `contradicted` label comes with a verbatim quote from the
  abstract. Code checks that the quote appears in that PMID's abstract and drops the label if it
  doesn't, which catches made-up evidence cheaply.
- **Contradicted** means an explicit negative finding ("A did not inhibit B"). An abstract that
  mentions both entities but says nothing about the relationship is `no evidence`.
- **Direction:** an abstract that reports the opposite direction to an edge's ("A decreases B"
  against "A causes increased expression of B") is `ambiguous` for that edge, not
  `contradicted`. Its direction is proposed as a qualifier instead (below), so it becomes a new
  positive edge. The same abstract can still confirm an unqualified "A affects B".

### Can qualifiers be added?

The LLM proposes qualifiers where the abstracts say more than the edge does: a direction (A
affects expression of B → A causes increased expression of B), a subject or object aspect
(A affects B → A upregulation affects B), a mechanism, and so on. The full list is in
[Biolink's qualifiers](https://biolink.github.io/biolink-model/qualifier/).

- **Allowed values:** the [Biolink Model Toolkit](https://biolink.github.io/biolink-model-toolkit/)
  (`bmt`) gives the edge's association class from
  `get_associations(subject_categories, predicates, object_categories)`, and the class fixes
  which qualifiers and values are allowed. The prompt lists only those, and code validates the
  answer with `validate_qualifier` and `is_permissible_value_of_enum`.
- **Causal or associative:** directions usually come with `qualified_predicate: causes` (see
  Biolink's
  [examples](https://biolink.github.io/biolink-model/association-examples-with-qualifiers/)), so
  the LLM also says whether the abstract claims causation or only association.
- **Qualifiers already on an edge** are part of its sentence, so step 5 checks them along with
  the rest of the edge. An edge can arrive with them if the metadata stage maps a source column
  (such as an action type) to a qualifier; nothing documents that yet.

## Step 6: Applying the labels

Nothing in the baseline graph is changed except `publications`, and nothing is removed.
Conflicts are left to the cleaning stage, which can weigh each edge by its publications.

| Finding | Change to the graph |
| --- | --- |
| `confirmed` | The PMID is added to the original edge's `publications` |
| `contradicted` | A new edge with the original's subject, predicate, object and qualifiers, `negated: true`, and the contradicting PMIDs in `publications` |
| Qualifiers proposed, including a different direction | A new edge with the original's subject, predicate and object, the new qualifiers, and the supporting PMIDs in `publications` |
| `ambiguous`, `no evidence` | None; kept in the label cache |

- **One abstract is enough** for a new edge. Nothing is overwritten, and the second pass has
  already reviewed it.
- **One new edge per claim:** a single negated edge per original statement, carrying every
  contradicting PMID, and a single qualified edge per distinct set of qualifiers.
- **`negated`** is Biolink's slot for an association that "is not true". The metadata stage
  already uses it for negative rows in source data.
- **Every new edge** has `knowledge_level: knowledge_assertion`, `agent_type: text_mining_agent`
  and `primary_knowledge_source: infores:kgbcia-placeholder`. The placeholder stands in until the
  leads choose the team's infores ID; it is set in one place and can be overridden on the
  command line.

## Caching

Two SQLite caches under `data/`, shared on the RENCI drive since data stays out of git:

- **PubMed:** ESearch results keyed by query string, with the retrieval date, and abstracts keyed
  by PMID, so an abstract shared by several pairs is fetched once and reruns make no requests.
  PubMed changes over time, so the query string and retrieval date stay with the evidence.
- **LLM labels:** keyed by (edge statement, PMID, prompt version, model), with the quotes and
  proposed qualifiers. A rerun sends only PMIDs that have no label yet, and a new prompt version
  starts fresh. Step 6 reads only this cache, so the graph can be rebuilt without calling a
  model.

## Not used: embeddings

Embeddings capture topic but not polarity: "A inhibits B" and "A does not inhibit B" come out
almost the same, so they can't tell support from refutation. They could trim abstracts before
step 5, but the step 3 query already requires both entities, and at bulk-model prices trimming
saves little. Revisit if we move to full text and need to choose which paragraphs to send.

## Open items

- Filter team: which of these edge types the filter keeps by default, and how many there are.
  This sets the scale.
- Transformation team: whether TCRD's PMID columns are carried into `publications`.
- Who holds the NCBI API key.
- Leads: the team's infores ID, to replace `infores:kgbcia-placeholder`.
- All teams: anything that ignores `negated` reads a negated edge as a positive link, including
  GNN link prediction and MCP neighbor queries. Negated edges also come from source data, so we
  need a team-wide rule, such as dropping `negated: true` edges before training.
- Whether new edges carry the original edge's `kgbcia_source_records`.

## Run it alone

```bash
uv run kgb-augment --input <previous stage output> --output <path>
```

## Input / output

Uses the KGX records in `shared/schema.py`.

- **Input:** the filtered baseline graph, as `nodes.jsonl` and `edges.jsonl`.
- **Output:** the same graph with supporting PMIDs added to `publications`, plus the new negated
  and qualified edges.

Describe the exact files and paths here once decided.

## Status

See the tracking issue for this team on GitHub. Replace this section with what works, what
doesn't, and how to try it.
