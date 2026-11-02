# Work files, caches and ids

Each step reads the files of the steps before it in the work directory (`--work`, default
`<output>/work`) and writes its own, so any step can be rerun alone. What costs requests or
tokens is in the two caches (`--cache-dir`, default `data/augmentation/cache`); the work files
are cheap to rebuild. The record types are in `scripts/pubmed_evidence/records.py`, the caches
in `cache.py`.

PMIDs are bare digits (`"12345"`) in work files and caches, and `PMID:12345` in the graph's
`publications`.

## Work directory

| File | Written by | One line (or file) per |
| --- | --- | --- |
| `entities.jsonl` | step 2 | node in a kept pair |
| `statements.jsonl` | step 2 | distinct statement of a kept edge |
| `pairs.jsonl` | step 2 | kept node pair |
| `scope_report.json` | step 2 | counts per rule and skipped edge type ([rules.md](rules.md)) |
| `pubmed.jsonl` | step 3 | pair with a search or source PMIDs |
| `sentences.jsonl` | step 4 | statement |
| `label_run.json` | step 5 (full run that succeeded), review | the run review and step 6 use |
| `label_sample.md`, `label_sample.jsonl` | step 5 `--sample` | sample pair / (statement, PMID) |
| `label_request_example.txt` | step 5 `--dry-run` | the first request: system and user text |
| `apply_report.json` | step 6 | what step 6 added and skipped |
| `evaluate_report.md` | `evaluate_models.py` | gold-set scores per run |
| `FILE`, `FILE.instructions.md` | review `--export FILE` | item to review / its instructions |

Fields by file:

- **`entities.jsonl`** (`Entity`): `id`; `name` (a name that is only the node's id, like
  Xenbase's `XB-GENE-865198`, is replaced by the first symbol or synonym, or dropped; MONDO's
  `obsolete ` prefix is removed); `category`; `equivalent_identifiers`; `synonyms` (the node's
  `symbol`, `synonym` and `full_name`, without the name).
- **`statements.jsonl`** (`Statement`): `statement_id`, `pair_id`, `subject`, `predicate`,
  `object`, `qualifiers` (the edge's `qualified_predicate` and `*_qualifier` slots), `rule`,
  `edge_count` (input edges making it), `sources` (their `primary_knowledge_source`s),
  `source_pmids` (PMIDs in their `publications`), `score` (capped rules; may be absent).
- **`pairs.jsonl`** (`Pair`): `pair_id`, `nodes` (sorted), `statement_ids`, `source_pmids`
  (union of its statements'), `rules`.
- **`pubmed.jsonl`** (`PubmedRecord`): `pair_id`; `query` (`""` for a pair not searched because a
  node has no usable name, kept for its source PMIDs); `retrieved` (ISO date of the search);
  `total` (ESearch hit count); `synonyms` and `mesh` (node id → the `[tiab]` terms and `[mh]`
  headings used); `pmids` (`pmid`, `origin` = `search`, `source` or `source+search`, `rank` =
  1-based search rank or null; search results first, by rank); `no_abstract` (fetched PMIDs
  without abstract text). Titles and abstracts are in the PubMed cache.
- **`sentences.jsonl`** (`SentenceRecord`): `statement_id`, `sentence`.
- **`label_run.json`** (`LabelRun`): `model`, `prompt_version`, `prompt` (path of the prompt
  file), `reviewer`, `review_prompt_version`. A new label run with another model or prompt clears
  the reviewer; `--sample`, `--gold` and `--dry-run` runs never write it.
- **`label_sample.md`**: per sample pair, the entities, the statements with their sentences,
  and each abstract's title with every label (quote, whether it was found) and proposal (and
  its Biolink check). What you read to tune the prompt.
- **`label_sample.jsonl`**: one line per (statement, PMID) of the sample: `statement_id`,
  `pair_id`, `sentence`, `pmid`, `title`, `abstract`, `label`, `quote`, `quote_valid`, `claim`,
  `gold_label: null`. It is in the gold format ([formats.md](formats.md)).
- **`apply_report.json`**: `run` (the label run plus `require_review`), `input`, `output`,
  `infores`, `nodes`, `edges_read`, `negated_in_input`, `edges_given_pmids`, `pmids_added`,
  `negated_edges_added`, `qualified_edges_added`, `statements_confirmed`, `edges_written`, and
  `skipped`: `invalid_quote` and `unreviewed` and `rejected` (each by `confirmed`/`contradicted`/
  `proposal` rows), `invalid_qualifiers`, `out_of_scope` (cached labels for statements not in
  `statements.jsonl`), `same_as_original`, `already_in_graph`, `orphan_statements`.
- **`evaluate_report.md`**: a comparison table of the runs scored, then per run its coverage,
  invalid-quote share, cost (per call, per pair, per 10,000 pairs), confusion matrix and
  precision/recall/F1 as labeled and with bad-quote labels counted as `no_evidence`, and the
  disagreements.

## Caches

Two SQLite files under `--cache-dir` (`KGB_AUGMENT_CACHE_DIR`), meant to be shared on the
RENCI drive. They use SQLite's rollback journal, not WAL (unsafe on network filesystems), so
only one process should write to a cache at a time. Never delete them: they hold paid labels.

**`pubmed.sqlite`** (`PubMedCache`):

| Table | Key | Columns |
| --- | --- | --- |
| `searches` | `query` | `retrieved` (date), `total` (hit count), `pmids` (JSON list, by relevance) |
| `abstracts` | `pmid` | `title`, `abstract` (`""` when PubMed has none, so it isn't fetched again), `year`, `retrieved` |
| `nodenorm` | `curie` | `entry` (NodeNorm's JSON, null for an unknown CURIE), `retrieved` |

A cached search is reused while it holds at least as many PMIDs as asked for (or all its
hits); `--refresh` redoes searches. An empty cached abstract is never refetched.

**`labels.sqlite`** (`LabelCache`):

| Table | Key | Columns |
| --- | --- | --- |
| `labels` | (statement_id, pmid, prompt_version, model) | `label`, `quote`, `quote_valid` (1, 0, or null when there's no quote), `claim`, `created` |
| `proposals` | (statement_id, pmid, prompt_version, model, qualifiers) | `qualifiers` (canonical JSON of the change), `quote`, `quote_valid`, `valid` (Biolink check), `errors` (JSON list), `claim`, `created` |
| `reviews` | (kind, statement_id, pmid, prompt_version, model, qualifiers, reviewer, review_prompt_version) | `verdict` (`accept`/`reject`), `reason`, `created`; `qualifiers` is `""` for a contradicted label |
| `calls` | autoincrement id | `step` (`label`/`review`), `pair_id`, `model`, `prompt_version`, `input_tokens`, `output_tokens`, `cost` (USD, null if unknown), `seconds`, `error`, `created` |

Every combination sent in a readable reply gets a `labels` row (`no_evidence` if the reply left
it out), so a rerun sends only what has no label for that (prompt version, model). Step 6 reads
only this cache, the graph, `label_run.json` and `statements.jsonl`: no model calls.

## Ids and versions

- **Statement id** (`graphio.statement_id`): the first 16 hex digits of the SHA-256 of the
  canonical JSON (sorted keys, no spaces) of `{subject, predicate, object, qualifiers (sorted),
  negated}`. It ignores provenance, so the same claim from two sources is one statement, and
  steps 2, 5 and 6 compute it the same way. A new edge's `id` is
  `kgbcia-augmentation:<its statement id>`.
- **Pair id** (`graphio.pair_id`): the two node ids sorted and joined with `|`, e.g.
  `CHEBI:45783|NCBIGene:25`.
- **Prompt version** (`llm.prompt_version`): `<prompt file stem>@<first 8 hex digits of
  SHA-256(prompt text + "\0" + FORMAT_VERSION)>`, e.g. `label@2fc040f2`. `FORMAT_VERSION` is
  the version of the request layout the code builds: `label-message-3` in `label.py`,
  `review-format-1` in `review.py`. Any edit to the prompt file starts fresh labels; bump
  `FORMAT_VERSION` when the code that writes the request changes. A Pi-subagent review's version
  is `<stem>-pi@<hash of the exported instructions>` ([formats.md](formats.md)).
- **Review item id** (`review.item_id`): the kind's initial and 12 hex digits of the SHA-256 of
  (kind, statement id, PMID, canonical qualifiers), e.g. `c-04adbc1449d3`. Stable across
  exports and label runs.
