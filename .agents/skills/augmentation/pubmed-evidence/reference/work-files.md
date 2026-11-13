# Work files, caches and ids

Each step reads the files of the steps before it in the work directory (`--work`, default
`<output>/work`) and writes its own, so any step can be rerun alone. What costs requests or
tokens is in the two caches (the PubMed cache, `KGB_PUBMED_CACHE`, default
`data/cache/pubmed/pubmed.sqlite`, and the label cache in `--cache-dir`, default
`data/cache/augmentation`); the work files are cheap to rebuild. The record types are in
`scripts/pubmed_evidence/records.py` (`Entity` and `Pair` extend `shared.pubmed`'s, and
`PubmedRecord` is `shared.pubmed`'s own), the caches in `cache.py`, and the graph's records in
the repository's `shared/schema.py`.

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

Fields by file:

- **`entities.jsonl`** (`Entity`): `id`; `name` (a name that is only the node's id, like
  Xenbase's `XB-GENE-865198`, is replaced by the first symbol or synonym, or dropped; MONDO's
  `obsolete ` prefix is removed); `category`; `equivalent_identifiers`; `synonyms` (the node's
  `symbol`, `synonym` and `full_name`, without the name).
- **`statements.jsonl`** (`SelectedStatement`): `statement_id`, `pair_id`, `subject`, `predicate`,
  `object`, `qualifiers` (the edge's `qualified_predicate` and `*_qualifier` slots), `negated`,
  `rule` (the statement rule that kept it), `provenance_rule` (the provenance rule that set its
  tier and cap, when one lowered every kept edge making it; absent otherwise), `edge_count`
  (input edges making it), `sources` (their `primary_knowledge_source`s), `source_pmids` (PMIDs
  in their `publications`), `score` (capped rules; may be absent).
- **`pairs.jsonl`** (`Pair`): `pair_id`, `nodes` (sorted), `statement_ids`, `source_pmids`
  (union of its statements'), `rules` (the statement rules its statements matched, never a
  provenance rule, which set the predicates its claims may use).
- **`pubmed.jsonl`** (`PubmedRecord`): `pair_id`; `query` (`""` for a pair not searched because a
  node has no usable name, kept for its source PMIDs); `retrieved` (ISO date of the search);
  `total` (ESearch hit count); `synonyms` and `mesh` (node id → the `[tiab]` terms and `[mh]`
  headings used); `pmids` (`pmid`, `origin` = `search`, `source` or `source+search`, `rank` =
  1-based search rank or null; search results first, by rank); `no_abstract` (fetched PMIDs
  without abstract text). Titles, abstracts and publication types are in the PubMed cache.
- **`label_run.json`** (`LabelRun`): the run that ground, review, apply and the full run use.
  - `model`, `prompt_version`, `prompt` (path of the prompt file) and `reasoning_effort` (the
    effort the run sent, `none` for none) of the label run;
  - `repairer` and `repair_prompt_version`: the sentence repairs that count, those of the label
    run's repair pass, or of an earlier pass of the same run if this one made none;
  - `reviewer`, `review_prompt_version` and `review_reasoning_effort`, written by review. The
    full run continues the review's effort while the same reviewer reviews;
  - `uberon_version`: the `data-version` of the `uberon-basic.obo` that ground pinned, kept
    across label runs.

  A new label run with another model or prompt clears the review and the repairs. `--sample`,
  `--pairs` and `--dry-run` runs never write it, and neither does a run whose failed calls are
  more than 5% of the calls its pairs need (those answered by earlier runs included).
- **`label_sample.md`**: per sample pair (chosen with `--sample`'s seed, or named by the
  `--pairs` file), the entities, then each abstract's title, design and publication types, and
  its claims: each in words with its role, hedging, finding type and context, its evidence
  sentence and whether it was found, the repair of one that wasn't, its `also_supports` in
  words, and its check errors if it's invalid. What you read to tune the prompt.
- **`label_sample.jsonl`**: one line per (pair, PMID) of the sample: `pair_id`, `pmid`,
  `title`, `abstract`, `publication_types`, `study_design` (the model's), and `claims`, each
  `{claim_id, claim, valid, errors, sentence_valid, repaired_sentence}` with `claim` as stored.
  Add `gold_claims` to a line to make it a gold abstract line ([formats.md](formats.md)).
- **`refinements.jsonl`**: one line per refined pair, by pair id; a held pair, or one not fully
  labeled, gets none.
  - `pair_id`;
  - `abstracts_read`: the number of abstracts read for the pair (those step 2 found with
    text); `evaluate --draw` leaves out the pairs with 0;
  - `edges`: each in-scope edge of the pair, once: `edge_id`, `statement` (subject, predicate,
    object, `qualified_predicate` and qualifiers, `negated`), `supported`, `support_score`
    (its distinct supporting PMIDs), `supporting_publications`, `added_publications` (those its
    source didn't cite, appended to its `publications`) and `refined_by` (the proposed edges
    more specific than it, and the edges its links name);
  - `proposed_edges`: each edge step 6 adds: its `id`, statement slots (with `negated`),
    `publications` (its PMIDs), `support_score` and `original_edge_ids` (the originals it
    refines);
  - `links`: each proposal an edge of the pair already makes, whether select kept that edge or
    not: `linked_to` (those edges), and the same fields as a proposed edge but its `id`.

  With the species taken out of proposals (`config.SPECIES_IN_PROPOSALS` off), the proposals
  and links are those left after the fold; the species-qualified ones it folded are only
  counted, in the report.
- **`apply_report.json`**:
  - `run`: the label run applied (the fields of `label_run.json`), plus `require_review`
    (false with `--no-review`), `species_in_proposals` and `review_support_only`; `input`, `output`; `uberon_version`
    (of the file loaded); `nodes`;
  - `edges_read`, `ids_assigned` (edges given a hashed `id`), `earlier_edges_dropped` (edges
    an earlier run added), `pairs_refined`;
  - `edges_checked`, `edges_supported`, `edges_unsupported`, `edges_refined` (supported edges
    with a proposal or link), and `publications_added` (PMIDs appended to `publications`);
  - `proposed`: `edges` (proposed edges written), `links` (proposals an edge of the pair
    already makes, whether select kept it or not) and `species_folded` (the species-qualified
    proposals and links taken into ones without a species; 0 with `--species-in-proposals`);
  - `held`: `pairs_unreviewed` (a claim that would count has no review),
    `pairs_anatomy_not_looked_up` (a claim names anatomy the ground step never looked up), and
    `edges`, their in-scope edges, written as read;
  - `unchecked`: in-scope edges left as read, `edges_not_fully_labeled` (their pair has an
    unanswered abstract after a failed call, or claims a stopped label run never checked) and
    `edges_not_searched`, and `pairs_not_fully_labeled`;
  - `claims`: of the bundled pairs, held or not, `used`, `upheld`, `rejected`, `unreviewed` and
    `sentences_repaired`; and `counting`, those that support an edge of a refined pair;
  - `skipped`: pairs searched with no edge in scope, pairs with claims never checked and pairs
    with anatomy names never grounded, abstracts without publication types, claims failing a
    check;
  - `refinements` (lines written to `refinements.jsonl`) and `edges_written`.
  - `invalid_edges`: edges that aren't valid `Edge` records, written as read and unchecked:
    `edges`, `by_reason` (count per reason), `examples` (the first 10, `line` and `reason`).
- **`gold_pairs.jsonl`**: one line per pair `evaluate --draw` drew, in the order drawn, from
  the refined pairs with at least one abstract read (the draw prints how many it left out):
  `pair_id`, the stratum it was drawn for (`outcome`: `unsupported`, `supported` or `refined`;
  and `family`, the id of the statement rule that kept the edge, or its predicate if none
  matches),
  and `edges` (each refined edge of the pair: its id and outcome). `label --pairs` reads the
  `pair_id`s.
- **`evaluate_report.md`**:
  - a header: the gold set's size, the lines skipped, the UBERON release, the gold claims'
    anatomy names (grounded, not, failed to look up), and whether proposals keep their species;
  - the gold side: step 6's refinement on the curator's claims, with each gold edge's outcome,
    PMIDs and `refined_by`, and each pair's proposals and links;
  - a comparison table of the runs scored: abstracts covered; checked claims' precision and
    recall, exact and up to an ancestor; edges covered; support accuracy and Cohen's κ; PMID
    and proposal precision and recall; cost per 10,000 pairs;
  - per run: its coverage (and the abstracts a fallback model answered, left out with their
    pairs' edges), its claims and their anatomy names, the edges refined, its calls, tokens and
    cost (per call, per pair, per 10,000 pairs); its claims as answered and as checked; the
    support confusion matrix with accuracy, κ and per-label precision, recall and F1; the PMID
    and proposal scores; and the disagreements, the edges whose PMIDs differ and the pairs whose
    proposals differ.

**The review summary.** Step 5 writes no work file; it prints a summary, which
`review.review_claims` returns: `label_model`, `label_prompt_version`, `reviewer`,
`review_prompt_version` and `reasoning_effort`; `pairs`, `pairs_not_fully_labeled`,
`pairs_not_looked_up` and `skipped` (as in the apply report); `claims_to_review` (claims that
would count with no verdict of this review when the run starts), `already_reviewed` (those with
one, upheld or rejected) and `sent`; `verdicts` (`upheld`, `reject`), `omitted` (left out of a
reply) and `repeated`; `unreviewed` (claims that would count still without a verdict, whose
pairs step 6 holds); `calls`, `calls_needed` (this run's calls and those the claims reviewed
before took: the 5% rule takes the failed calls as a share of them), `failed_calls` and
`fallback_served`;
`input_tokens`, `cache_read_tokens`, `cache_write_tokens`, `output_tokens` and `cost`; and `ok`.

## Caches

Two SQLite files, meant to be shared across a team: the PubMed cache, the file
`KGB_PUBMED_CACHE` names (default `data/cache/pubmed/pubmed.sqlite`), which every agent that reads
PubMed shares, and `labels.sqlite` under `--cache-dir` (`KGB_AUGMENT_CACHE_DIR`, default
`data/cache/augmentation`), both under the cache root `KGB_CACHE_DIR` moves. A cache still at
its old default (`data/cache/pubmed.sqlite`, `data/augmentation/cache/`) is used, with a warning
to move it, until the new one exists. They use SQLite's rollback journal, not WAL (unsafe on
network filesystems), so only one process should write to a cache at a time.
Never delete them: they hold paid claims. Beside the label cache
sits `uberon-basic.obo`, the UBERON release step 4 pins (`config.UBERON_URL`); delete it only
to fetch another release after changing the pin.

**`pubmed.sqlite`** (`PubMedCache`: `shared.pubmed`'s tables, and the skill's `groundings`):

| Table | Key | Columns |
| --- | --- | --- |
| `searches` | `query` | `retrieved` (date), `total` (hit count), `pmids` (JSON list, by relevance) |
| `abstracts` | `pmid` | `title`, `abstract` (`""` when PubMed has none, so it isn't fetched again), `year`, `retrieved`, `publication_types` (JSON list of PubMed's types, `[]` when it gives none; null for an abstract cached before they were recorded, which step 2 fetches again) |
| `nodenorm` | `curie` | `entry` (NodeNorm's JSON, null for an unknown CURIE), `retrieved` |
| `groundings` | (`text`, `kind`) | `curie` (the UBERON term, null for a name that didn't ground), `label`, `source` (`nameres+nodenorm`), `created`; `text` is the name casefolded with its whitespace collapsed, `kind` is `anatomy` |

A cached search is reused while it holds at least as many PMIDs as asked for (or all its
hits); `--refresh` redoes searches. An empty cached abstract is never refetched once its
publication types are cached. Ground's `--refresh` looks names up again.

**`labels.sqlite`** (`LabelCache`):

| Table | Key | Columns |
| --- | --- | --- |
| `abstract_claims` | (pair_id, pmid, prompt_version, model) | `study_design` (the model's), `claims` (JSON list of stored claims), `created`, `answered_by` (the model that answered: a fallback's, if one did) |
| `claim_checks` | … + claim_id | `valid` (every check but the sentence's passed), `errors` (JSON list), `sentence_valid` (the sentence was found as given) |
| `claim_reviews` | … + claim_id, reviewer, review_prompt_version | `verdict` (`upheld` or `reject`), `reason`, `created`, `answered_by` |
| `claim_repairs` | … + claim_id, repairer, repair_prompt_version | `evidence_sentence` (null when the repairer found none), `sentence_valid`, `created`, `answered_by` |
| `calls` | autoincrement id | `step` (`label`/`review`/`repair`), `pair_id`, `model`, `prompt_version`, `input_tokens` (every prompt token), `cache_read_tokens` and `cache_write_tokens` (those of them the service's prompt cache read and wrote), `output_tokens`, `cost` (USD, null if unknown), `seconds`, `error`, `created`, `answered_by` |

`…` is `abstract_claims`' key. Columns added to `calls` since the first release (the cache
token counts, `answered_by`) are added to an older cache when it is opened, and are null in its
old rows. A cache from before the claims also has the statement labels' tables (`labels`,
`proposals`, `reviews`, `repairs`): no code reads or writes them, and they stay as they are.

Every abstract a readable reply answers gets an `abstract_claims` row (`[]` for no claims), so
a rerun asks only for abstracts with no answer for that (prompt version, model). The checks
are recomputed for the run's pairs on every label run. Step 6 reads this cache, the PubMed
cache's years, publication types and groundings, the UBERON file, the graph,
`label_run.json`, `statements.jsonl`, `entities.jsonl` and `pubmed.jsonl`: no model calls.

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
  SHA-256(prompt text + "\0" + extra)>`, e.g. `label@2fc040f2`. The prompt text has each
  `{{include NAME}}` line replaced by the file it names (`claim-rules.md`: beside the prompt,
  else the skill's `prompts/`). `extra` is the step's `FORMAT_VERSION`, its reply schema (for
  label, every scope rule's schema; skip and provenance rules have none) and the reasoning
  effort sent, so answers made at one effort are never reused at another. `FORMAT_VERSION` is the version of the request layout
  the code builds: `label-message-5` in `label.py`, `review-message-5` in `review.py` and
  `repair-message-2` in `repair.py`. Any edit to a prompt file or a schema starts fresh
  answers (or reviews, or repairs); bump `FORMAT_VERSION` when the code that writes the request
  changes.
