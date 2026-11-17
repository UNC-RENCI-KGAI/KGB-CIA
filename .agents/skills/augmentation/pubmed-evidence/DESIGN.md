# Design: PubMed evidence for the graph's edges

What each step of the augmentation agent does, the rules it applies, and why. `SKILL.md` is the
workflow an agent follows, `README.md` lists every option, and `reference/` holds the scope
rules (`rules.md`), the work files and caches (`work-files.md`) and the gold-set and reply
formats (`formats.md`). This document points to them rather than repeating them.

Terms: a **pair** is the unordered pair of nodes an edge joins. A **statement** is what an edge
says without its provenance: subject, predicate, object, qualifiers and `negated`. A **claim**
is one finding an abstract states about a pair. An edge is **in scope** when step 1 kept its
statement.

Step 6 follows eleven numbered rules. "Step 6" below gives each its number ("rule 2"), and the
code cites them by those numbers.

## Plan: PubMed evidence for existing edges

1. **select:** keep the edges whose types are worth checking in the literature, and group them
   by pair.
2. **pubmed:** search PubMed for each pair and fetch the abstracts.
3. **label:** an LLM reads each pair's abstracts and writes down the claims each one makes
   about the pair. Code checks every claim against Biolink and the abstract.
4. **ground:** resolve the anatomy the claims name to UBERON terms, and pin UBERON's release.
5. **review:** a second pass reads again every claim that would count: each one that would add
   a PMID to an edge or propose a more specific edge.
6. **apply:** code refines each checked edge from its pair's claims and writes the graph. An
   edge the abstracts support gains their PMIDs, and each more specific statement they make is
   proposed as a new edge beside the originals, which stay.

The LLM only extracts and reviews claims; code refines. Every claim, check, verdict and repair
is cached, so step 6 rebuilds the graph without calling a model, as the team rule "agents
decide, code transforms" asks. The literature only adds to the graph. Nothing is removed,
negated or replaced, and every addition names the claims it rests on.

## How the agent runs

Each step is a subcommand of `scripts/augment.py`, backed by one module of the library in
`scripts/pubmed_evidence/`. `kgb-augment` (`agents/augmentation/cli.py`) runs the same library.

- **The full run** (no subcommand) is what `shared/pipeline.py` calls; `SKILL.md`, "The full
  run without you", has the details. It continues the run `label_run.json` names, so a tuned
  run is reused from the cache. It caps labeling by the dry run's estimate (`--budget` stops
  the run; `--max-label-cost`, default `config.FULL_RUN_LABEL_BUDGET`, $20, labels the first
  pairs that fit). It runs label once more when pairs are left not fully labeled, then warns
  and goes on, with their edges unchecked: a call that fails the same way every time would
  otherwise stop every run.
- **Bulk extraction:** step 3 calls a cheap model directly rather than through subagents. A
  subagent carries the harness's system prompt and tool definitions and re-sends its context
  every turn, which costs far more per abstract, and `shared/pipeline.py` can't start one.
- **Second pass:** step 5 reviews with the bulk model again at a higher effort: gpt-6-luna at
  `medium` (`config.REVIEW_MODEL`, `config.REVIEW_EFFORT`). gpt-5.6-terra costs 18-24x more,
  and `review --model gpt-5.6-terra` still reviews with it. The measurements:
  - On 6,953 claims (2026-11-16), luna at medium agreed with terra at Cohen's kappa 0.47-0.51
    and caught about half of terra's rejects.
  - In a blind read of 33 records, luna agreed with the human reader on 27, and terra on 19.
  - On a random 2,000-pair sample of the 2026-11-17 label run, luna at medium reviewed 1,780
    claims for $0.26 and rejected 9.2%.
- **Choosing the models:** a curator's gold set decides ("Choosing the model: the gold set").
  Bulk candidates: gpt-6-luna (the default), gpt-5.6-luna, DeepSeek-V4-Flash, gpt-oss-120b and
  claude-haiku-5-5. Review candidates: gpt-6-luna at medium (the default), gpt-5.6-terra,
  gpt-6.1-sol, claude-sonnet-5-5 and claude-opus-5-5.
- **One model table:** every model a step can call is in `config.MODELS`; any other is an
  error. An entry has its API, endpoint, key variable and prices, and the output cap and
  reasoning effort a call sends unless `--max-output-tokens` or `--reasoning-effort` overrides
  them. The Azure prices are copied by hand from `.pi/models.json`, which Pi reads for itself.
- **Reasoning effort:** the table's default is `low` for the Azure bulk candidates and
  `medium` for the other Azure models; review sends `config.REVIEW_EFFORT` whatever the model.
  `none` is an effort and is sent as such; `omit` sends no setting, so the service's default
  applies. They differ: gpt-6-luna doesn't reason at `none`, and reasons at medium with no
  setting (probed 2026-11-16). An effort a model's `efforts` doesn't list is an error before
  any call.
- **Claude models** go through Anthropic's Messages API (`llm.AnthropicClient`), with a
  prompt-cache breakpoint on the system prompt. Sonnet and Opus ask for Anthropic's
  server-side fallback: a request a safety classifier declines is run again on another model,
  each attempt priced by its own model. Every answer and verdict records the model that
  answered (`answered_by`), and `evaluate` leaves a fallback's answers out of the requested
  model's scores. Haiku has no fallback, so its refusals are failed calls.
- **Parallel calls:** label, quote repair and review send calls from a pool of
  `config.WORKERS` (32) threads. On Azure gpt-6-luna, 32 gave 1.84x the throughput of 16
  (2026-11-16), near the resource's 7.5M tokens a minute. Cache writes and Biolink lookups stay
  on the main thread.
- **The 5% rule:** a step that calls a service returns 1 when more than 5% of its work failed,
  since the graph shouldn't be built on a service that is down. The work is pairs in step 2,
  anatomy names in step 4 (`shared.pubmed.MAX_FAILED_SHARE`) and calls in steps 3 and 5
  (`llm.MAX_FAILED_SHARE`). Steps 3 and 5 count failed calls against all the calls the work
  needs, those earlier runs answered included (`llm.too_many_failed`): counting only this
  run's would let one stuck call fail every later run. A run that fails this way leaves
  `label_run.json` as it was.

## Step 1: Which edges to query

An edge qualifies when abstracts state its relationship as a claim that a paper could
contradict. Rules choose edges by (subject category, predicate, object category), matching on
Biolink ancestry, and by the edge's `knowledge_level` and `agent_type`.

| Tier | Relationships |
| --- | --- |
| Query: keep all | chemical `treats` or `contraindicated_in` a disease or phenotype; chemical `affects`, `binds` or `interacts_with` a gene or protein; gene `associated_with` or `contributes_to` a disease |
| Capped: keep the best-scored pairs | gene–gene interactions; gene `has_phenotype`; gene–trait associations; text-mined `located_in` a cellular component; any other kept edge mined from the literature |
| Skip | gene to pathway or GO term; `expressed_in`; `orthologous_to`, `has_part`, `subclass_of`, `member_of`; `occurs_together_in_literature_with` |

`reference/rules.md` has the built-in rules (`scope.DEFAULT_RULES`) with their order, how a
rule matches an edge, how a cap ranks pairs, and the `--rules FILE` format that replaces them.

- **Capped:** gene–gene interactions are numerous and symmetric, and many come from one
  high-throughput screen. Phenotype and trait names are long and varied, so the search finds
  few. A capped rule keeps its 10,000 best-scored pairs (`config.CAPPED_LIMIT`).
- **Text-mined edges** (`knowledge_level: text_co_occurrence` or `agent_type:
  text_mining_agent`) are capped in caps of their own, whatever rule keeps them. They are the
  least trustworthy edges in the graph and carry no publications or supporting sentence, so the
  cleaning stage's evidence check can't judge them: this step is the only one that can.
  Checking them isn't circular, since step 3 asks whether an abstract states the relationship,
  not whether it mentions both entities. The cap bounds the cost: on the Nov 12 2026 run, which
  skipped only literature co-occurrence, eRAM's co-mentions (written as
  `gene_associated_with_condition`) were 127,774 of the 131,195 pairs in the query tier.
- **Pair counts** in the cleaned Nov 9 2026 graph, before any cap: 25,148 drug-target
  `interacts_with` pairs (query); 920 DrugCentral indications to a phenotype (query); 564,098
  TIGA gene–trait pairs (capped); and 4,782,056 COMPARTMENTS text-mined locations, the only
  text-mined edges in that graph (capped at 10,000).
- **Skipped:** pathway and GO names rarely appear verbatim in abstracts, and GO annotations
  carry their own references and evidence codes. Expression edges are measurements, not claims.
  Literature co-occurrence isn't a claim, and checking it against PubMed would be circular.
  The rest are true by construction.
- **Two kinds of rule:** a statement rule names predicates and picks edges by what they say. A
  provenance rule names only a knowledge level or an agent type, and lowers the tier of edges a
  statement rule picked. A lowered edge keeps its statement rule, whose predicates its pair's
  claims may use, so a provenance rule changes no reply schema and no prompt version.
- **Cap ties,** unscored pairs among them, go to the pair with the most distinct primary
  knowledge sources, then by a hash of the pair id, so that an unscored cap isn't the pairs
  whose CURIEs sort first (`scope.cap_rank`). Scores never come from step 6's
  `lit_support_score`.
- **Negated source edges** are kept like any other, as their own statement. Nothing is
  narrower than a negated statement, so step 6 checks a negated edge and never supports it.
- **Misdirected edges:** a kept edge whose predicate doesn't read from its subject to its
  object the way Biolink puts it ('gene condition_associated_with_gene disease') is counted and
  warned about. It is checked as written, but claims are read Biolink's way round (step 3), so
  they rarely reach it.

## Step 2: PubMed query

The search (`search_pairs`), the E-utilities client and the PubMed cache are `shared.pubmed`'s,
which every agent that reads PubMed shares (the repository's `shared/AGENTS.md`). Step 2 hands
it the pairs and entities step 1 wrote, and writes what it found to `pubmed.jsonl`.

### Source PMIDs

Every in-scope pair is searched, even when its edges arrive with PMIDs in `publications`.
Those PMIDs are fetched and read as well, even when they fall outside the search's top
results: for ChEMBL compound–target edges a name search finds little. The exception is a paper
that a hundred pairs or more cite ("Source papers many pairs cite").

### What step 2 records for each pair

The query string, retrieval date and ESearch hit count, since PubMed changes over time; and
each fetched PMID with its origin (`search`, `source` or `source+search`) and relevance rank.
Titles, abstracts, years and publication types go in the PubMed cache
(`reference/work-files.md`).

- **Abstracts per pair:** at most 20 from the search, by relevance (`--max-abstracts`), with
  source PMIDs on top. This limits how many supporting PMIDs an edge can collect, so the hit
  count is kept as a separate signal.
- **Search terms:** the node's name, symbol and synonyms, then the labels NodeNorm gives its
  equivalent identifiers, at most 8 per node; MeSH headings from its `MESH:D…` identifiers. A
  pair with a node that has no usable term isn't searched, but its source PMIDs are still read.

### API: NCBI E-utilities

ESearch finds the PMIDs and EFetch retrieves the abstracts.

```text
("<A synonym>"[tiab] OR "<A synonym>"[tiab] OR "<A MeSH heading>"[mh])
  AND ("<B synonym>"[tiab] OR "<B MeSH heading>"[mh])
  AND hasabstract
sort=relevance, retmax=20
```

- **Why E-utilities:** it searches all of current PubMed and covers every entity type above
  (PubTator3 indexes only genes, chemicals, diseases, variants, species and cell lines). It
  favors recall, which costs only tokens: an abstract that states nothing about the pair gets
  no claims in step 3.
- **Rate limits:** 3 requests a second without `NCBI_API_KEY` and 10 with it. Without the key
  the step says so, and stops before sending anything when more than `KEYLESS_SEARCHES` (500)
  searches aren't cached, unless `--without-key`: on Nov 12 2026 a shell without the key took
  42 minutes for 2,957 pairs, against 75 seconds for 500 with one.
- **Throughput:** one ESearch per pair plus one EFetch per 200 PMIDs: roughly 25,000 pairs an
  hour with a key and 4,000 without (measured Nov 12 2026).
- **Off-peak:** NCBI asks that large jobs run on weekends or 9pm-5am ET on weekdays; the step
  warns when more than 1,000 uncached searches start at peak hours.
- **In reserve:** Europe PMC (the same abstracts plus open-access full text), a local PubMed
  baseline (worth building only for millions of pairs), and PubTator3's precomputed relations
  as a cross-check of step 3's claims.

## Step 3: Claims

The label step is the extractor. Step 6 never reads abstracts: it refines from the claims
alone, so every refinement can be reproduced and audited.

### One request per pair

- **Per pair, not per edge.** One request per pair, with its abstracts in chunks of 20
  (`config.ABSTRACTS_PER_CALL`; `--abstracts-per-call` can lower it). The nodes go as `X` and
  `Y`, with name, id, category, synonyms and MeSH headings. The edges are never sent, so one
  pair's claims serve every edge between the pair, from any graph, now or later.
- **The category shown** is the node's most specific (`biolink.shown_category`), not its
  first: the cleaned graph lists categories alphabetically, so the first is `BiologicalEntity`
  for every gene, disease and phenotype.
- **Per abstract:** a study design and zero or more claims. No claims means the abstract
  states nothing about the pair.
- **A claim** is one finding about X and Y (`reference/formats.md` has the reply format): a
  `polarity`; a `statement` (subject `X` or `Y`, a predicate, `qualified_predicate: causes` or
  none, and qualifiers); `also_supports`, further positive statements the same finding shows
  (`in_clinical_trials_for` from a null phase 3 trial); a `claim_role` (`primary_finding`,
  `background`, `hypothesis`); `hedged`; a `negative_finding_type`; a `context` (species,
  anatomy, notes); and a verbatim `evidence_sentence`.
- **Context:** the species comes from an enum, mapped to NCBITaxon in code
  (`config.SPECIES_TAXA`). The anatomy is the name as the abstract writes it, which step 4
  grounds. Population, cell line, dose and time point go in the notes and are never part of a
  statement: Biolink wants a CURIE for a population, and there is no settled vocabulary.
- **Stored as given.** Code stores each claim with the pair's CURIEs in place of X and Y.
  `claim_id` hashes the stored claim, and its check, review and repair hang on that id. So a
  stored claim is never rewritten: groundings, repairs and verdicts are kept beside it.
- **The prompt:** `prompts/label.md` is the extractor contract. The domain rules are in
  `prompts/claim-rules.md`, which the label and review prompts both include, so the reviewer
  judges by the same rules.

### Source papers many pairs cite

- **The rule.** A PMID that a pair has only as a source citation (its edges cite it, and the
  search didn't find it) isn't read when at least `config.SOURCE_SCREEN_MIN_PAIRS` (100) pairs
  of the selection cite it, unless its title and abstract name both nodes (`label.screened`).
  A paper the search found is always read. `None` turns the screen off.
- **Why.** Such a paper is a high-throughput study, such as BioPlex 3.0 (cited by 167,351
  pairs of the Nov 13 2026 search) or a GWAS paper. Its abstract names few of the pairs that
  cite it, so reading it for each one costs a call and finds nothing. In that search, 1.23M of
  the 3.72M (pair, PMID) jobs were source citations of 17,247 papers.
- **The threshold.** In the Nov 6 2026 Monarch run, the 558 answers from source papers cited by
  100 or more pairs held no claim. Looser rules lose evidence, since a source paper is often an
  edge's only evidence: skipping those cited by 20 or more pairs lost 25% of Monarch's
  supported edges, and skipping every source paper whose abstract names neither node lost 16%.
- **The count is the selection's.** Select counts the pairs it keeps that cite each source PMID
  and writes the counts on each pair (`Pair.source_pmid_pairs`). So the screen doesn't depend
  on how `pairs.jsonl` is split into parts or how many pairs step 2 searches. A `pairs.jsonl`
  without the counts screens nothing.
- **Naming both nodes.** `label.names_both` looks for any term step 2 searched each node by, as
  whole words, or for a term of 3 characters or more with spaces and hyphens squashed out. It
  is permissive, so a doubtful abstract is read.
- **What follows.** Steps 5 and 6 ask about the same abstracts (`label.pair_pmids`), so an
  unread paper never holds a pair or leaves it not fully labeled. A pair the screen leaves with
  nothing to read is checked with no evidence, its edges unsupported, as when the search finds
  nothing. `refinements.jsonl` names what was left unread (`abstracts_screened`), and the label
  summary and apply report count it. The edge keeps the paper in its `publications`.

### Which predicates, and which way round

- **Allowed predicates:** those of the statement rules a pair matches, everything Biolink puts
  below them, and their `opposite_of` partners (`label.predicates_of`). Predicates a
  category-free skip rule covers are left out: `occurs_together_in_literature_with` sits below
  `associated_with`, and co-mention would otherwise support gene–disease edges.
- **Read the way Biolink reads them.** Each predicate is turned to read from the rule's
  subject to its object by its domain and range (`biolink.oriented`): one defined the other way
  round becomes its inverse, a non-canonical one its canonical inverse, and one that fits
  neither way is dropped. So gene–disease pairs never get `condition_associated_with_gene`.
- **What fits:** a predicate's constraints are its own domain and range plus its inverse's. A
  missing constraint or a node with no known category constrains nothing, and a category fits
  a class when their descendant sets overlap. So imprecise categories never drop a claim.
- **After the call.** The reply schema has one predicate enum for both directions, so the model
  can still put the wrong node first. Code reads every statement, `also_supports` entries
  included, Biolink's way round (`refine.directed`). One that fits as written stays, and so
  does a symmetric one that fits the other way. Otherwise the predicate becomes its inverse.
  Last, a non-canonical predicate is turned to its canonical inverse, since the graph's edges
  use canonical predicates. A statement that fits neither way fails its claim. The stored
  claim keeps the model's wording.

### Checked in code

A claim that fails any check, even after the repairs below, is invalid as a whole: it stays in
the cache and counts for nothing, with its errors in the `claim_checks` table and the label
summary. Nothing versions the checks, so they are recomputed on every run for the run's pairs.

- The predicate fits the pair's nodes one way round or the other.
- Each statement's qualifier slots and values are allowed for its predicate's association
  between these entities. Subject and object qualifiers (aspect, direction, form or variant,
  part, derivative) describe a gene, gene product or chemical, so they are never allowed on a
  disease or phenotype, and a chemical–disease statement gets none.
- `qualified_predicate` comes only with a subject or object qualifier, and `causes` only with
  an aspect or a direction.
- `negative_finding_type` is on negative claims only. A negative claim may lack one; the
  summary counts these.
- Each `also_supports` entry adds something: a positive claim's own statement doesn't already
  entail it, and it doesn't entail what a negative claim denies.
- The evidence sentence is in the abstract. This check is kept apart (`sentence_valid`), since
  the repair pass can still supply the sentence.

The association class comes from `pubmed_evidence/biolink.py`, not `bmt.get_associations`,
which matches too loosely (it offers `GeneToGoTermAssociation` for gene–disease). Qualifiers
whose values are ontology terms (anatomy, species, population) are never in the reply's
qualifiers, since models make up CURIEs: species and anatomy come in the claim's context.

### Repaired after labeling

Some claims fail a check only for how the model wrote them: 13.1% of the claims of a 200-pair
sample of the Nov 16 2026 kinase cut, of which 66 of 71 were true when read against their
abstracts. So the check, the review and step 6 read every claim through `label.read_claim`,
which repairs a statement that fails as written (`label.SALVAGE`):

- **A bare `causes`:** `qualified_predicate: causes` with no aspect or direction is dropped.
- **A gene product as its gene:** a Protein, which NodeNorm keeps apart from its gene, also
  fits a `gene` domain or range (`gene_associated_with_condition`, `regulates`), where its own
  categories fail the statement. Step 6 still proposes an edge only if it is valid with the
  node's own categories, so such a claim supports the Protein's edges and adds no invalid one.
- **The gene's qualifier on the disease's side:** a subject or object qualifier on a disease or
  phenotype moves to the gene, gene product or chemical on the other side. A direction moves
  only with an aspect beside it, since alone it may describe the disease's own course;
  otherwise it is dropped, as is a slot the other side sets already.
- **A symmetric statement written from the disease,** with the gene's qualifiers on the
  object, is read from the gene.
- **A redundant `also_supports` entry,** one the claim's own statement entails, is dropped.

A statement that passes as written is read as written. No repair makes a statement say more
than the model wrote, and none drops a slot from what a negative claim denies, which would
deny more. The repairs change no version: the stored claim stays as the model gave it, and the
next label run applies them to every cached answer without a call. On cached runs they recover
all 71 failing claims of the kinase sample, 147 of the Nov 6 Monarch run's 148 and all 271 of
DrugCentral's; step 6 replayed on them loses nothing and gains supported edges (29 to 34, 348
to 373, 3,943 to 3,944).

### Quotes and repair

- **The check:** `quotes.quote_found` looks for the evidence sentence in the title and
  abstract, ignoring case, whitespace, Unicode forms and dash and quote variants, but not
  wording. A quote may join up to three spans of 20 characters or more with `...`
  (`reference/formats.md`, "Quotes").
- **Repair:** the label step ends with a repair pass (`repair.py`; `--no-repair` skips it) for
  each claim that passes every other check but whose sentence wasn't found, about 1% of them.
  Nearly all such quotes are the abstract's own words, slightly off, so code repairs them
  first.
  - **Code** (`quote_align.py`) covers the quote's words with at most two sentences of the
    title or of the abstract and quotes them whole, so its quote is verbatim by construction.
    If the passage doesn't name both entities, it joins a sentence that names the missing one.
  - **The repair model** (`config.REPAIR_MODEL`, gpt-5.6-terra, set apart from the reviewer)
    gets only a claim code can't place: under half the quote's words are in the abstract. It
    returns a verbatim quote, checked the same way, or null. Null is a normal answer, since a
    model asked for a quote may grab a loosely related sentence.
  - **Why code first:** on the 409 unfound sentences of a 2026-11-16 test, code placed 393 and
    the model found 9 of the other 16: 402 found for $0.06. The model alone found 347, for
    $1.55. Embeddings and a cross-encoder did worse than the alignment.
- **A repair supplies a sentence and never changes the claim.** The claim record flags it
  (`evidence_sentence_repaired`), and the apply report counts it.
- **Which repairs count:** a repair is cached per claim under its repairer (`code`, or the
  model) and version (`quote_align.VERSION`, or the repair prompt version), one answered
  attempt each, a "none" included. `label_run.json` names the repairs that count in priority
  order (`repairers`): the code's, then the model's. Review, step 6 and `evaluate` take each
  claim's sentence from the first that found it, and warn when the aligner, the repair prompt
  or the repair model has changed since. A `label_run.json` naming a single `repairer` is
  still read.

### Structured replies

The reply is strict structured output: each call sends a JSON Schema, and the service returns
an object that fits it (`reference/formats.md`). Every role, design, species, predicate and
qualifier value is an enum, so code checks only what a schema can't say. A deployment without
strict output fails with its own error rather than falling back to JSON mode.

- **One schema per set of scope rules.** A pair's schema allows the predicates of every rule it
  matches. Pairs of the same rules share a schema, and with it the cached prompt prefix, at a
  tenth of the price.
- **`none`, not null, for qualifier slots.** Anthropic's strict output allows at most 16
  parameters with union types, which nullable fields are, and 13 nullable slots, counted again
  in `also_supports`, would be over it. The schema has 5 nullable fields, and a test counts
  every reply schema's.
- **Positions, not ids:** abstracts are `A1`...`A20`, and code maps them back to PMIDs. A
  position beyond the request's, or an abstract answered twice, fails the call. An abstract the
  reply leaves out is counted and asked again next run.

### Study design and publication data

- **One design per abstract,** not per claim. PubMed's publication types set it where they name
  one (`config.PUBMED_DESIGNS`), and the extractor's answer is used where they don't. Where
  several match, the one `StudyDesign` lists first wins.

  | PubMed publication type | study_design |
  | --- | --- |
  | Meta-Analysis, Network Meta-Analysis | `meta_analysis` |
  | Systematic Review | `systematic_review` |
  | Randomized Controlled Trial, Equivalence Trial, Pragmatic Clinical Trial | `rct` |
  | Observational Study, Clinical Trial and its phases, Controlled Clinical Trial | `cohort` |
  | Case Reports | `case_report` |
  | Review, Editorial, Comment, Letter | `narrative_review` |

  Veterinary trials, trial protocols and scoping reviews name none.
- **Nothing PubMed returns about a paper is dropped.** Each abstract's evidence record
  (`PublicationEvidence` in `shared/schema.py`) keeps the publication types, a coarse article
  type, the year, retraction, the design and what set it, and the extractor's own design. So
  claim rates can be compared by publication type without fetching or labeling again. Step 6
  reads only the retraction (rule 9).

## Step 4: Grounding anatomy

- **Exact names only.** Each anatomy name is looked up in the RENCI Name Resolver
  (`NAMERES_URL`) among anatomical entities, and the matches are normalized with NodeNorm. The
  name grounds to the first match whose label or a synonym *is* the name, case and whitespace
  aside, and whose NodeNorm clique has an UBERON term. So a near match, a cell type or a cell
  line never grounds: liver, kidneys and serum do; hepatocytes, T cells and HeLa cells don't.
  Precision beats recall here, since a grounded name narrows the claim's statements, and so the
  edges they support and propose.
- **Ungrounded names** stay on the claim as text and go to `lit_context_notes`. They narrow
  nothing.
- **Names never looked up** (the lookup failed, or no ground run came after the label run)
  would count in every context with nothing said. Step 6 holds the pair until a ground run
  looks the name up.
- **Cached** by name in the PubMed cache's `groundings` table, misses too; `--refresh` looks
  them up again. A failed lookup isn't cached.
- **UBERON's release is pinned.** The step fetches `uberon-basic.obo` from the release
  `config.UBERON_URL` names into the cache directory, once, and records its `data-version` in
  `label_run.json`. Step 6 builds the `is_a`/`part_of` closure from it at load (0.1 s, no new
  dependency). To change the release, change the pin and delete the file.
- **At or below:** one anatomy term is at or below another when they're equal or the closure
  lists the other among its ancestors (`refine.Lattice.at_or_below`). So a claim in the liver
  supports an edge in the digestive system, or one with no anatomy, but a claim with no anatomy
  doesn't support an edge in the liver. Species have no hierarchy and compare by equality.

## Step 5: Review

- **Every claim that would count.** A claim would count, once upheld, when its quote passed
  the check, its paper isn't retracted, it isn't a `hypothesis`, and one of its statements is
  narrower than or equal to an in-scope edge of its pair (`refine.would_count`; step 6 defines
  narrower). That takes in the claims that only add a PMID to an edge, and a negative claim
  through the statements it `also_supports` (rule 8). A claim that supports no edge of its pair
  isn't sent: it changes nothing. Nor is one whose only such statement is support-only (rule
  11), unless `--review-support-only`.
- **One pass.** Each claim's effect is independent of the others', so the step sends, once,
  every such claim with no verdict of this reviewer and prompt version yet. A claim whose call
  fails, or that a reply leaves out, stays unreviewed: step 6 holds its pair, and the next run
  sends it again. A rejected claim is never sent again.
- **Verdicts:** `upheld` or `reject`, with a reason. The reviewer judges the whole claim,
  `also_supports` included: does the abstract state this, at this level, with this polarity? It
  rejects a claim the abstract doesn't state, or only poses as a hypothesis or a question,
  whatever its `claim_role` (rule 10; a hedged finding, or background the abstract asserts, is
  not a hypothesis); one about safety, cost or approval status; one with the opposite
  direction, about the wrong entity, or more specific than the abstract; a negative claim
  without explicit language; and a species or tissue the abstract contradicts. A claim that
  says less than the abstract is upheld.
- **The same bundles as step 6.** Both build each pair's claims through `bundle.py`, so the
  claims review sends are the ones step 6 would count. Review reads the input graph (`--input`)
  for that.
- **The request:** each claim in words and as JSON, with its evidence sentence (the repaired
  one, if any), and each abstract once. Items are batched per pair, split by abstract chunk
  (20), then at 40 items a call (`config.REVIEW_ITEMS_PER_CALL`).
- **Versions:** the review prompt version hashes the prompt with its includes,
  `FORMAT_VERSION`, the reply schema and the effort, so another effort reviews afresh. It
  doesn't hash the model; verdicts are stored per reviewer, so two reviewers' never mix.
- **Which review step 6 applies:** the one `label_run.json` names, not the default. A work
  directory another model reviewed keeps that model's verdicts until a review run names its
  own.

## Step 6: Refining the edges

### What a pair brings

`bundle.py` gathers each pair from the work files and the caches:

- **Checked edges:** an edge is checked when it is in scope and its pair is fully labeled:
  every abstract step 3 asks about has an answer of the run, and every claim of those answers
  has been checked. A pair with no abstracts is checked too, and its edges are unsupported. A
  pair with a failed call, or with claims never checked, leaves its edges unchanged until a
  label run finishes it. It is never refined on part of its claims.
- **Evidence:** one record per abstract read for the pair, with its claims, but those that
  failed a check. Each claim carries its grounded anatomy, its statements read Biolink's way
  round, the verdict of the review `label_run.json` names, and whether its paper is retracted.
- **The pair's other edges,** those select left out, come too. They aren't checked, but a
  proposal one of them already makes links to it (rule 5).

### Refinement (`refine.py`)

A pure module, with no I/O and no model calls, implementing the rules below for one pair.

- **Narrower.** S1 is narrower than or equal to S2 (S1 ⊑ S2) when they join the same subject
  and object, S1's predicate is S2's or below it (mixins included), and every qualifier S2
  sets, S1 sets to the same value or one below it: by the enum's `is_a` (upregulated below
  increased), by predicate for `qualified_predicate`, by the UBERON closure for anatomy, and by
  equality for species and any other slot. S1 may set more, and an added qualifier makes it
  narrower. A symmetric statement compares either way round. *Strictly narrower* is narrower
  and not the same statement. Nothing is narrower than a negated statement, and a negated
  statement is narrower than nothing.
- **A claim's statements** (`refine.statements_of`): a positive claim's statement and each
  `also_supports` entry; a negative claim's `also_supports` entries alone, since what it denies
  supports nothing (rule 8: a failed trial's "X in_clinical_trials_for Y" counts). Each
  statement gains the claim's grounded species and anatomy as `species_context_qualifier` and
  `anatomical_context_qualifier` where its association allows the slot.
- **Which claims count** (rules 1, 9 and 10). A claim counts when its quote passed the check,
  its paper isn't a Retracted Publication, its role isn't `hypothesis`, and review upheld it.
  Nothing else filters claims: there are no weights and no thresholds. Study design, hedging,
  a background role and the finding type are recorded on the evidence and filter nothing.
- **Support and PMIDs** (rules 2 and 3). An in-scope edge E is supported when at least one
  counting claim has a statement narrower than or equal to E's. Its supporting PMIDs are those
  claims' distinct PMIDs, the ones E already cites included, and their number is E's support
  score. Those E doesn't cite are appended to its `publications`.
- **Proposals** (rule 4). Each statement of a supporting claim that is strictly narrower than
  E is proposed: "X increases Y expression in the liver" under "X affects Y".
  - There is one proposal per distinct statement the abstracts make, not every combination the
    hierarchy allows, and one per pair however many claims make it or originals it refines.
  - Its PMIDs are those of the claims that make exactly its statement. A claim for a narrower
    statement adds its PMID to that narrower proposal, not to a broader one, though every
    original it is narrower than takes the PMID.
  - It must be a valid Biolink edge between its nodes: its predicate fits them, and its
    association allows each slot and value it sets. One that isn't is dropped, and its claims
    still support E.
- **Links** (rule 5). A proposal whose statement an edge of the pair already makes isn't
  written as a new edge: E names that edge in `lit_refined_by` instead. An edge select left
  out is written as read, not duplicated. A symmetric statement is the same either way round
  (`refine.statement_key`): 'MDM2 interacts with TP53' links to 'TP53 interacts with MDM2'.
- **Ignored claims** (rule 6) change nothing: what a negative claim denies, contrary claims,
  and claims that support no edge of the pair (broader, sibling or other-branch statements).
  An unsupported E is written unchanged, with score 0 and no proposals.
- **Species in proposals** (rule 7) are off by default (`config.SPECIES_IN_PROPOSALS`; apply's
  `--species-in-proposals`). Almost every in-scope association allows a species, so with them
  on almost every supported edge whose claims name one proposes "E in human".
  - **Off:** step 6 takes `species_context_qualifier` out of the proposals and links
    (`refine.without_species`). Proposals that become the same statement merge, with the union
    of their PMIDs and originals. One that refines an original with that same species keeps
    it, to stay narrower than that original. One that becomes an original it refines ("E in
    human" becomes E) no longer refines it, and is dropped if it refines nothing else. One that
    becomes another edge of the pair is a link to it: dropping it would make `lit_refined_by`
    depend on whether an abstract named a species. The report counts what was folded
    (`species_folded`).
  - **Either way,** the originals' support and PMIDs are the same, and claims are reviewed
    whatever the flag says, so flipping it needs no model call. Say E is "A affects B",
    abstract 1 says "A increases B activity" and abstract 2 says the same in human. Off, there
    is one proposal with PMIDs 1 and 2; on, there are two, the second in human.
- **Support only** (rule 11). A statement narrower than E only by a predicate
  `config.SUPPORT_ONLY` lists under E's predicate, or one below it, supports E but is never
  proposed under it. The one entry is `ameliorates_condition` under `treats`: the graph
  doesn't need the narrower edge, and in DrugCentral it was 700 of the 1,186 proposals.
  - **Review:** by default (`config.REVIEW_SUPPORT_ONLY` off) such a statement isn't sent to
    review and counts without a verdict, unless the claim was rejected, so it holds no pair.
    `--review-support-only` sends it and counts it only once upheld. Give the flag to both
    review and apply, since apply holds a pair whose claim review didn't send.
  - **Only under the listed predicate.** Under a broader edge, such as
    `treats_or_applied_or_studied_to_treat`, `ameliorates_condition` is an ordinary narrower
    statement: it is proposed, and needs review.
- **Order.** The output doesn't depend on the claims' order. A symmetric proposal is written as
  its first valid reading with the nodes in id order: Biolink gives a chemical–gene symmetric
  statement its qualifiers only with the chemical as subject.
- **Holds.** A pair is held while a claim that would count has no verdict of the review
  `label_run.json` names (a review run clears it), or a claim names anatomy step 4 never looked
  up (a ground run clears it). A held pair's edges pass through as read, and the report counts
  them and warns of each kind of hold, so a partial run never writes a partial answer. `apply
  --no-review`, for testing only, counts unreviewed claims instead.
- **Tests:** seven properties in `tests/test_refine.py`, each over 300 seeded random claim
  sets, the first six with the species kept and taken out:
  1. adding a counting claim never removes a PMID, a supported edge or a proposal;
  2. the claims' order doesn't change the output;
  3. ignored claims change nothing;
  4. no proposal duplicates an edge of the pair;
  5. every proposal is strictly narrower than each original it refines, canonical and valid;
  6. a proposal's PMIDs are exactly those of the claims whose statements propose it;
  7. with the species taken out, each proposal is the merge of those that say its statement,
     and the originals' support and PMIDs don't change.

### What step 6 writes

Nothing is removed, and an input edge keeps every slot. `AugmentedEdge` in `shared/schema.py`
defines the fields step 6 adds, and `reference/work-files.md` those of `refinements.jsonl` and
`apply_report.json`.

- **A checked edge** is the record as read, with `augmentation: checked`, its support score,
  its supporting PMIDs, the edges that refine it (`lit_refined_by`) and `lit_evidence`: every
  abstract read for the pair, each claim marked with whether it `supports` this edge. The
  supporting PMIDs its source didn't cite are appended to `publications` and listed again in
  `lit_added_publications`, so the source's own citations can be told apart and a rerun takes
  the added ones back out. An unsupported edge is written the same way, with score 0, so
  "checked, nothing found" reads apart from "never checked".
- **A proposed edge** is our own claim, with `augmentation: proposed`. Its provenance, set in
  `config.py`, is `primary_knowledge_source: infores:kgbcia-augmentor`,
  `supporting_data_source: [infores:pubmed]`, `knowledge_level: knowledge_assertion` and
  `agent_type: text_mining_agent`, with the PMIDs of the claims that make it as
  `publications`. `original_edge_ids` names every original it refines. Its id is
  `kgbcia-augmentation:` plus a hash of its statement's key, so the same statement in two
  graphs, either way round, gets one id. It is never negated, and it carries no
  `kgbcia_source_records`, which the MCP interface's check accepts for this stage's edges.
- **Unchecked edges** pass through as read, apart from their `id`: those out of scope, in a
  held pair, in a pair not fully labeled, or in a pair step 2 didn't search. The report counts
  each kind.
- **Ids:** every input edge gets an `id`: its own, or `kgbcia-edge:` plus a hash of the record
  as read. The hash changes if the transformation's output for the edge does, which is
  acceptable until that stage assigns stable ids. Identical records share one outcome. Two
  different records with one id stop the step, and review, with an error naming their lines.
- **Records are written back as read:** the same keys in the same order with the same values,
  nulls included, then the `id` where an edge had none, then step 6's fields.
- **Feeding the stage its own output adds nothing twice.** On an edge an earlier run wrote (it
  carries `augmentation` or a `lit_*` field), every field step 6 has ever written is stripped
  (`apply.AUGMENTED`) and the PMIDs it appended are taken back out, before the edge is refined
  again. Edges an earlier run added are dropped, and step 1 never selects them. Any other edge
  keeps every field, even one named like ours (`evidence`, `original_edge_ids`): stripping
  those everywhere would delete a source's own data.
- **One bad edge never stops the run.** An edge that isn't a valid `Edge` record is skipped by
  select and the bundle, and written exactly as read by apply. Both reports list it under
  `invalid_edges`, with a warning. Removing bad edges is the final cleaning pass's job. Only a
  problem with the input as a whole (a line that isn't JSON, a missing file) stops the step.
- **All or nothing:** the graph and `refinements.jsonl` are written under temporary names and
  renamed into place at the end; `apply_report.json` follows them.

## Choosing the model: the gold set

`augment.py evaluate` scores a run against a curator's gold set, and calls no model. The gold
file (`reference/formats.md`) holds a curator's claims for some abstracts of some pairs, and
edges of those pairs to score. An edge needs no label: its expected outcome is step 6's
refinement on the curator's claims (the gold side), so a curator judges only what the abstracts
say. Each run's checked claims are refined the same way (the run side), and the sides compared:

- **Claims:** each gold abstract's claims against the run's, one to one. A match is the same
  polarity and the same directed statement, exactly, or up to an ancestor (the run's statement
  is the curator's or one it entails). Context, role, hedging and `also_supports` aren't part
  of a match; their effect shows in the scores below.
- **Support:** whether each gold edge is supported on each side: accuracy, Cohen's κ, and
  precision and recall per label.
- **PMIDs:** each edge's supporting PMIDs, run against gold: micro precision and recall. A
  wrong PMID cites a paper for what it doesn't say.
- **Proposals:** each pair's proposals and links, by statement key: micro precision and recall.
  A wrong proposal is a false edge in the graph.

To compare models evenly, both sides read the same abstracts: an edge is refined on its pair's
gold abstracts only, and on the run's side only where the run's own model answered all of them
(a fallback model's answer leaves its pair out). No review verdict is read on either side, so
the scores are the extractor's. Otherwise claims count as in step 6, and anatomy is grounded as
in step 4. Cost comes from the calls the label cache logged, so it is what the run spent.

Two tools make the gold set:

- **`evaluate --draw N [--seed S]`** samples N pairs from step 6's `refinements.jsonl` into
  `work/gold_pairs.jsonl`, stratified so that the rarer outcomes appear. A stratum is an edge's
  outcome (`unsupported`, `supported`, or `refined`: supported with proposals or links) and
  the statement rule that kept it. The strata take turns, the smallest first, each drawing a
  pair not yet drawn. A pair with no abstracts read is left out: a curator has nothing to read.
- **`label --pairs FILE`** labels exactly the pairs a file names, writes `label_sample.jsonl`
  and leaves `label_run.json` alone, as `--sample` does. A curator turns its lines into gold
  lines, and each candidate model labels the same file.

## Caching

Two SQLite caches hold everything that costs a request or tokens, so a rerun asks only for
what has no answer yet. `reference/work-files.md` ("Caches") has their locations, the
environment variables that move them, and their tables. A cache still at its earlier default
location is used, with a warning to move it, until the new one exists.

- **PubMed** (`shared.pubmed`'s cache): ESearch results by query string, with the retrieval
  date; abstracts by PMID, so one shared by several pairs is fetched once; NodeNorm entries;
  and the skill's own table, the anatomy groundings, by name.
- **Claims** (`labels.sqlite`): each abstract's answer for a pair, keyed by (pair, PMID, label
  prompt version, model); each claim's checks, keyed by that and the claim's id; verdicts and
  sentence repairs, keyed by that and their own model (or `code`) and version; and a log of
  every call's tokens and cost.
- **What invalidates an answer:** a prompt version hashes the prompt file with the files it
  includes, the step's `FORMAT_VERSION` (the request layout), its reply schema or schemas and
  the reasoning effort sent (`omit` when none is). So an edit to a prompt or schema, or another
  effort, starts fresh answers, as does another model. Bump `FORMAT_VERSION` when the code
  that writes the request changes, and `quote_align.VERSION` when code's repairs change.
- **The label version** covers every non-skip statement rule's schema, so one run has one
  version, and runs on two graphs with the same rules share claims. A rules change that alters
  a predicate list starts fresh claims; a provenance rule has no schema and changes nothing.
- **Rebuilding:** step 6 reads the label cache, the PubMed cache's publication types and
  groundings, the UBERON file and the work files: no request and no model call.

## Cost

- **The dry run** (`label --dry-run`) estimates labeling alone: prompt tokens as characters
  divided by 4, the cached prefix at the cache-read price, and 2,000 output tokens a call
  (`label.ESTIMATED_OUTPUT_TOKENS`, a guess). Review and quote repair come on top, and no
  multiplier is assumed for them. Grounding costs no tokens.
- **Budgets:** `label --dry-run --budget USD` exits 1 when the estimate is over the budget;
  the full run's `--budget` and `--max-label-cost` act on the same estimate.
- **Review's share** grows with the number of supported edges, since it reads every claim that
  would count.

Measured:

- On the Nov 12 2026 run, labeling was 58% of the total, review 35% (with gpt-5.6-terra) and
  repair 7%, and the labeling estimate was 20% over the actual ($3.44 against $2.81).
- Review with gpt-6-luna at medium costs 18-24x less than with terra ("Second pass").
- Quote repair, now code first, calls the model for about 4% of the sentences not found.
- The last relabel of both graphs, in an earlier label format with smaller replies, cost $2.12
  (Monarch) and $11.14 (DrugCentral) for labeling and review together.

Not built: lowering `ABSTRACTS_PER_CALL` to 10 if the 95th percentile of a 20-abstract call's
output tokens exceeds 70% of the model's 16,000-token cap.

## Not used: embeddings

Embeddings capture topic but not polarity: "A inhibits B" and "A does not inhibit B" come out
almost the same, so they can't tell support from refutation. They could trim abstracts before
step 3, but the step 2 query already requires both entities, and at bulk-model prices trimming
saves little. Revisit if we move to full text and need to choose which paragraphs to send.

## Open items

Limits of the design as built:

- **The source screen's threshold** (100 pairs) is validated only on Monarch's Nov 6 2026
  source papers. An edge doesn't say that its source paper went unread: only
  `refinements.jsonl` and the report do.
- **A support threshold:** an edge is supported from one abstract on. "Supported by 2+
  abstracts" would be a filter on `lit_support_score`, here or in the cleaning stage; the gold
  set can show whether one abstract is precise enough.
- **Support-only claims go unreviewed** by default (rule 11). In DrugCentral's run, review
  upheld 1,309 of 1,470 such claims, and they added 1,255 PMIDs.
- **Unstated qualifiers:** an original with a qualifier no claim can state, such as
  `frequency_qualifier`, `onset_qualifier` or a population, can never be supported.
- **One predicate enum for both directions:** a schema per subject would stop a statement with
  the wrong node first at the source, rather than turning or dropping it after the call.
- **Score scales differ by source** (STRING's raw combined score is 0–1000), so one
  `--min-score` for every capped rule is crude.
- **Recall:** MeSH supplementary concepts (`MESH:C…`, many ChEMBL compounds) could be searched
  with `[nm]`, and terms under 3 characters (the gene symbols F2, C3) are dropped from queries.
- **Negated edges:** anything that ignores `negated` reads a negated edge as a positive link,
  GNN link prediction and MCP neighbor queries included. They come from source data (step 6
  never proposes one), so this needs a team-wide rule, such as dropping them before training.
- **Not built:** Cohen's κ between two curators' gold sets (`evaluate.metrics` can compute
  it); flat fields beside `lit_evidence` for a store such as Neo4j that can't hold a list of
  objects; attaching the evidence through Biolink's `has_supporting_studies`; and Message
  Batches for Claude bulk runs (half price, but a submit-then-collect flow).

To confirm, since the repository can't show them:

- **Not run yet:** the tuning sample and the relabel of both graphs on claims. The sample
  measures output tokens per abstract (`ESTIMATED_OUTPUT_TOKENS`), how often one bad
  `also_supports` entry invalidates a claim, how often the model puts the wrong node first, and
  how many claims review gets.
- **Claude models have run only offline here:** the label schema has yet to meet Anthropic's
  structured-output limits live, and how often Haiku refuses biomedical abstracts is
  unmeasured.
- **The checked Monarch and DrugCentral edges carry no qualifiers** today, so the unstated
  qualifiers above cost nothing yet.
- **Filter team:** which of these edge types the filter keeps by default, and how many there
  are. This sets the scale.
- **Transformation team:** whether TCRD's PMID columns (likely GeneRIF, ChEMBL activity
  references and DrugCentral references) are carried into `publications`.
- **Who holds the NCBI API key.**
