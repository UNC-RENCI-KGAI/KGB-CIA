# PubMed evidence

Checks a knowledge graph's edges against PubMed, the augmentation stage of KGB-CIA:

- chooses the edges whose relationships papers state as claims (a drug treats a disease, a gene
  causes one, two proteins bind) and groups them by node pair;
- searches PubMed for each pair and fetches the abstracts, plus those the edges already cite;
- has an LLM label every abstract as confirming, contradicting or not bearing on each edge, with
  a verbatim quote, and propose the Biolink qualifiers the abstracts support;
- has a stronger model review the labels that would add edges;
- adds the supporting PMIDs to the original edges' `publications`, and new edges for reviewed
  contradictions (`negated: true`) and qualifiers.

The LLM only labels; code does everything else, and every label is cached, so the graph can be
rebuilt from the cache without calling a model. [`DESIGN.md`](DESIGN.md) explains each step and
the choices behind it. [`SKILL.md`](SKILL.md) is the workflow an agent follows: run each step,
read what it wrote, tune the labeling prompt on a sample, choose the model, then run the full
set.

## How it works

```
select_edges.py     graph            ->  work/: statements, pairs, entities, scope_report.json
search_pubmed.py    pairs, entities  ->  work/pubmed.jsonl; searches and abstracts to the cache
write_sentences.py  statements       ->  work/sentences.jsonl
label_abstracts.py  all of the above ->  labels to the cache; work/label_run.json
review_labels.py    the labels that add edges -> verdicts to the cache
apply_labels.py     graph + cache    ->  OUT/nodes.jsonl, OUT/edges.jsonl, work/apply_report.json
evaluate_models.py  gold set + cache ->  work/evaluate_report.md
run_all.py          every step but evaluate, in order
```

Each step reads the files of the steps before it, so any one reruns alone. The caches
(`data/augmentation/cache/`: `pubmed.sqlite`, `labels.sqlite`) hold everything that costs a
request or tokens; a rerun asks only for what's new.

## Run it alone

The scripts are `uv run --script` files and install their own dependencies. Run them from the
repository root: they read the repository's KGX records (`shared/schema.py`) and `.pi/models.json`,
and their default paths (`data/filter`, `data/augmentation`) are relative to it.

```bash
S=.agents/skills/augmentation/pubmed-evidence/scripts
$S/select_edges.py --input data/filter --output data/augmentation
$S/search_pubmed.py --output data/augmentation
$S/write_sentences.py --output data/augmentation
$S/label_abstracts.py --output data/augmentation --dry-run     # cost estimate, no calls
$S/label_abstracts.py --output data/augmentation --model gpt-6-luna
$S/review_labels.py --output data/augmentation
$S/apply_labels.py --input data/filter --output data/augmentation
```

`--help` on any script lists its options. Every script takes `--input` (the graph), `--output`
(where step 6 writes the augmented graph), `--work` (default `<output>/work`) and `--cache-dir`
(default `data/augmentation/cache`, or `KGB_AUGMENT_CACHE_DIR` for the shared RENCI drive).

| Script | Main options (default) |
| --- | --- |
| `select_edges.py` | `--rules FILE` (built-in rules), `--capped-limit` (10,000), `--min-score` (none) |
| `search_pubmed.py` | `--max-abstracts` (20), `--limit N`, `--refresh`, `--no-nodenorm` |
| `label_abstracts.py` | `--model` (gpt-5.6-luna), `--prompt FILE`, `--sample N --seed S`, `--gold FILE`, `--dry-run`, `--workers` (8), `--limit N` |
| `review_labels.py` | `--model` (gpt-5.6-terra), `--export FILE --reviewer NAME`, `--import FILE --reviewer NAME` |
| `apply_labels.py` | `--infores` (infores:kgbcia-placeholder), `--no-review` |
| `evaluate_models.py` | `--gold FILE`, `--run MODEL[@PROMPT_VERSION]` (repeatable) |
| `run_all.py` | `--model`, `--review-model`, `--infores`, `--limit`, `--skip-review` |

Environment: `RENCI_AZURE_API_KEY` for labeling and review (not for `--dry-run`); `NCBI_EMAIL`,
and `NCBI_API_KEY` for 10 requests a second instead of 3, for the PubMed search.

The same library is the pipeline's augmentation stage: `uv run kgb-augment STEP ...` takes the
same options (steps `select`, `pubmed`, `sentences`, `label`, `review`, `apply`, `evaluate`), and
`kgb-augment` alone is `run_all.py`.

## Layout

```
SKILL.md, DESIGN.md, README.md
prompts/            label.md and review.md, the two system prompts; a prompt's text sets its
                    version, so an edit starts fresh labels
reference/          rules.md (step 2's scope rules), work-files.md (every file and cache),
                    formats.md (gold set, model replies, the review export/import lines)
scripts/            one script per step
scripts/pubmed_evidence/   the library: config, graphio, records, cache, quotes, biolink, llm,
                    scope, pubmed, sentences, label, review, apply, evaluate, cli
tests/              no network: PubMed, NodeNorm and the models are faked; Biolink is real
```

## Tests

```bash
uv run pytest .agents/skills/augmentation/pubmed-evidence/tests
```

CI runs them with the rest of the repository's tests (`testpaths` in `pyproject.toml`).
