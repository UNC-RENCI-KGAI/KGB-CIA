# PubMed evidence

Checks a knowledge graph's edges against PubMed, the augmentation stage of KGB-CIA. An edge the
abstracts support gains their PMIDs in its `publications`, and each more specific statement
they make is proposed as a new edge beside the originals, which stay.

The LLM only extracts and reviews claims. Code refines the edges, and every claim is cached, so
the graph can be rebuilt from the cache without calling a model.

| Read | For |
| --- | --- |
| [`SKILL.md`](SKILL.md) | the workflow an agent follows, and when to use which option |
| [`DESIGN.md`](DESIGN.md) | what each step does and the choices behind it |
| [`reference/rules.md`](reference/rules.md) | step 1's scope rules and `scope_report.json` |
| [`reference/work-files.md`](reference/work-files.md) | every work file and cache, with fields and locations |
| [`reference/formats.md`](reference/formats.md) | the graph in and out, the gold set, the model replies |

## Steps

| Step | Reads | Writes |
| --- | --- | --- |
| 1 `select` | the graph | `work/`: `statements.jsonl`, `pairs.jsonl`, `entities.jsonl`, `scope_report.json` |
| 2 `pubmed` | pairs, entities | `work/pubmed.jsonl`; searches and abstracts to the PubMed cache |
| 3 `label` | pairs, abstracts | claims, checks and sentence repairs to the label cache; `work/label_run.json` |
| 4 `ground` | the claims | anatomy groundings to the PubMed cache; the UBERON release to the cache directory |
| 5 `review` | graph, claims | verdicts on the claims that would count, to the label cache |
| 6 `apply` | graph, caches | `OUT/nodes.jsonl`, `OUT/edges.jsonl`; `work/refinements.jsonl`, `work/apply_report.json` |
| `evaluate` | gold set, caches | `work/evaluate_report.md` |
| `evaluate --draw N` | `work/refinements.jsonl` | `work/gold_pairs.jsonl`, the pairs to curate |

Each step reads the files of the steps before it, so any one reruns alone, and a rerun asks
only for what the caches don't hold. Label's model is gpt-6-luna; evidence sentences it
misquotes are repaired by code first, and by gpt-5.6-terra only where code can't place the
quote. Review is gpt-6-luna at medium effort.

## Run it

`scripts/augment.py` is a `uv run --script` file and installs its own dependencies. Run it from
the repository root: its default input and output paths are relative to the current directory
(it finds the repository's `shared/` package and the cache root from its own location).

```bash
A=.agents/skills/augmentation/pubmed-evidence/scripts/augment.py
$A --input data/filter --output data/augmentation select
$A --output data/augmentation pubmed
$A --output data/augmentation label --dry-run     # cost estimate, no calls
$A --output data/augmentation label --model gpt-6-luna
$A --output data/augmentation ground
$A --input data/filter --output data/augmentation review
$A --input data/filter --output data/augmentation apply
$A --input data/filter --output data/augmentation     # every step but evaluate, in order
```

`uv run kgb-augment` takes the same arguments: it is the same library, as the pipeline's
augmentation stage. `--help` lists the steps, and `augment.py STEP --help` a step's options.

## Options

The models a step can call, with their prices, efforts and output caps, are `MODELS` in
`scripts/pubmed_evidence/config.py`. Defaults named `config.X` are constants there.

**Every step** (before or after the step's name):

| Option | Default | Meaning |
| --- | --- | --- |
| `--input DIR` | `data/filter` | the graph to check |
| `--output DIR` | `data/augmentation` | where step 6 writes the augmented graph |
| `--work DIR` | `<output>/work` | each step's work files |
| `--cache-dir DIR` | `augmentation/` under the cache root | the label cache and the UBERON release ([work-files.md](reference/work-files.md), "Caches") |

**`select`**

| Option | Default | Meaning |
| --- | --- | --- |
| `--rules FILE` | the built-in rules | a JSON file of scope rules that replaces them ([rules.md](reference/rules.md)) |
| `--capped-limit N` | 10,000 | pairs kept per capped rule, best-scored first |
| `--min-score X` | none | first drop capped-rule statements scored below X, or unscored |

**`pubmed`**

| Option | Default | Meaning |
| --- | --- | --- |
| `--max-abstracts N` | 20 | PMIDs per pair from the search, by relevance; source PMIDs come on top |
| `--limit N` | all | search only the first N pairs |
| `--refresh` | off | redo searches that are cached |
| `--no-nodenorm` | off | search by the nodes' own names, without NodeNorm's synonyms and MeSH |
| `--without-key` | off | search more than 500 uncached pairs without `NCBI_API_KEY` (the step stops otherwise) |

**`label`**

| Option | Default | Meaning |
| --- | --- | --- |
| `--model M` | gpt-6-luna | the bulk model |
| `--reasoning-effort E` | the model's (low for gpt-6-luna) | the effort sent; `none` is a level, `omit` sends no setting. Part of the prompt version |
| `--prompt FILE` | `prompts/label.md` | the system prompt; its text sets the prompt version |
| `--sample N` | | label N random pairs and write `label_sample.md` and `label_sample.jsonl` |
| `--seed S` | 0 | the seed for `--sample` |
| `--pairs FILE` | | label exactly the pairs a JSONL file names (instead of `--sample`), and write the sample files |
| `--limit N` | all | label only the first N pairs |
| `--dry-run` | off | send nothing: estimate the cost, write the first request to `label_request_example.txt` |
| `--budget USD` | none | with `--dry-run` only: exit 1 when the estimate is over it |
| `--no-repair` | off | skip the quote repair pass |
| `--abstracts-per-call N` | 20 | abstracts in one call; 20 is also the most |
| `--max-output-tokens N` | the model's (16,000) | output cap per call, reasoning included |
| `--workers N` | 32 | concurrent calls, quote repair's too |

**`ground`**

| Option | Default | Meaning |
| --- | --- | --- |
| `--refresh` | off | look up again the names already cached, ungrounded ones too |

**`review`**

| Option | Default | Meaning |
| --- | --- | --- |
| `--model M` | gpt-6-luna | the reviewer |
| `--reasoning-effort E` | medium (`config.REVIEW_EFFORT`) | the effort sent, whatever the model; a model that takes none is sent none. Part of the review prompt version |
| `--prompt FILE` | `prompts/review.md` | the review prompt |
| `--limit N` | all | review only the first N pairs by id |
| `--max-output-tokens N` | the model's (16,000) | output cap per call |
| `--workers N` | 32 | concurrent calls |
| `--review-support-only` | off (`config.REVIEW_SUPPORT_ONLY`) | also review claims that support an edge only through a support-only predicate (`config.SUPPORT_ONLY`: `ameliorates_condition` under `treats`) |

**`apply`**

| Option | Default | Meaning |
| --- | --- | --- |
| `--no-review` | off | count claims no review has judged instead of holding their pairs; for testing only |
| `--species-in-proposals` | off (`config.SPECIES_IN_PROPOSALS`) | keep the species in proposed edges |
| `--review-support-only` | off | count support-only statements only once upheld; give review the same flag |
| `--model M`, `--prompt-version V` | from `label_run.json` | the label run to apply |
| `--reviewer M`, `--review-prompt-version V` | from `label_run.json` | the review to apply |

**`evaluate`**

| Option | Default | Meaning |
| --- | --- | --- |
| `--gold FILE` | `<work>/gold.jsonl` | the gold set to score against |
| `--run MODEL[@PROMPT_VERSION]` | the run in `label_run.json` | a run to score; repeat to compare. A model alone scores each of its prompt versions |
| `--species-in-proposals` | off | score proposals with their species, as apply wrote them with the same flag |
| `--draw N` | | instead of scoring, draw N pairs to curate into `gold_pairs.jsonl` |
| `--seed S` | 0 | the seed for `--draw` |

**The full run** (no step name; these go before any step name only here):

| Option | Default | Meaning |
| --- | --- | --- |
| `--model M` | the one `label_run.json` names, else gpt-6-luna | the bulk model |
| `--reasoning-effort E` | the run's in `label_run.json` while its model labels, else the model's | step 3's effort |
| `--review-model M` | the reviewer `label_run.json` names, at its effort, else gpt-6-luna | the reviewer; another one reviews at `config.REVIEW_EFFORT` |
| `--limit N` | all | search and label only the first N pairs; turns the cost cap off |
| `--max-label-cost USD` | 20 (`config.FULL_RUN_LABEL_BUDGET`) | label only the first pairs whose estimated cost fits; `inf` for no cap |
| `--budget USD` | none | stop with exit 1 before any model call when labeling's estimate is over it |
| `--skip-review` | off | skip step 5; apply then holds every pair with an unreviewed claim that would count |

`--review-support-only` and `--species-in-proposals` also take a `--no-` form, for when the
`config` default is on.

## Environment

Scripts read environment variables only; the repository's `.env.example` lists the keys and
cache variables. A flag wins over its variable.

| Variable | Default | Sets |
| --- | --- | --- |
| `AZURE_OPENAI_API_KEY` | | the key for the Azure OpenAI models: label, review and the repair model (not `--dry-run`) |
| `AZURE_OPENAI_BASE_URL` | the project's endpoint | your own Azure OpenAI resource, with the models deployed under the same ids |
| `ANTHROPIC_API_KEY` | | the key for `claude-...` models; an `ant auth login` profile works too |
| `NCBI_EMAIL` | | the contact address NCBI asks for |
| `NCBI_API_KEY` | | 10 PubMed requests a second instead of 3; needed above 500 uncached searches unless `--without-key` |
| `KGB_AUGMENT_INPUT` | `data/filter` | `--input`'s default |
| `KGB_AUGMENT_OUTPUT` | `data/augmentation` | `--output`'s default |
| `KGB_AUGMENT_MODEL` | gpt-6-luna | `label --model`'s default, and the full run's |
| `KGB_AUGMENT_REVIEW_MODEL` | gpt-6-luna | `review --model`'s default, and the full run's |
| `KGB_AUGMENT_REPAIR_MODEL` | gpt-5.6-terra | the quote repair model; it has no flag |
| `KGB_CACHE_DIR` | the repository's `data/cache` | the root both caches are under |
| `KGB_AUGMENT_CACHE_DIR` | `augmentation/` under the root | `--cache-dir`'s default |
| `KGB_PUBMED_CACHE` | `pubmed/pubmed.sqlite` under the root | the PubMed cache file, shared with every agent that reads PubMed; it has no flag |
| `NAMERES_URL` | RENCI's Name Resolver | another Name Resolver for the ground step |
| `EUTILS_URL` | NCBI's E-utilities | another E-utilities server for the pubmed step |

A model variable must name a model in `MODELS`.

## Layout

```
SKILL.md, DESIGN.md, README.md
prompts/            label.md, review.md and repair.md, the three system prompts, and
                    claim-rules.md, which label.md and review.md both include; a prompt's text
                    sets its version, so an edit starts fresh answers
reference/          rules.md, work-files.md, formats.md
scripts/            augment.py, the one entry script
scripts/pubmed_evidence/   the library: config, graphio, records, cache, quotes, biolink,
                    sentences, llm, scope, pubmed, label, quote_align (the code quote repair),
                    repair, uberon, ground, bundle, refine (step 6's rules, for one pair),
                    review, apply, evaluate, cli
tests/              no network: PubMed, NodeNorm, the Name Resolver, UBERON's release and the
                    models are faked, and each test has a PubMed cache of its own; Biolink is
                    real. The PubMed search itself is tested with shared.pubmed, in the
                    repository's tests/test_pubmed.py
```

## Tests

```bash
uv run pytest .agents/skills/augmentation/pubmed-evidence/tests
```

CI runs them with the rest of the repository's tests (`testpaths` in `pyproject.toml`).
