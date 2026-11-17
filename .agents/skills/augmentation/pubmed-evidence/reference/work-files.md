# Work files, caches and ids

Each step reads the files of the steps before it in the work directory (`--work`, default
`<output>/work`) and writes its own, so any step can be rerun alone. What costs requests or
tokens is in the two [caches](#caches); the work files are cheap to rebuild.

The record types are in `scripts/pubmed_evidence/records.py` (`Entity` and `Pair` extend
`shared.pubmed`'s, and `PubmedRecord` is `shared.pubmed`'s own), the caches in `cache.py`, and
the graph's records in the repository's `shared/schema.py`.

PMIDs are bare digits (`"12345"`) in work files and caches, and `PMID:12345` in the graph's
`publications` and in `lit_*` fields.

## Work directory

| File | Written by | One line (or file) per |
| --- | --- | --- |
| `entities.jsonl` | step 1, select | node in a kept pair |
| `statements.jsonl` | step 1 | distinct statement of a kept edge |
| `pairs.jsonl` | step 1 | kept node pair |
| `scope_report.json` | step 1 | counts per rule and skipped edge type ([rules.md](rules.md)) |
| `pubmed.jsonl` | step 2, pubmed | pair with a search or source PMIDs |
| `label_run.json` | step 3, label (a full run that succeeded); step 4, ground; step 5, review | the run that steps 4 to 6 use |
| `label_sample.md`, `label_sample.jsonl` | step 3 `--sample` or `--pairs` | sample pair / (pair, PMID) |
| `label_request_example.txt` | step 3 `--dry-run` | the first request: system and user text |
| `refinements.jsonl` | step 6, apply | refined pair: its edges' support and PMIDs, its proposed edges and links |
| `apply_report.json` | step 6 | what step 6 checked, added and held, and what it left unchecked |
| `gold_pairs.jsonl` | `augment.py evaluate --draw` | pair drawn for a gold set |
| `gold.jsonl` | you or a curator | the default gold set ([formats.md](formats.md)) |
| `evaluate_report.md` | `augment.py evaluate` | gold-set scores per run |

Step 5 writes no file of its own: it prints [its summary](#the-review-summary).

### `entities.jsonl` (`Entity`)

| Field | Content |
| --- | --- |
| `id`, `category`, `equivalent_identifiers` | as on the node |
| `name` | the node's name. One that is only the node's id (Xenbase's `XB-GENE-865198`) is replaced by the first symbol or synonym, or dropped; MONDO's `obsolete ` prefix is removed |
| `synonyms` | the node's `symbol`, `synonym` and `full_name`, without the name |

### `statements.jsonl` (`SelectedStatement`)

| Field | Content |
| --- | --- |
| `statement_id`, `pair_id` | [ids](#ids-and-versions) |
| `subject`, `predicate`, `object`, `negated` | the statement |
| `qualifiers` | the edge's `qualified_predicate` and `*_qualifier` slots |
| `rule` | the statement rule that kept it |
| `provenance_rule` | the provenance rule that set its tier and cap, when one lowered every kept edge making it; absent otherwise |
| `edge_count` | input edges making it |
| `sources` | their `primary_knowledge_source`s |
| `source_pmids` | PMIDs in their `publications` |
| `score` | the best score of those edges, for capped rules; may be absent |

### `pairs.jsonl` (`Pair`)

| Field | Content |
| --- | --- |
| `pair_id`, `nodes` | the pair's id and its two nodes, sorted |
| `statement_ids` | its statements |
| `source_pmids` | the union of its statements' |
| `rules` | the statement rules its statements matched, never a provenance rule; they set the predicates its claims may use |
| `source_pmid_pairs` | each of its `source_pmids` -> the number of pairs select kept that cite it, this one included |

`source_pmid_pairs` counts over the whole selection and is written on every pair, so a part
split from `pairs.jsonl`, or parts merged back, keep the counts. Step 3's source screen reads
them, and steps 5 and 6 read them again to ask about the same abstracts: a PMID a pair has only
as a source citation isn't read when at least 100 pairs cite it
(`config.SOURCE_SCREEN_MIN_PAIRS`), unless its title and abstract name both nodes. A
`pairs.jsonl` without the field screens nothing.

### `pubmed.jsonl` (`PubmedRecord`)

| Field | Content |
| --- | --- |
| `pair_id` | the pair |
| `query` | the search; `""` for a pair not searched because a node has no usable name, kept for its source PMIDs |
| `retrieved`, `total` | ISO date of the search; ESearch hit count |
| `synonyms`, `mesh` | node id -> the `[tiab]` terms and `[mh]` headings used |
| `pmids` | each `pmid` with its `origin` (`search`, `source` or `source+search`) and `rank` (1-based search rank, or null); search results first, by rank |
| `no_abstract` | fetched PMIDs without abstract text |

Titles, abstracts and publication types are in the PubMed cache.

### `label_run.json` (`LabelRun`)

The run that ground, review, apply and the full run use.

| Field | Written by | Content |
| --- | --- | --- |
| `model`, `prompt_version`, `prompt` | label | the label run's model, prompt version and prompt file path |
| `reasoning_effort` | label | the effort the run sent: `omit` when it sent none, `none` for the level `none` |
| `repairers` | label | the sentence repairs that count, as `[repairer, version]` pairs in priority order, e.g. `[["code", "align-2"], ["gpt-5.6-terra", "repair@..."]]`. A claim's sentence is the first of their repairs that was found |
| `repairer`, `repair_prompt_version` | label | the last of `repairers` |
| `reviewer`, `review_prompt_version`, `review_reasoning_effort` | review | the review that apply uses; the full run continues its effort while the same reviewer reviews |
| `uberon_version` | ground | the `data-version` of the pinned `uberon-basic.obo`; kept across label runs |

- **When label writes it:** a run without `--sample`, `--pairs` or `--dry-run`, whose failed
  calls are at most 5% of the calls its pairs need (those answered by earlier runs included).
- **What a new label run keeps:** a run with another model or prompt version clears the
  review and the repairs; a rerun of the same run keeps its review.
- **Which repairs count:** those of the run's repair pass, the code's and then the model's. If
  the model's pass was skipped for lack of credentials, an earlier pass's model repairs of the
  same run stay after the code's. With `--no-repair`, all of an earlier pass's stay.
- **A file without `repairers`** names one repairer in `repairer` and
  `repair_prompt_version`, and those repairs count. Review and apply warn when the repairs
  named aren't the ones a label run would make now; the next label run adds the code's repairs,
  first, without a model call for a claim the model has answered.

### `label_sample.md` and `label_sample.jsonl`

- **`label_sample.md`** is what you read to tune the prompt. Per sample pair (chosen with
  `--sample`'s seed, or named by the `--pairs` file): the entities, then each abstract's title,
  design and publication types, and its claims. Each claim is in words, with its role, hedging,
  finding type and context, its evidence sentence and whether it was found, the repair of one
  that wasn't and who made it (the first found of the code's and the model's, else the last
  made), its `also_supports` in words, and its check errors if it's invalid.
- **`label_sample.jsonl`** has one line per (pair, PMID): `pair_id`, `pmid`, `title`,
  `abstract`, `publication_types`, `study_design` (the model's) and `claims`, each
  `{claim_id, claim, valid, errors, sentence_valid, repaired_sentence}` with `claim` as stored.
  Adding `gold_claims` to a line makes it a gold abstract line ([formats.md](formats.md)).

### `refinements.jsonl`

One line per refined pair, by pair id. A held pair, or one not fully labeled, gets none.

| Field | Content |
| --- | --- |
| `pair_id` | the pair |
| `abstracts_read` | the number of abstracts read for the pair: those step 2 found with text, less those the source screen left unread. `evaluate --draw` leaves out pairs with 0 |
| `abstracts_screened` | the source papers with text that the source screen left unread, as `PMID:` CURIEs (`[]` for none). A pair with 0 read and some here had only mass-cited source papers |
| `edges` | each in-scope edge, once: `edge_id`, `statement` (subject, predicate, object, `qualified_predicate`, qualifiers, `negated`), `supported`, `support_score` (its distinct supporting PMIDs), `supporting_publications`, `added_publications` (those its source didn't cite) and `refined_by` (the proposed edges more specific than it, and the edges its links name) |
| `proposed_edges` | each edge step 6 adds: `id`, the statement slots (with `negated`), `publications`, `support_score` and `original_edge_ids` |
| `links` | each proposal an edge of the pair already makes, whether select kept that edge or not: `linked_to` (those edges) and a proposed edge's fields but `id` |

With the species taken out of proposals (the default), the proposals and links are those left
after the fold; the species-qualified ones it folded are only counted, in the report.

### `apply_report.json`

| Key | Content |
| --- | --- |
| `run` | the fields of `label_run.json` for the run applied, plus `require_review` (false with `--no-review`), `species_in_proposals` and `review_support_only` |
| `input`, `output`, `nodes` | the directories, and the nodes written |
| `uberon_version` | of the file loaded |
| `edges_read`, `ids_assigned`, `earlier_edges_dropped` | edges read, those given a hashed `id`, and those an earlier run added |
| `pairs_refined` | pairs with a line in `refinements.jsonl` |
| `edges_checked`, `edges_supported`, `edges_unsupported`, `edges_refined` | checked edges; `edges_refined` are supported ones with a proposal or link |
| `publications_added` | PMIDs appended to `publications` |
| `proposed` | `edges` (proposed edges written), `links`, and `species_folded` (species-qualified proposals and links folded into ones without a species; 0 with `--species-in-proposals`) |
| `held` | `pairs_unreviewed` (a claim that would count has no review), `pairs_anatomy_not_looked_up` (a claim names anatomy the ground step never looked up), and `edges`, their in-scope edges, written as read |
| `unchecked` | in-scope edges left as read: `edges_not_fully_labeled` (their pair has an unanswered abstract, or claims a stopped label run never checked), `edges_not_searched`, and `pairs_not_fully_labeled` |
| `claims` | of the bundled pairs, held or not: `used`, `upheld`, `rejected`, `unreviewed`, `sentences_repaired`; and `counting`, those that support an edge of a refined pair |
| `skipped` | counts, each present only when there is one: `pairs searched with no edge in scope`, `pairs with claims never checked`, `pairs with anatomy names never grounded`, `abstracts without publication types`, `claims failing a check`, `source abstracts screened out`, `pairs left without abstracts by the source screen` |
| `refinements`, `edges_written` | lines written to `refinements.jsonl` and to `edges.jsonl` |
| `invalid_edges` | edges that aren't valid `Edge` records, written as read and unchecked: `edges`, `by_reason` (count per reason), `examples` (the first 10, `line` and `reason`) |

### `gold_pairs.jsonl`

One line per pair `evaluate --draw` drew, in the order drawn, from the refined pairs with at
least one abstract read (the draw prints how many it left out).

| Field | Content |
| --- | --- |
| `pair_id` | what `label --pairs` reads |
| `outcome`, `family` | the stratum it was drawn for: `unsupported`, `supported` or `refined`; and the id of the statement rule that kept the edge, or its predicate if none matches |
| `edges` | each refined edge of the pair: its id and outcome |

### `evaluate_report.md`

- **Header:** the gold set's size, the lines skipped, the UBERON release, the gold claims'
  anatomy names (grounded, not, failed to look up), and whether proposals keep their species.
- **The gold side:** step 6's refinement on the curator's claims, with each gold edge's
  outcome, PMIDs and `refined_by`, and each pair's proposals and links.
- **Comparison table** of the runs scored: abstracts covered; checked claims' precision and
  recall, exact and up to an ancestor; edges covered; support accuracy and Cohen's κ; PMID and
  proposal precision and recall; cost per 10,000 pairs.
- **Per run:** its coverage (and the abstracts a fallback model answered, left out with their
  pairs' edges); its claims and their anatomy names; the edges refined; its calls, tokens and
  cost (per call, per pair, per 10,000 pairs); its claims as answered and as checked; the
  support confusion matrix with accuracy, κ and per-label precision, recall and F1; the PMID
  and proposal scores; and the disagreements, the edges whose PMIDs differ and the pairs whose
  proposals differ.

### The review summary

What step 5 prints, and `review.review_claims` returns:

| Key | Content |
| --- | --- |
| `label_model`, `label_prompt_version` | the run reviewed |
| `reviewer`, `review_prompt_version`, `reasoning_effort`, `review_support_only` | the review |
| `pairs`, `pairs_not_fully_labeled`, `pairs_not_looked_up`, `skipped` | as in the apply report |
| `claims_to_review`, `already_reviewed`, `sent` | claims that would count with no verdict of this review when the run starts; those with one; those sent |
| `verdicts` | `upheld` and `reject` counts |
| `omitted`, `repeated` | claims left out of a reply; repeated entries |
| `unreviewed` | claims that would count still without a verdict, whose pairs step 6 holds |
| `calls`, `calls_needed`, `failed_calls`, `fallback_served` | `calls_needed` is this run's calls plus those the claims reviewed before took; the 5% rule takes the failed calls as a share of it |
| `input_tokens`, `cache_read_tokens`, `cache_write_tokens`, `output_tokens`, `cost` | usage |
| `ok` | false when the 5% rule failed the run |

## Caches

Two SQLite files, meant to be shared across a team. Never delete them: they hold paid claims.

| Cache | Default location | Changed by |
| --- | --- | --- |
| the cache root | the repository's `data/cache`, whatever directory a command runs from | `KGB_CACHE_DIR` |
| PubMed cache, `pubmed.sqlite` | `pubmed/pubmed.sqlite` under the root | `KGB_PUBMED_CACHE` (the file's path) |
| label cache, `labels.sqlite`, and `uberon-basic.obo` | `augmentation/` under the root | `--cache-dir`, else `KGB_AUGMENT_CACHE_DIR` (the directory) |

- **Sharing:** every agent that reads PubMed shares the PubMed cache. Point the variables at a
  shared directory to share across a team, or `KGB_CACHE_DIR` outside the checkout to share
  between checkouts and git worktrees.
- **Old defaults:** `data/cache/pubmed.sqlite` and `data/augmentation/cache/` are still used,
  with a warning that says how to move them, while the new default doesn't exist and no flag or
  variable names another.
- **One writer:** they use SQLite's rollback journal, not WAL (unsafe on network filesystems),
  so only one process should write to a cache at a time.
- **UBERON:** `uberon-basic.obo` is the release step 4 pins (`config.UBERON_URL`). Delete it
  only to fetch another release after changing the pin.
- **What step 6 reads:** the label cache, the PubMed cache's years, publication types and
  groundings, the UBERON file, the graph, `label_run.json`, `statements.jsonl`,
  `entities.jsonl` and `pubmed.jsonl`. No model calls.

### `pubmed.sqlite` (`PubMedCache`: `shared.pubmed`'s tables, and the skill's `groundings`)

| Table | Key | Columns |
| --- | --- | --- |
| `searches` | `query` | `retrieved` (date), `total` (hit count), `pmids` (JSON list, by relevance) |
| `abstracts` | `pmid` | `title`, `abstract` (`""` when PubMed has none, so it isn't fetched again), `year`, `retrieved`, `publication_types` (JSON list of PubMed's types, `[]` when it gives none; null in a row cached without them, which step 2 fetches again) |
| `nodenorm` | `curie` | `entry` (NodeNorm's JSON, null for an unknown CURIE), `retrieved` |
| `groundings` | (`text`, `kind`) | `curie` (the UBERON term, null for a name that didn't ground), `label`, `source` (`nameres+nodenorm`), `created`; `text` is the name casefolded with its whitespace collapsed, `kind` is `anatomy` |

A cached search is reused while it holds at least as many PMIDs as asked for (or all its
hits); `pubmed --refresh` redoes searches. An empty cached abstract is never refetched once
its publication types are cached. `ground --refresh` looks names up again.

### `labels.sqlite` (`LabelCache`)

| Table | Key | Columns |
| --- | --- | --- |
| `abstract_claims` | (pair_id, pmid, prompt_version, model) | `study_design` (the model's), `claims` (JSON list of stored claims), `created`, `answered_by` (the model that answered: a fallback's, if one did) |
| `claim_checks` | … + claim_id | `valid` (every check but the sentence's passed), `errors` (JSON list), `sentence_valid` (the sentence was found as given) |
| `claim_reviews` | … + claim_id, reviewer, review_prompt_version | `verdict` (`upheld` or `reject`), `reason`, `created`, `answered_by` |
| `claim_repairs` | … + claim_id, repairer, repair_prompt_version | `evidence_sentence` (null when the repairer found none), `sentence_valid`, `created`, `answered_by`. `repairer` is `code` (version `quote_align.VERSION`, `answered_by` `code`) or the repair model (with the repair prompt version) |
| `calls` | autoincrement id | `step` (`label`/`review`/`repair`), `pair_id`, `model`, `prompt_version`, `input_tokens` (every prompt token), `cache_read_tokens` and `cache_write_tokens` (those of them the service's prompt cache read and wrote), `output_tokens`, `cost` (USD, null if unknown), `seconds`, `error`, `created`, `answered_by` |

`…` is `abstract_claims`' key.

- Every abstract a readable reply answers gets an `abstract_claims` row (`[]` for no claims),
  so a rerun asks only for abstracts with no answer for that (prompt version, model).
- The checks are recomputed for the run's pairs on every label run.
- Opening an older cache adds the `calls` columns it lacks (the cache token counts,
  `answered_by`), null in its old rows.
- An older cache may also hold the tables `labels`, `proposals`, `reviews` and `repairs`. No
  code reads or writes them.

## Ids and versions

- **Statement id** (`graphio.statement_id`): the first 16 hex digits of the SHA-256 of the
  canonical JSON (sorted keys, no spaces) of `{subject, predicate, object, qualifiers (sorted),
  negated}`, where the qualifiers include `qualified_predicate`. It ignores provenance, so the
  same claim from two sources is one statement, and every step computes it the same way.
- **Edge id** (`graphio.edge_id`): an input edge's own `id`, or `kgbcia-edge:` plus the first 16
  hex digits of the SHA-256 of its canonical JSON (the record as read).
- **Proposed edge id** (`graphio.proposed_edge_id`): `kgbcia-augmentation:` plus the first 16
  hex digits of the SHA-256 of its statement key (`refine.statement_key`: the statement id,
  of the reading with the nodes in id order for a symmetric predicate), so every original that
  proposes a statement, in any graph and either way round, gets the same edge.
- **Pair id** (`graphio.pair_id`): the two node ids sorted and joined with `|`, e.g.
  `CHEBI:45783|NCBIGene:25`.
- **Claim id** (`cache.claim_id`): the first 16 hex digits of the SHA-256 of the claim's
  canonical JSON, the claim exactly as `abstract_claims` stores it. Its check, review and repair
  hang on it, so a stored claim is never rewritten.
- **Prompt version** (`llm.prompt_version`): `<prompt file stem>@<first 8 hex digits of
  SHA-256(prompt text + "\0" + extra)>`, e.g. `label@2fc040f2`.
  - The prompt text has each `{{include NAME}}` line replaced by the file it names
    (`claim-rules.md`: beside the prompt, else the skill's `prompts/`).
  - `extra` is the step's `FORMAT_VERSION`, its reply schema (for label, every scope rule's
    schema; skip and provenance rules have none) and the reasoning effort sent (`omit` when
    none is sent), so answers made at one effort are never reused at another.
  - `FORMAT_VERSION` is the version of the request layout the code builds: `label-message-5`
    in `label.py`, `review-message-5` in `review.py` and `repair-message-2` in `repair.py`.
    Bump it when the code that writes the request changes.
  - So any edit to a prompt file, an included file or a schema starts fresh answers (or
    reviews, or repairs).
- **Code repair version** (`quote_align.VERSION`, `align-2`): the version code's quote repairs
  are stored under, beside the repairer `code`. It names no prompt: bump it by hand on any
  change to what `quote_align.repair` returns, and the next label run repairs every claim
  again with code (no model call for a claim the model has answered).
