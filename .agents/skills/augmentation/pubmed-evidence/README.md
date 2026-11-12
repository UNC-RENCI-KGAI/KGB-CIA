# PubMed evidence

Checks a knowledge graph's edges against PubMed, the augmentation stage of KGB-CIA:

- chooses the edges whose relationships papers state as claims (a drug treats a disease, a gene
  causes one, two proteins bind) and groups them by node pair;
- searches PubMed for each pair and fetches the abstracts, plus those the edges already cite;
- has an LLM write down the claims each abstract makes about the pair, each with its study
  design, role, hedging, context and a verbatim evidence sentence, and checks every claim in
  code against Biolink and the abstract;
- grounds the anatomy the claims name to UBERON;
- has a stronger model find the evidence sentences code couldn't find, and review every claim
  that would count;
- refines every checked edge from its pair's claims, as DESIGN.md's step 6 says: an edge
  the abstracts support gains their PMIDs in its `publications`, with its support score and
  evidence, and each more specific statement they make is proposed as a new edge beside the
  originals, which stay.

The LLM only extracts and reviews claims; code refines, and every claim is cached, so the graph
can be rebuilt from the cache without calling a model. [`DESIGN.md`](DESIGN.md) explains each
step and the choices behind it. [`SKILL.md`](SKILL.md) is the workflow an agent follows: run
each step, read what it wrote, tune the extraction prompt on a sample, choose the model, then
run the full set.

## How it works

```
augment.py select     graph            ->  work/: statements, pairs, entities, scope_report.json
augment.py pubmed     pairs, entities  ->  work/pubmed.jsonl; searches and abstracts to the cache
augment.py label      pairs + abstracts -> claims, checks and sentence repairs to the cache;
                                           work/label_run.json
augment.py ground     the claims       ->  anatomy groundings and the UBERON release to the caches
augment.py review     graph + claims   ->  verdicts on the claims that would count, to the cache
augment.py apply      graph + caches   ->  OUT/nodes.jsonl, OUT/edges.jsonl,
                                           work/refinements.jsonl, work/apply_report.json
augment.py evaluate   gold set + cache ->  work/evaluate_report.md
augment.py evaluate --draw N   refinements -> work/gold_pairs.jsonl, the pairs to curate
augment.py            every step but evaluate, in order
```

Each step reads the files of the steps before it, so any one reruns alone. The caches (the
PubMed cache, `data/cache/pubmed.sqlite`, and in `data/augmentation/cache/`, `labels.sqlite` and
`uberon-basic.obo`) hold everything that costs a request or tokens; a rerun asks only for
what's new.

## Run it alone

`scripts/augment.py` is a `uv run --script` file and installs its own dependencies. Run it from
the repository root: it reads the repository's KGX records (`shared/schema.py`), and its
default paths (`data/filter`, `data/augmentation`) are relative to it. The models it can call,
with their prices, are in `scripts/pubmed_evidence/config.py` (`MODELS`).

```bash
A=.agents/skills/augmentation/pubmed-evidence/scripts/augment.py
$A --input data/filter --output data/augmentation select
$A --output data/augmentation pubmed
$A --output data/augmentation label --dry-run     # cost estimate, no calls
$A --output data/augmentation label --model gpt-6-luna
$A --output data/augmentation ground
$A --input data/filter --output data/augmentation review
$A --input data/filter --output data/augmentation apply
```

`--help` lists the steps, and `augment.py STEP --help` a step's options. Every step takes
`--input` (the graph), `--output` (where step 6 writes the augmented graph), `--work` (default
`<output>/work`) and `--cache-dir` (the label cache and the UBERON release; default
`data/augmentation/cache`, or `KGB_AUGMENT_CACHE_DIR` for the shared RENCI drive), before or
after the step's name.

| Step | Main options (default) |
| --- | --- |
| `select` | `--rules FILE` (built-in rules), `--capped-limit` (10,000), `--min-score` (none) |
| `pubmed` | `--max-abstracts` (20), `--limit N`, `--refresh`, `--no-nodenorm` |
| `label` | `--model` (gpt-6-luna), `--reasoning-effort E` (the model's: low for gpt-6-luna), `--prompt FILE`, `--sample N --seed S` or `--pairs FILE`, `--dry-run`, `--no-repair`, `--abstracts-per-call` (20), `--workers` (8), `--limit N` |
| `ground` | `--refresh` |
| `review` | `--model` (gpt-5.6-terra), `--reasoning-effort E`, `--prompt FILE`, `--limit N`, `--review-support-only` (off: `config.REVIEW_SUPPORT_ONLY`) |
| `apply` | `--no-review` (for testing only), `--species-in-proposals` (off: `config.SPECIES_IN_PROPOSALS`), `--review-support-only` (off, as review), `--model`, `--prompt-version`, `--reviewer`, `--review-prompt-version` (default: from `label_run.json`) |
| `evaluate` | `--gold FILE` (`<work>/gold.jsonl`), `--run MODEL[@PROMPT_VERSION]` (repeatable; default the run in `label_run.json`), `--species-in-proposals` (off, as apply); or `--draw N --seed S` (0) to draw the pairs to curate |
| (none: every step) | `--model`, `--reasoning-effort`, `--review-model`, `--limit`, `--skip-review` |

Environment (the repository's `.env.example` lists every variable): `AZURE_OPENAI_API_KEY`
for labeling and review with the RENCI models (not for `--dry-run`), and `ANTHROPIC_API_KEY` or
an `ant auth login` profile with Claude models; labeling also needs the review model's,
`AZURE_OPENAI_API_KEY` by default, for the sentence repair pass. `AZURE_OPENAI_BASE_URL` points
the RENCI model ids at your own Azure OpenAI resource (empty: RENCI's). `NCBI_EMAIL`, and
`NCBI_API_KEY` for 10 requests a second instead of 3, for the PubMed search. `KGB_PUBMED_CACHE`
is the PubMed cache file, `shared.pubmed`'s, which every agent that reads PubMed shares
(default `data/cache/pubmed.sqlite`); a `pubmed.sqlite` from an older `--cache-dir` opens as it
is, so move it there or point `KGB_PUBMED_CACHE` at it. `NAMERES_URL` points the ground step at
another Name Resolver.

The same library is the pipeline's augmentation stage: `uv run kgb-augment` takes the same
arguments as `augment.py`.

## Layout

```
SKILL.md, DESIGN.md, README.md
prompts/            label.md, review.md and repair.md, the three system prompts, and
                    claim-rules.md, which label.md and review.md both include; a prompt's text
                    sets its version, so an edit starts fresh answers
reference/          rules.md (step 1's scope rules), work-files.md (every file and cache),
                    formats.md (gold set, model replies)
scripts/            augment.py, the one entry script
scripts/pubmed_evidence/   the library: config, graphio, records, cache, quotes, biolink,
                    sentences, llm, scope, pubmed, label, repair, uberon, ground, bundle,
                    refine (step 6's rules, for one pair), review, apply, evaluate, cli
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
