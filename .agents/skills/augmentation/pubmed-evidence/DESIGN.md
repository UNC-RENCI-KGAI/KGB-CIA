# Design: PubMed evidence for the graph's edges

Why the augmentation agent works the way it does: what each step does and the choices behind
it. `SKILL.md` is the workflow an agent follows; `reference/` has the file and rule formats;
`README.md` says how to run it.

Step 6 follows ten rules the team adopted on 2026-11-06. Step 6 below states each of them and
gives its number ("rule 2"), and the code cites them by those numbers.

## Plan: PubMed evidence for existing edges

1. **select:** keep the edges whose types are worth checking in the literature, and group them
   by node pair.
2. **pubmed:** search PubMed for each pair and fetch the abstracts.
3. **label:** an LLM reads each pair's abstracts and writes down the claims each one makes
   about the pair. Code checks every claim against Biolink and the abstract.
4. **ground:** resolve the anatomy the claims name to UBERON terms, and pin UBERON's release.
5. **review:** a stronger model reads again every claim that would count: each one that would
   add a PMID to an edge or propose a more specific edge.
6. **apply:** code refines each checked edge from its pair's claims and writes the graph. An
   edge the abstracts support gains their PMIDs, and each more specific statement they make is
   proposed as a new edge beside the originals, which stay.

The LLM only extracts and reviews claims, and code refines. Every claim, check, verdict and
repair is cached, so step 6 can rebuild the graph without calling a model, as the team rule
"agents decide, code transforms" asks. The literature only adds to the graph: PMIDs on the
edges it supports, and edges more specific than them. Nothing is removed, negated or replaced,
and every addition names the claims it rests on.

## How the agent runs

The agent is this skill. `SKILL.md` is the workflow an agent (Pi or Claude Code) follows, and
it drives the steps one at a time. Each step is a subcommand of `scripts/augment.py`, backed by
one module of the library in `scripts/pubmed_evidence/`. The agent runs the label step on a
sample, reads the results, tunes the prompt, then runs the full set. It calls the models listed
in `config.MODELS`: the Azure OpenAI deployments Pi uses, and Claude models on Anthropic's own API.
`kgb-augment` (`agents/augmentation/cli.py`) runs the same library. With no subcommand it runs
every step with its defaults, which is what `shared/pipeline.py` does.

- **Bulk extraction:** step 3 calls a cheap model directly rather than through subagents. A
  subagent carries the harness's system prompt and tool definitions and re-sends its context
  every turn, which costs far more per abstract. `kgb-augment` also has to run from
  `shared/pipeline.py`, which can't start subagents.
- **Second pass:** a stronger model reviews every claim that would count (step 5), and upholds
  or rejects each one. The script calls the model directly, as step 3 does, so `kgb-augment`
  runs without Pi.
- **Choosing the models:** a curator's gold set decides which models are good enough
  (`evaluate`, below). Candidates for the bulk pass are gpt-6-luna (the default),
  gpt-5.6-luna, DeepSeek-V4-Flash and gpt-oss-120b. Candidates for the second pass are
  gpt-5.6-terra (the default) and gpt-6.1-sol. The Claude candidates are claude-haiku-5-5 for
  the bulk pass (the price of gpt-6-luna), and claude-sonnet-5-5 and claude-opus-5-5 for the
  second.
- **One model table:** every model a step can call is in `config.MODELS`. Each entry has its
  API, endpoint and key variable, and its prices per million tokens (input, output, cache read
  and write, and any longer-prompt tier) with where and when they were copied from. It also has
  the output cap and reasoning effort a call sends unless `--max-output-tokens` or
  `--reasoning-effort` overrides them: `low` for the Azure bulk candidates, `medium` for the
  rest, and Anthropic's own default for each Claude model until tuned. A model not in the table
  is an error. The Azure prices are copied from `.pi/models.json`, which Pi reads for itself,
  so a price change there has to be copied by hand. The Claude prices are Anthropic's list
  prices, from its pricing page.
- **Claude models** go through Anthropic's Messages API and its SDK (`llm.AnthropicClient`),
  with `ANTHROPIC_API_KEY` or an `ant auth login` profile. A model's `backend` in the table
  picks its client, and the system prompt carries a prompt-cache breakpoint. Sonnet and Opus
  ask for Anthropic's server-side fallback (`fallbacks: "default"`). A request that a safety
  classifier declines (`bio` is among the categories, and biomedical abstracts may trip it) is
  run again on a model Anthropic picks by the refusal's category, and each attempt is priced by
  its own model. The model that answered is recorded with every answer and verdict
  (`answered_by`). The label and review summaries count the calls a fallback answered, and
  `evaluate` leaves those answers out of the requested model's scores. Haiku has no fallback,
  so its refusals are failed calls, retried on the next run.

## Step 1: Which edges to query

Choose edges by (subject category, predicate, object category), matching on predicate ancestry
so that child predicates the transform picks are covered, and by the edge's `knowledge_level`
and `agent_type`. A relationship qualifies when abstracts state it as a claim and a paper could
contradict it.

| Tier | Subject → predicate → object | Main TCRD sources |
| --- | --- | --- |
| Query | ChemicalEntity/Drug → `treats`, `contraindicated_in`, `treats_or_applied_or_studied_to_treat` → Disease | DrugCentral |
| Query | ChemicalEntity → `affects`, `directly_physically_interacts_with`, `binds` → Gene/Protein | DrugCentral, ChEMBL, IUPHAR |
| Query | Gene → `associated_with` (and everything below it: `gene_associated_with_condition`, `genetically_associated_with`, `associated_with_increased_likelihood_of`, `correlated_with`, `biomarker_for`, ...), `contributes_to` (and `causes`) → Disease | DisGeNET, JensenLab, OMIM, Monarch (ClinGen), GWAS Catalog |
| Query, capped | Gene ↔ `physically_interacts_with`, `interacts_with` ↔ Gene | STRING, BioPlex, Reactome |
| Query, capped | Gene → `has_phenotype` → PhenotypicFeature | IMPC, MGI, HPO via Monarch |
| Skip | Gene → `participates_in` → Pathway; Gene → `enables`, `participates_in`, `located_in` → GO terms | Reactome, KEGG, WikiPathways, GO |
| Skip | Gene → `expressed_in` → AnatomicalEntity/Cell | GTEx, HPA, Jensen TISSUES |
| Skip | `orthologous_to`, `has_part`, `subclass_of`, `member_of` | Orthologs, Pfam, PANTHER, DTO |
| Skip | `occurs_together_in_literature_with` | Literature co-occurrence |
| Skip | any edge with `knowledge_level: text_co_occurrence` or `agent_type: text_mining_agent`, whatever its predicate | eRAM's text mining (`gene_associated_with_condition`) |

- **Capped:** gene–gene interactions are numerous and symmetric, and many come from a single
  high-throughput screen. Query only pairs above a confidence cutoff, and dedupe unordered
  pairs. Phenotype names are long and varied, so expect low recall.
- **Skipped:** pathway and GO names rarely appear verbatim in abstracts, and GO annotations
  already carry their own references and evidence codes. Expression edges are measurements
  rather than claims and are very numerous. Literature co-occurrence isn't a claim, and
  checking it against PubMed would be circular. It sits under `associated_with`, so its skip
  rule comes first. Nor is an edge a text-mining pipeline made, whatever predicate it carries:
  its evidence is the literature itself. eRAM writes 113,706 co-mentions as
  `gene_associated_with_condition` with `knowledge_level: text_co_occurrence`, and the Nov 12
  2026 run put 127,774 of its 131,195 pairs in the query tier that way, so two skip rules on
  the edge's provenance come before every rule on its statement. The rest are true by
  construction.

Group the kept edges by unordered node pair, since one search serves edges in both directions.

- **Matching:** "Gene" and "Gene/Protein" match `biolink:GeneOrGeneProduct`, so proteins
  NodeNorm didn't conflate into genes still match. "ChemicalEntity/Drug" matches
  `biolink:ChemicalEntity`. An edge also matches in reverse when Biolink marks its predicate
  symmetric (Protein `binds` chemical).
- **Negated source edges** are kept like any other, as their own statement. A claim's
  statements are never negated (a negative claim denies a positive statement), and nothing is
  narrower than a negated statement, so step 6 checks a negated edge and never supports it.
- **Misdirected edges:** a kept edge whose predicate doesn't read from its subject to its object
  the way Biolink's domain, range and canonical direction put it ('gene
  condition_associated_with_gene disease') is counted and warned about. It is checked as
  written, but claims are read Biolink's way round (step 3), so they rarely reach it. On
  2026-11-05 no in-scope edge of Monarch or DrugCentral was misdirected.
- **Scores** for the caps come from the edge's `score`, `confidence_score`,
  `has_confidence_score`, `combined_score` or `stringdb_combined_score`, never from the
  `lit_support_score` that step 6 writes. Without a score, a pair ranks last.
- The rules are data in `scripts/pubmed_evidence/scope.py`; `--rules FILE` replaces them
  (`reference/rules.md`).

## Step 2: PubMed query

The search (`search_pairs`), the E-utilities client and the PubMed cache are `shared.pubmed`'s,
which every agent that reads PubMed shares (the repository's `shared/AGENTS.md`). Step 2 hands
the search the pairs and entities step 1 wrote, and writes what it found for each pair to
`pubmed.jsonl`. What follows is how that search works, and why.

### Source PMIDs

Every in-scope pair is searched, even when its edges arrive with PMIDs in `publications`
(likely GeneRIF, ChEMBL activity references and DrugCentral references; the TCRD columns still
need checking). Those PMIDs are always fetched and read as well, even when they fall outside
the search's top results. This matters most for ChEMBL compound–target edges, where a name
search finds little.

### What step 2 records for each pair

- The query string, retrieval date, and ESearch total hit count.
- Each fetched PMID with its origin (`search`, `source` or `source+search`, so it is read once)
  and its relevance rank.
- The title, abstract and year of each fetched PMID, and its PubMed publication types
  (`Journal Article`, `Review`, `Retracted Publication`, ...), in the PubMed cache. An abstract
  cached before the types were recorded has none, so step 2 fetches it again.

The cap on abstracts per pair (default 20 by relevance, configurable) limits how many supporting
PMIDs an edge can collect. The total hit count is kept as a separate signal.

Synonyms are the node's name, symbol and synonyms, then the labels NodeNorm gives its
equivalent identifiers, at most 8 per node. MeSH headings come from labeled `MESH:D…`
identifiers. A pair with a node that has no usable term isn't searched, but its source PMIDs
are still fetched and read.

### API: NCBI E-utilities

ESearch finds the PMIDs and EFetch retrieves the abstracts.

- **Complete:** it searches all of current PubMed, and its `[tiab]` and `[mh]` field tags let a
  query combine synonyms from our ID libraries with MeSH terms where NodeNorm supplies MESH IDs.
- **Covers every entity type above.** PubTator3 indexes only genes, chemicals, diseases,
  variants, species and cell lines.
- **Favors recall:** an abstract that states nothing about the pair gets no claims in step 3,
  so a broad search costs only tokens. PubTator3's main advantage, matching entities by ID,
  matters less when the LLM is given each entity's synonyms.

Query shape:

```text
("<A synonym>"[tiab] OR "<A synonym>"[tiab] OR <A MeSH>[mh])
  AND ("<B synonym>"[tiab] OR <B MeSH>[mh])
  AND hasabstract
sort=relevance, retmax=20
```

- **Rate limits:** 3 requests a second without an API key and 10 with one (one key per NCBI
  account). The client sends `tool` and `email` with every request, and reads the key and
  email from `NCBI_API_KEY` and `NCBI_EMAIL`. Without the key the step says so at its start,
  and stops before sending anything when more than `KEYLESS_SEARCHES` (500) searches aren't
  cached, unless `--without-key`: the Nov 12 2026 run searched 2,957 pairs in 42 minutes
  without a key and 500 in 75 seconds with one, because one shell lacked it.
- **Throughput:** one ESearch per pair plus one EFetch per ~200 deduplicated PMIDs, so roughly
  25,000 pairs an hour with a key and 4,000 without, as measured on Nov 12 2026. ESearch
  returns at most 10,000 IDs per query.
- **Off-peak:** NCBI asks that large jobs run on weekends or between 9pm and 5am ET on weekdays.

Kept in reserve:

- **Europe PMC REST:** the same abstracts plus open-access full text, with abstracts returned by
  the search itself. Switch if we need full text or the NCBI limits start to hurt.
- **A local PubMed baseline index:** no rate limits and a fixed snapshot, but worth building only
  if the pair count reaches the millions.
- **PubTator3:** a possible cross-check for gene, chemical and disease pairs. Its precomputed
  relation types (treat, inhibit, cause, associate and others) could check step 3's claims.

## Step 3: Claims

The label step is the extractor. Step 6 never reads abstracts: it refines from the claims
alone, so every refinement can be reproduced and audited.

### One request per pair

- **Per pair, not per edge.** One request per unordered node pair (`graphio.pair_id`), with its
  abstracts in chunks of 20 (`--abstracts-per-call` can lower that). The nodes go as `X` and
  `Y`, with name, id, most specific category, synonyms and MeSH headings. The edges are never
  sent, so one pair's claims serve every edge between the pair, from any graph, now or later.
- **Per abstract:** a study design (one of `StudyDesign`'s eleven values) and zero or more
  claims. No claims means the abstract states nothing about the pair.
- **A claim** is one finding about X and Y: its `polarity` (`positive` or `negative`), its
  `statement` (subject `X` or `Y`, a predicate, `qualified_predicate: causes` or none, and
  qualifiers), `also_supports` (further positive statements the same finding shows, such as
  `in_clinical_trials_for` from a null phase 3 trial), `claim_role` (`primary_finding`,
  `background`, `hypothesis`), `hedged`, `negative_finding_type` (`demonstrated_absence`,
  `failed_to_detect`), a `context` (species, anatomy, notes) and a verbatim
  `evidence_sentence`.
- **Context:** the species comes from an enum (human, mouse, rat, zebrafish, fly, worm, yeast,
  dog, pig, macaque, rabbit, `other`), mapped to NCBITaxon in code (`config.SPECIES_TAXA`;
  `other` names no species, and the organism goes in the notes). The anatomy is the name as
  the abstract writes it, which step 4 grounds. Population, cell line, dose, formulation,
  endpoint and time point go in the notes, as free text, and are never part of a statement.
  Biolink wants a CURIE for `population_context_qualifier`, and there is no settled vocabulary
  for one.
- **Stored as given.** Code stores each claim with the pair's CURIEs in place of X and Y and
  each statement's object made explicit. `claim_id` hashes the stored claim, and the claim's
  check, review and repair hang on that id, so a stored claim is never rewritten: groundings,
  repairs and verdicts are kept beside it.
- **The prompt:** `prompts/label.md` is the extractor contract, with its five rules
  (explicit negatives, claim at the level stated, split mixed results, ground the entities,
  quote verbatim). The domain rules (what counts as a negative, treatment and comparators,
  which entity a finding is about, genes and qualifiers) are in `prompts/claim-rules.md`, which
  both the label and the review prompt include, so the reviewer judges by the same rules.

### Which predicates, and which way round

- **Allowed predicates:** the predicates of the scope rules a pair matches, everything Biolink
  puts below them (mixins included), and their `opposite_of` partners. Predicates that a
  category-free skip rule covers are left out: `occurs_together_in_literature_with` sits below
  `associated_with`, and co-mention would otherwise support gene–disease edges.
- **Read the way Biolink reads them.** Each predicate is turned to read from the rule's
  subject to its object by its `domain` and `range` (`biolink.oriented`): one defined the other
  way round becomes its `inverse`, a non-canonical one becomes its canonical inverse, and one
  that fits neither way is dropped. So gene–disease pairs never get
  `condition_associated_with_gene` or `has_biomarker`, and chemical–gene pairs never get
  `has_adverse_event` or `enables`.
- **What fits:** a predicate's constraints are its own domain and range plus its inverse's
  range and domain. A missing constraint, a range that isn't a class, or a node with no known
  category constrains nothing. A category fits a class when their descendant sets overlap
  (mixins included), and of a node's categories only the most specific count, any of which may
  fit. So imprecise categories never drop a claim.
- **After the call.** The reply schema has one predicate enum for both directions, so the model
  can still put the wrong node first. Code reads every statement, the claim's own and each
  `also_supports` entry, Biolink's way round (`refine.directed`). A statement that fits as
  written stays. A symmetric predicate that fits the other way stays, and step 6 compares it
  either way round. Otherwise the predicate becomes its inverse, with the same subject and
  object. Last, a non-canonical predicate is turned round to its canonical inverse, so
  'disease gene_associated_with_condition gene' becomes 'gene gene_associated_with_condition
  disease'. Without that last turn such claims would never reach the graph's edges, which use
  canonical predicates. A statement that fits neither way fails its claim. The stored claim
  keeps the model's wording; the review and step 6 read the directed statements, and so every
  edge step 6 proposes is canonical and reads Biolink's way round.

### Checked in code

A claim that fails any check is invalid as a whole: it stays in the cache and counts for
nothing, and its errors go to the `claim_checks` table and the label summary. The checks are
recomputed on every run for the run's pairs, since nothing versions them.

- The predicate fits the pair's nodes one way round or the other (above).
- Each statement's qualifier slots and values are allowed for its predicate's association
  between these entities. Subject and object qualifiers (aspect, direction, form or variant,
  part, derivative) describe a gene, gene product or chemical, so they are never allowed on a
  disease or phenotype, and a chemical–disease statement gets none. `qualified_predicate` comes
  only with a subject or object qualifier, and Biolink validates the set.
- `qualified_predicate: causes` comes only with an aspect or a direction.
- `negative_finding_type` is on negative claims only. A negative claim may lack one; the
  summary counts these.
- Each `also_supports` entry adds something: a positive claim's own statement doesn't already
  entail it, and it doesn't entail what a negative claim denies.
- The evidence sentence is in the abstract (`quotes.quote_found`). This one is kept apart
  (`sentence_valid`), since the repair pass (below) can still supply the sentence.

The association class comes from `pubmed_evidence/biolink.py`, not `bmt.get_associations`,
which matches far too loosely for these edges (it offers `GeneToGoTermAssociation` for
gene–disease). It finds the classes from each class's inherited subject, predicate and object
ranges instead. Context qualifiers whose values are ontology terms (anatomical, species,
population) are never in the reply's qualifiers: models make up CURIEs, so species and anatomy
come in the claim's context instead.

### Quotes and repair

- **The check:** `quote_found` compares after Unicode NFKC, lower case, collapsed whitespace,
  plain dashes and quote marks, brackets removed and outer punctuation stripped. A quote may
  join up to three spans with `...`, each at least 20 characters, so two entity names joined by
  an ellipsis don't pass.
- **Repair:** the label step ends with a repair pass (`repair.py`; `--no-repair` skips it).
  Each claim that passes every other check, but whose sentence wasn't found, gets one small
  call to the review model (`config.REPAIR_MODEL`) with the abstract, the claim and the failed
  sentence. The reply is a verbatim quote or null, and code checks it the same way. Under the
  earlier statement labels, about 1% of decisive quotes needed it. A repair supplies a sentence
  and never changes the claim, and null is a normal answer, since a model asked for a quote may
  grab a loosely related sentence.
- **Which repairs count:** repairs are cached per claim under the repairer and its prompt
  version, one answered attempt each, and a failed call is retried on the next run. Review,
  step 6 and `evaluate` use the repairs `label_run.json` names, as it names the review, and
  warn when the repair prompt or model has changed since. A repaired sentence is flagged on the
  claim record (`evidence_sentence_repaired`) and counted in the apply report.

### Structured replies

The reply is strict structured output, not JSON dug out of text: each call sends a JSON Schema,
and the service returns an object that fits it. Every role, design, species, predicate and
qualifier value is an enum, so code checks only what a schema can't say (above).

- **One schema per set of scope rules.** A pair's schema allows the predicates of every rule it
  matches. Pairs of the same rules share a schema, and with it the cached prompt prefix: the
  service's prompt cache holds the system prompt and the schema, at a tenth of the price.
- **Every enum-valued qualifier slot** Biolink has is in one object, each its values or
  `none`; which apply is checked in code. Statements are defined once (`$defs`), for a claim's
  own and its `also_supports`.
- **At most 16 unions.** Anthropic's strict output rejects a request whose schemas have more
  than 16 parameters with union types (`anyOf`, which nullable fields are, or a type array).
  So the qualifier slots take `none` rather than null: 13 nullable slots, counted again in
  `also_supports`, would be over it. The schema has 5 nullable fields (`qualified_predicate`,
  `negative_finding_type` and the three context fields), 6 counting `also_supports`; a test
  counts every reply schema's.
- **Positions, not ids:** abstracts are `A1`...`A20` in the order the request lists them, and
  code maps them back to PMIDs. A position beyond the request's, or an abstract answered twice,
  fails the call. An abstract the reply leaves out stays unanswered and is asked again next
  run; it is counted, not a failed call.
- **The prompt version** hashes the prompt with the files it includes, `FORMAT_VERSION`, every
  non-skip rule's schema and the reasoning effort, so one run has one version, and runs on two
  graphs with the same rules share it. A deployment that doesn't support strict output fails
  with its own error rather than falling back to JSON mode.
- The message shows each abstract's PubMed publication types, which helps the model's design
  answer.

### Study design and publication data

- **One design per abstract,** not per claim. PubMed's publication types set it where they name
  one, and the extractor's answer is used where they don't. Where several match, the one
  `StudyDesign` lists first wins.

  | PubMed publication type | study_design |
  | --- | --- |
  | Meta-Analysis, Network Meta-Analysis | `meta_analysis` |
  | Systematic Review | `systematic_review` |
  | Randomized Controlled Trial, Equivalence Trial, Pragmatic Clinical Trial | `rct` |
  | Observational Study, Clinical Trial and its phases, Controlled Clinical Trial | `cohort` |
  | Case Reports | `case_report` |
  | Review, Editorial, Comment, Letter | `narrative_review` |

  The veterinary trials and observational studies (animal studies), trial protocols and scoping
  reviews name none, so the extractor decides those (`config.PUBMED_DESIGNS`).
- **Nothing PubMed returns about a paper is dropped.** Each abstract's record keeps the
  publication types verbatim, a coarse article type (`supporting_document_type`: `opinion`,
  `review`, `guideline`, `case_report`, `research` or `other`), the year, retraction (the
  "Retracted Publication" type), the design and what set it (`study_design_source`), and the
  extractor's own design even when PubMed overrode it. So claim rates can be compared by
  publication type without fetching again or calling the model. Of all this, step 6 reads only
  the retraction (rule 9); the rest is recorded for readers of the evidence.

## Step 4: Grounding anatomy

- **Exact names only.** Each anatomy name is looked up in the RENCI Name Resolver
  (`NAMERES_URL`) among anatomical entities, and the matches are normalized with NodeNorm. The
  name grounds to the first match whose label or a synonym *is* the name, case and whitespace
  aside, and whose NodeNorm clique has an UBERON term. So a near match never grounds a name, and
  neither does a cell type (CL) or a cell line. Checked live: liver, kidneys, serum, plasma,
  prefrontal cortex and lymph nodes ground; hepatocytes, T cells, HeLa cells and tumour margin
  don't. Precision beats recall here, since a grounded name narrows the claim's statements, and
  so the edges they support and propose: "skin" grounds to skin epidermis, and there is no
  plural folding.
- **Ungrounded names** stay on the claim as text and go to `lit_context_notes`. They narrow no
  statement, and neither do population, cell line, dose or time point.
- **Names never looked up** are another matter: a claim whose name has no groundings row (its
  lookup failed, or no ground run came after the label run) would count in every context with
  nothing said. Step 6 holds its pair, counted and warned of in the apply report, until a
  ground run looks the name up; then the pair is refined.
- **Cached** by name in the PubMed cache's `groundings` table, misses too, so a rerun asks
  nothing; `--refresh` looks them up again. A failed lookup isn't cached. The step fails if
  more than 5% of names fail.
- **UBERON's release is pinned.** The step fetches `uberon-basic.obo` from the release
  `config.UBERON_URL` pins (2026-11-05) into the cache directory, once, and records its
  `data-version` in `label_run.json` (`uberon_version`). Step 6 builds the `is_a`/`part_of`
  closure from it at load: about 15,000 live terms and 453,000 ancestor pairs in 0.1 s, with a
  small parser and no new dependency. To change the release, change the pin and delete the
  file. A grounded term that isn't live in the pinned release is warned about; it is at or
  below only itself.
- **At or below:** one anatomy term is at or below another when they're equal or the closure
  lists the other among its ancestors (`refine.Lattice.at_or_below`). The liver is below the
  digestive system; the liver and the kidney are unrelated. Species have no hierarchy and
  compare by equality. So a claim in the liver supports an edge in the digestive system, or
  one with no anatomy, but a claim with no anatomy doesn't support an edge in the liver
  (step 6).

## Step 5: Review

- **Every claim that would count.** A claim would count, once upheld, when its quote passed
  the check, its paper isn't retracted, it isn't a `hypothesis`, and one of its statements is
  narrower than or equal to an in-scope edge of its pair (`refine.would_count`; step 6 below
  says what narrower means). That takes in the claims that only add a PMID to an edge as well
  as those that propose one, and a negative claim through the positive statements it
  `also_supports` (rule 8). A claim that supports no edge of its pair, a broader, sibling or
  contrary statement say, isn't sent: it changes nothing. Nor is one whose only such statement
  is support-only for that edge (rule 11, `ameliorates_condition` under `treats`): step 6
  counts it without review, unless `config.REVIEW_SUPPORT_ONLY` or `--review-support-only`
  sends it.
- **One pass.** Each claim's effect is independent of the others', so the step sends, once,
  every claim that would count and has no verdict of this reviewer and prompt version yet. A
  claim whose call fails, or that a reply leaves out, stays unreviewed: step 6 holds its pair,
  and the next run sends it again. A rejected claim is never sent again.
- **Verdicts:** `upheld` or `reject`, with a reason. A rejected claim counts for nothing. The
  reviewer judges the whole claim, `also_supports` included: does the abstract state this, at
  this level, with this polarity? It rejects a claim the abstract doesn't state, or only poses
  as a hypothesis or a question, whatever its `claim_role` (rule 10; a hedged finding, or
  background the abstract asserts, is not a hypothesis); one about safety, cost or approval
  status; one with the opposite direction, about the wrong entity, or more specific than the
  abstract; a negative claim without explicit language; and a species or tissue the abstract
  contradicts, since a claim's context narrows its statements. A claim that says less than the
  abstract is upheld.
- **The same bundles as step 6.** Review and step 6 both build each pair's claims through
  `bundle.py`, so the claims review sends are the ones step 6 would count. Review reads the
  input graph (`--input`) for that.
- **The item:** the claim and its `also_supports` in words, the same claim as JSON in the
  extractor's vocabulary (species by name, anatomy as written, statements read Biolink's way
  round), and its evidence sentence, the repaired one if it was repaired. Each abstract is sent
  once per call, with its year and publication types.
- **Batching:** items go in batches per pair, split by abstract chunk (20), then at 40 items a
  call.
- **Versions:** the review prompt version hashes the prompt with its includes,
  `FORMAT_VERSION`, the reply schema and the reasoning effort, so another effort reviews
  afresh. `label_run.json` names the reviewer, its prompt version and its effort, and the full
  run continues them while the same reviewer reviews.
- **Failed calls:** the step returns 1, and leaves `label_run.json` as it was, when more than
  5% of the calls the claims that would count need have failed. Those are this run's calls
  plus the calls the claims reviewed before took (`calls_needed`). Counting only this run's
  calls would let one call that keeps failing fail every later review run, and with it the
  full pipeline.

## Step 6: Refining the edges

### What a pair brings

- **Checked edges:** an edge is checked when select kept its statement and its pair is fully
  labeled: every abstract step 3 asks about has an answer of the run, and every claim of those
  answers has been checked. A pair with no abstracts is checked too, and its edges are
  unsupported. A pair with a failed call, or with claims a label run stored but stopped before
  checking, leaves its edges unchanged until a label run finishes it; it is never refined on
  part of its claims.
- **Evidence:** one record per abstract read for the pair, with its claims. Claims that failed
  a check are left out and counted. Anatomy is grounded through step 4's cache, and every
  statement is read Biolink's way round. Each claim carries the verdict of the review
  `label_run.json` names, if any, and whether PubMed marks its paper retracted.
- **The pair's other edges,** those select left out, come too. They aren't checked, but a
  proposal one of them already makes links to it (below).

`bundle.py` gathers all of this per pair from the work files and the caches, in memory, since
claims are per pair.

### Refinement (`refine.py`)

A pure module, with no I/O and no model calls, implementing the rules below for one pair. Its
order on statements is the one step 5 uses to find the claims that would count.

- **Narrower.** S1 is narrower than or equal to S2 (S1 ⊑ S2) when they join the same subject
  and object, S1's predicate is S2's or below it (mixins included), and every qualifier S2
  sets, S1 sets to the same value or one below it: by the enum's `is_a` (upregulated below
  increased, expression below abundance), by predicate for `qualified_predicate`, by the
  UBERON closure for anatomy (step 4), and by equality for species and any other slot. S1 may
  set more, and an added qualifier makes it narrower. A symmetric statement compares either way
  round. *Strictly narrower* is narrower and not the same statement. Nothing is narrower than a
  negated statement, and a negated statement is narrower than nothing.
- **A claim's statements** (`refine.statements_of`): a positive claim's statement and each
  `also_supports` entry; a negative claim's `also_supports` entries alone, since what it denies
  supports nothing (rule 8: a failed trial's "X in_clinical_trials_for Y" counts like a
  positive claim). Each statement gains the claim's grounded species and anatomy as
  `species_context_qualifier` and `anatomical_context_qualifier` where its association allows
  the slot; elsewhere the context neither narrows nor blocks. An anatomy name that didn't
  ground adds nothing and becomes a context note.
- **Which claims count** (rules 1, 9 and 10). A claim counts when its quote passed the check,
  its paper isn't a Retracted Publication, its role isn't `hypothesis`, and review upheld it
  (or, for a support-only statement, rule 11, wasn't rejected). Nothing else filters claims:
  there are no weights and no thresholds, for `treats` as for any other predicate. A claim's
  study design, hedging, background role and finding type are
  recorded on the evidence and filter nothing.
- **Support and PMIDs** (rules 2 and 3). An in-scope edge E is supported when at least one
  counting claim has a statement narrower than or equal to E's. Its supporting PMIDs are those
  claims' distinct PMIDs, and their number is E's support score (`lit_support_score`), the
  PMIDs E already cites included. Those E doesn't cite are appended to its `publications`. A
  threshold such as "supported by 2+ abstracts" is one filter on that score; none is built.
- **Proposals** (rule 4). Each statement of a supporting claim that is strictly narrower than
  E is proposed: "X increases Y expression in the liver" under "X affects Y".
  - There is one proposal per distinct statement the abstracts make, not every combination the
    hierarchy allows, and one per pair however many claims make it or originals it refines.
  - Its PMIDs are those of the claims that make exactly its statement, and their number is its
    score. A claim for a narrower statement adds its PMID to that narrower proposal, not to a
    broader one, though every original it is narrower than takes the PMID.
  - `original_edge_ids` names every original it refines.
  - It must be a valid Biolink edge between its nodes: its predicate fits them by domain and
    range, and its association allows each slot and value it sets. One that isn't is dropped,
    and its claims still support E. Only a gold claim, which skips label's checks, can make
    an invalid one.
- **Links** (rule 5). A proposal whose statement an edge of the pair already makes isn't
  written as a new edge: E names that edge in `lit_refined_by` instead. An in-scope edge it
  links to is supported by its claims already (rule 2); an edge select left out is written as
  read, not duplicated. A symmetric statement is the same either way round
  (`refine.statement_key`): 'MDM2 interacts with TP53' links to 'TP53 interacts with MDM2'.
- **Ignored claims** (rule 6) change nothing: what a negative claim denies, contrary claims,
  and claims that support no edge of the pair (broader, sibling or other-branch statements).
  They stay in the cache. An unsupported E is written unchanged, with score 0 and no
  proposals. Step 6 only adds PMIDs and proposed edges; it never removes, negates or replaces
  an edge.
- **Support only** (rule 11). A statement narrower than E only by a predicate
  `config.SUPPORT_ONLY` lists under E's predicate, or one below it, supports E but is never
  proposed under it. The table has one entry: `ameliorates_condition` under `treats`. Label
  tells them apart ("relieves B's symptoms without the abstract presenting it as a
  treatment"), but the graph doesn't need the narrower edge, and in DrugCentral it was 700 of
  the 1,186 proposals. Such a statement adds its PMID to E like any other narrower statement.
  - **Review.** Off by default (`config.REVIEW_SUPPORT_ONLY`): a claim isn't sent to review for
    a support-only statement, and step 6 counts that statement without a verdict, so it holds
    no pair. A rejected claim still counts for nothing, so verdicts from an earlier review
    still apply. Review's `--review-support-only` sends such claims, and apply's counts them
    only once upheld; give both steps the flag, since apply holds a pair whose claim review
    didn't send.
  - **Only under the listed predicate.** Under a broader edge, such as
    `treats_or_applied_or_studied_to_treat`, `ameliorates_condition` is an ordinary narrower
    statement: it is proposed, and needs review.
- **Species in proposals** (rule 7) sit behind a flag, `config.SPECIES_IN_PROPOSALS`, off by
  default; apply's `--species-in-proposals` turns it on for a run. Almost every in-scope
  association allows a species, so with it on almost every supported edge whose claims name
  one proposes "E in human".
  - **Off:** refinement proposes as above, and step 6 then takes `species_context_qualifier`
    out of the proposals and links (`refine.without_species`). One that refines an original
    with that same species keeps it, so that it stays narrower than that original, and still
    refines its other originals, those without a species too: "X increases Y expression in
    human" refines "X affects Y in human" and "X affects Y". Those that become the same
    statement merge, with the union of their PMIDs and their `original_edge_ids`. One that
    becomes an original it refines ("E in human" becomes E) leaves that original's
    refinements, and is dropped if it refines nothing else: its claims support E already. One
    that becomes another edge of the pair, in scope or not, is a link to it, as a claim
    without the species would make it: a proposal that becomes an in-scope edge E2 is a link
    from the broader original E1 to E2. Dropping it instead would make E1's `lit_refined_by`
    depend on whether an abstract named a species. The report counts every species-qualified
    proposal and link folded this way (`species_folded`).
  - **On:** a species refines like any other qualifier.
  - **Either way,** the originals' support and PMIDs are the same, and claims are reviewed and
    cached whatever the flag says, so flipping it needs no model call. Say E is "A affects B",
    abstract 1 says "A increases B activity" and abstract 2 says the same in human. Off, there
    is one proposal, "A increases B activity", with PMIDs 1 and 2; on, there are two, the
    second in human. E gains both PMIDs either way.
- **Order.** The output doesn't depend on the claims' order. A symmetric proposal is written as
  its first valid reading with the nodes in id order: Biolink gives a chemical–gene symmetric
  statement its anatomy, aspect and direction only with the chemical as subject.
- **Holds.** A pair is held while a claim that would count has no verdict of the review
  `label_run.json` names (a review run clears it), or one of its claims names anatomy step 4
  never looked up (a ground run clears it). A held pair's edges pass through as read, and the
  report counts them and warns of each kind of hold, so a partial run never writes a partial
  answer. A name that was looked up and didn't ground holds nothing: it is a context note.
  `apply --no-review`, for testing only, counts unreviewed claims instead of holding their
  pairs; a rejected claim counts for nothing either way. A support-only statement (rule 11)
  holds nothing unless `--review-support-only`.
- **Tests:** six properties, each over 300 seeded random claim sets, run with the species kept
  and with it taken out: adding a counting claim never removes a PMID, a supported edge or a
  proposal; the claims' order doesn't change the output; ignored claims change nothing; no
  proposal duplicates an edge of the pair; every proposal is strictly narrower than each
  original it refines, as written, species included, and is canonical and valid; and a
  proposal's PMIDs are exactly those of the claims whose statements propose it. A seventh
  checks the fold with the species taken out: each proposal is the merge of those refinement
  made that say its statement once rule 7 has taken their species out, and the originals'
  support and PMIDs don't change.

### What step 6 writes

Nothing is removed, and an input edge keeps every slot. Its `publications` gain the supporting
PMIDs its source didn't cite, after the source's own. They are listed again in
`lit_added_publications`, so the source's own citations can be told apart, and a rerun takes
them back out.

- **A checked edge** comes out as an `AugmentedEdge` (`shared/schema.py`): the record as read,
  slot for slot, with those PMIDs appended, `augmentation: checked`, `original_edge_ids` (its
  own id) and:
  - `lit_support_score` and `lit_supporting_publications`: its supporting PMIDs and their
    number;
  - `lit_added_publications`: those appended to its `publications`;
  - `lit_refined_by`: the proposed edges more specific than it, and the edges of the pair its
    links name;
  - `lit_context_notes`: its supporting claims' notes and ungrounded anatomy names;
  - `lit_evidence`: one `PublicationEvidence` per abstract read for the pair, claims or not,
    each claim with `supports` (whether it supports this edge), and `found_by` (`source` if
    this edge's own publications, as its source cited them, cite the PMID; `search` if step
    2's search found it).

  An unsupported edge is written the same way, with score 0, empty lists and its
  `publications` as read, so "checked, nothing found" reads apart from "never checked".
- **A proposed edge** is our own claim. Its id is `kgbcia-augmentation:` plus a hash of its
  statement's key (its id, read with the nodes in id order if it's symmetric), so the same
  statement in two graphs, either way round, gets one id, and `original_edge_ids` names every
  original it refines. It has `augmentation: proposed`, `primary_knowledge_source:
  infores:kgbcia-augmentor` with `supporting_data_source: [infores:pubmed]` (set in
  `config.py`; a list, since Biolink makes the slot multivalued, and an input edge's own value,
  a list or a bare CURIE, is kept as read), `knowledge_level: knowledge_assertion`,
  `agent_type: text_mining_agent`, and as `publications` the PMIDs of the claims that make it.
  Its `lit_*` fields are its own score, PMIDs, notes and evidence; there `supports` marks the
  claims that make it, and `source` the PMIDs its originals cite. It is never negated, and it
  carries no `kgbcia_source_records`.
- **Refinement records:** `work/refinements.jsonl` has one line per refined pair, by pair id:
  the number of abstracts read for it; each in-scope edge's statement, support, score,
  supporting and added PMIDs and `refined_by`;
  the proposed edges; and the links, each with the edges it names (`linked_to`). Held and
  unchecked pairs get no line, and neither do folded proposals, which only the report counts.
- **The report:** `work/apply_report.json` counts the edges checked, supported, unsupported and
  refined, the PMIDs added, the proposed edges, links and folded proposals, the held pairs and
  their edges, the edges in scope left unchecked, and the claims used, counting, upheld,
  rejected and unreviewed. `reference/work-files.md` lists its fields.
- **Unchecked edges** pass through as read, apart from their `id`: those out of scope, those in
  a held pair, those in a pair not fully labeled after a failed call or a label run stopped
  before its checks (the next label run finishes them), and those in a pair step 2 didn't
  search. The report counts each kind.
- **Ids:** every input edge gets an `id`: its own, or `kgbcia-edge:` plus the first 16 hex
  digits of the SHA-256 of its canonical JSON (sorted keys, the record as read). The hash
  changes if the transformation's output for the edge does, which is acceptable until the
  transformation stage assigns stable ids. Identical records hash to one id and share one
  outcome. Two different records with one id stop the step (and review) with an error naming
  their lines: step 6 writes an id's outcome on every edge that has it, so the second would
  get the first's.
- **Records are written back as read:** the same keys in the same order with the same values,
  nulls included (only the JSON's whitespace may differ), with the `id` added after them where
  an edge had none, a checked edge's supporting PMIDs appended to `publications`, and its own
  fields after that.
- **Feeding the stage its own output adds nothing twice.** On an edge an earlier run wrote (it
  carries `augmentation` or a `lit_*` field), every field step 6 has ever written, the `lit_*`
  fields and the earlier evidence fields alike, is stripped, and the PMIDs it appended
  (`lit_added_publications`) are taken back out of `publications`, before the edge is refined
  again. Any other edge keeps every field as its source wrote it, even one named like ours
  (`evidence`, `evidence_score`, `original_edge_ids`, ...): stripping those from every edge
  would delete a source's own data, so we decided to strip only on a rerun (review of #22). Edges an earlier run added (`kgbcia-augmentation:`) are dropped,
  and this run adds its own. Step 1 never selects them.
- **One bad edge never stops the run.** An edge that isn't a valid `Edge` record (a predicate
  without `biolink:`, a knowledge source without `infores:`, a missing subject, ...) is skipped
  by select and the bundle, and written exactly as read by apply, unchecked. Both reports list
  it under `invalid_edges` (by reason, with the first lines), and each step prints a warning;
  the run and the pipeline go on. Removing bad edges is the final cleaning pass's job, not
  augmentation's. Only a problem with the input as a whole (a line that isn't JSON, a missing
  file) stops the step.
- **All or nothing:** the graph and `refinements.jsonl` are written under temporary names and
  renamed into place at the end, so a failed run leaves none half-written; `apply_report.json`
  follows them.

## Choosing the model: the gold set

`augment.py evaluate` scores a run against a curator's gold set, and calls no model. The gold
file (`reference/formats.md`) has two kinds of line: a curator's claims for one abstract of a
pair, in the shape the label step stores them, and an edge of such a pair to score. An edge
line needs no label: its expected outcome is step 6's refinement on the curator's claims (the
gold side), so a curator judges only what the abstracts say. Each run's checked claims are
refined the same way (the run side), and the two sides are compared:

- **Claims:** each gold abstract's claims against the run's, each matching at most one on the
  other side. A match is the same polarity and the same directed statement, exactly, or up to
  an ancestor (the run's statement the curator's or one it entails). A symmetric statement
  matches written either way round ('MDM2 interacts with TP53' is 'TP53 interacts with MDM2').
  Context, role, hedging, finding type and `also_supports` aren't part of a match; their effect
  shows in the scores below. Scored as the run answered, and as checked (without the claims
  that failed a check or whose sentence wasn't found, even after repair).
- **Support:** whether each gold edge is supported on each side: accuracy, Cohen's κ, the
  confusion matrix, and precision, recall and F1 per label.
- **PMIDs:** each edge's supporting PMIDs on the run's side against the gold side's: micro
  precision, recall and F1 over the edges. A wrong PMID puts a paper that doesn't say so in an
  edge's `publications`.
- **Proposals:** each pair's proposals and links, by statement key: micro precision, recall and
  F1 over the pairs. A wrong proposal is a false edge in the graph.
- **Disagreements:** the edges whose PMIDs differ, with those missing and extra, and the pairs
  whose proposals differ, with the statements missing and extra.

Both sides read the same abstracts: an edge is refined on its pair's gold abstracts only, and
on the run's side only where the run's own model answered all of them. An abstract a fallback
model answered is left out, with every edge of its pair, and so is a gold edge whose pair has no
gold abstract. Both sides count claims as step 6 does, but without review verdicts, so the
scores are the extractor's and candidate models compare evenly: claims from a retracted paper
(PubMed's publication types, from the PubMed cache) and hypotheses don't count, and the species
comes out of the proposals unless `config.SPECIES_IN_PROPOSALS` or `--species-in-proposals`
keeps it. Every gold edge is in scope, since the gold set names no other edges of its pairs: a
proposal step 6 would link to an edge select left out is a proposal here, and proposals and
links are scored together by key, so the scores are the same. Node categories come from the
work directory's `entities.jsonl`, publication types from the PubMed cache, anatomy through the
groundings cache. Step 4 grounds only the names of the run `label_run.json` names, so evaluate
grounds the others (the curator's, another run's) the same way, through the Name Resolver and
NodeNorm, and caches them; the report counts the names grounded, those not, and those whose
lookup failed. Cost comes from the calls the label cache logged.

The gold pairs are drawn stratified by outcome and predicate family, so that unsupported
edges, supported ones and those with proposals all appear; a curator writes the claims of their
abstracts, and each candidate model labels the same pairs. Two gold tools serve this:

- **`evaluate --draw N [--seed S]`** samples N pairs from step 6's `refinements.jsonl` into
  `work/gold_pairs.jsonl`. A stratum is an edge's outcome (`unsupported`; `supported`, with
  nothing more specific; or `refined`, supported with proposals or links) and its predicate
  family, the scope rule that kept the edge (matched on its nodes as select matches it), and a
  pair is in the strata of all its edges. The strata take turns, the smallest first, each
  drawing a pair not yet drawn, in an order the seed shuffles: each gets about as many pairs as
  the others, or all it has. A pair with no abstracts read is left out, and counted: its edges
  are unsupported for want of anything to read, a curator has nothing to read, and `label
  --pairs` would skip it. Drawing needs a refined run, the default model's at first.
- **`label --pairs FILE`** labels exactly the pairs a file names, if select kept them (it lists
  those it can't label), and, like `--sample`, writes `label_sample.md` and
  `label_sample.jsonl` and leaves `label_run.json` alone. A curator turns the latter's lines
  into gold abstract lines and adds edge lines; each candidate model labels the same file.

## Caching

Two SQLite caches under `data/`, kept out of git and shareable across a team (`KGB_AUGMENT_CACHE_DIR`):

- **PubMed:** `shared.pubmed`'s cache, the file `KGB_PUBMED_CACHE` names (default
  `data/cache/pubmed.sqlite`), which every agent that reads PubMed shares: ESearch results
  keyed by query string, with the retrieval date; abstracts keyed by PMID, so an abstract
  shared by several pairs is fetched once and reruns make no requests; and NodeNorm entries.
  The skill adds one table of its own, the anatomy groundings, by name. PubMed changes over
  time, so the query string and retrieval date stay with the evidence.
- **Claims:** in the cache directory (`--cache-dir`, default `data/augmentation/cache`), with
  the pinned `uberon-basic.obo` beside it. Each abstract's answer for a pair, keyed by (pair,
  PMID, prompt version, model), with its design and claims; each claim's checks; the review's
  verdicts and the sentence repairs, keyed by the claim's id and their own model and prompt
  version; and a log of every call's tokens and cost. A rerun asks only for what has no answer
  yet, and a new prompt version starts fresh. Step 6 reads this cache, the publication types in
  the PubMed cache and the work files, so the graph can be rebuilt without calling a model.
- **Prompt versions** hash the prompt file's text with the files it includes, the step's
  `FORMAT_VERSION` (the version of the request layout), its reply schema or schemas and the
  reasoning effort sent. Any edit to a prompt or a schema, or another effort, starts fresh
  answers; bump `FORMAT_VERSION` when the code that writes the request changes.
- **Earlier tables:** a label cache from before the claims also has the statement labels'
  tables (`labels`, `proposals`, `reviews`, `repairs`). No code reads or writes them, and they
  stay as they are.

## Cost

Estimates, until the tuning sample measures them. The last relabel of both graphs, under the
earlier statement labels, cost $2.12 (Monarch) and $11.14 (DrugCentral) for labeling and review
together. Claims are larger replies (they carry `also_supports`, context and role), so expect
about 1.5 times that: about $20 for both graphs, plus about $1 for a tuning sample. Review
sends every claim that would count, those that only add a PMID to an edge included, so its
share grows with the number of supported edges; the tuning sample measures it too. Grounding
calls are free. The dry run's estimate assumes
2,000 output tokens a call (`label.ESTIMATED_OUTPUT_TOKENS`), a guess the tuning sample will
replace. If a 20-abstract call's 95th percentile of output tokens exceeds 70% of the model's
16,000-token cap, reasoning included, `ABSTRACTS_PER_CALL` drops to 10.

## Not used: embeddings

Embeddings capture topic but not polarity: "A inhibits B" and "A does not inhibit B" come out
almost the same, so they can't tell support from refutation. They could trim abstracts before
step 3, but the step 2 query already requires both entities, and at bulk-model prices trimming
saves little. Revisit if we move to full text and need to choose which paragraphs to send.

## Open items

- **Not run yet:** the tuning sample and the relabel of both graphs on claims. The sample
  measures output tokens per abstract (`ESTIMATED_OUTPUT_TOKENS`, `ABSTRACTS_PER_CALL`), how
  often a claim is invalid as a whole for one bad `also_supports` entry, how often the model
  puts the wrong node first, and how many claims review gets. Then the comparison pages are
  rewritten around refinements.
- **A support threshold:** an edge is supported from one abstract on. "Supported by 2+
  abstracts" would be one filter on `lit_support_score`, here or in the cleaning stage; the
  gold set can show whether one abstract is precise enough.
- **Species in proposals** are off by default (rule 7). Turned on, almost every supported edge
  whose claims name a species gains a proposed "E in human", "E in mouse" and so on.
- **Support-only claims go unreviewed** by default (rule 11). An unreviewed
  `ameliorates_condition` claim adds its PMID to its `treats` edge: in DrugCentral's run,
  review upheld 1,309 of 1,470 such claims, and they added 1,255 PMIDs.
- **Unstated qualifiers:** an original with a qualifier no claim can state, such as
  `frequency_qualifier`, `onset_qualifier`, a population or a disease context, can never be
  supported. The checked Monarch and DrugCentral edges carry no qualifiers today.
- **Not built:** Cohen's κ between two curators' gold sets. `evaluate.metrics` can compute it
  from two gold files' support outcomes if wanted.
- **One predicate enum for both directions:** the reply schema doesn't offer each subject its
  own predicates, so the model can still name the wrong node first. The check after the call
  turns or drops such statements, but a per-subject schema would stop them at the source.
- All teams: anything that ignores `negated` reads a negated edge as a positive link, including
  GNN link prediction and MCP neighbor queries. Negated edges come from source data (step 6
  never proposes one), so we need a team-wide rule, such as dropping `negated: true` edges
  before training.
- Whether proposed edges carry their originals' `kgbcia_source_records`. They carry none for
  now.
- Filter team: which of these edge types the filter keeps by default, and how many there are.
  This sets the scale. And the filter's output directory: `kgb-augment` reads `data/filter` by
  default.
- Transformation team: whether TCRD's PMID columns are carried into `publications`.
- Who holds the NCBI API key.
- Score scales differ by source (STRING's raw combined score is 0–1000), so one `--min-score`
  for both capped rules is crude.
- Recall: MeSH supplementary concepts (`MESH:C…`, many ChEMBL compounds) could be searched
  with `[nm]`, and gene symbols under 3 characters (F2, C3) are dropped from queries.
- Deferred until the cleaning or MCP stage needs them: flat fields beside `lit_evidence` for a
  store such as Neo4j that can't hold a list of objects, and attaching the evidence through
  Biolink's `has_supporting_studies`.
- Claude models have run only offline here: the label schema, whose qualifier object lists
  every enum-valued slot, has yet to meet Anthropic's structured-output limits, and how often
  Haiku refuses biomedical abstracts is unmeasured. The gold-set comparison measures both.
- Message Batches (half price, results within a day) would halve a Claude bulk run, but need a
  submit-then-collect flow.
