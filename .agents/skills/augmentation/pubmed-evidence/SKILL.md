---
name: pubmed-evidence
description: Check a Biolink knowledge graph's edges against PubMed — choose the edges worth checking, search PubMed for each node pair, have an LLM label every abstract as confirming, contradicting or not bearing on each edge and propose Biolink qualifiers, have a stronger model review the labels that add edges, then add supporting PMIDs and new negated or qualified edges to the graph. Use when asked to run, tune or rerun the augmentation agent, or to add literature evidence to the KGB-CIA graph.
---

# PubMed evidence for the graph's edges

`DESIGN.md` explains each step and the choices behind it; read it before a first run. In short:
**you decide, the code transforms.** You run the steps one at a time, read what each wrote,
tune the labeling prompt on a sample, choose the model, and then run the full set. You never
edit the graph, the caches or the labels by hand: every change to the graph comes from
`apply_labels.py` reading the label cache, so it can be rebuilt.

## Inputs and outputs

- **Input:** the filter stage's graph, a directory with `nodes.jsonl` and `edges.jsonl` (or
  ORION's `normalized_nodes.jsonl` and `normalized_edges.jsonl`; `.gz` works). Default
  `data/filter`; `--input DIR`.
- **Output:** the augmented graph in `--output DIR` (default `data/augmentation`): the same
  nodes, the same edges with supporting PMIDs added to `publications`, then the new negated and
  qualified edges.
- **Work directory** `OUT/work` (`--work DIR`): each step's files, the run manifest
  `label_run.json`, the sample and evaluation reports, your tuned prompts and rules, and your
  `notes.md`. `reference/work-files.md` describes every file.
- **Caches** in `data/augmentation/cache` (`--cache-dir`, or `KGB_AUGMENT_CACHE_DIR` for the
  shared RENCI drive): PubMed responses and every LLM label. Reruns reuse them, so rerunning a
  step is cheap. Only one process should write to a cache at a time.

Pass the same `--input`, `--output`, `--work` and `--cache-dir` to every step. Put downloads,
helper scripts and other scratch work in `OUT/work/`, not elsewhere.

## Scripts

All in `scripts/`, run from the repository root (they're `uv run --script` files and install
their own dependencies; they read the repository's `shared/schema.py` and `.pi/models.json`).
Use `--help` on any of them.

| Script | Use |
|---|---|
| `select_edges.py [--rules FILE] [--capped-limit N] [--min-score X]` | Step 2: keep the edges worth checking, by scope rule (`reference/rules.md`), grouped by node pair; `scope_report.json` counts what each rule kept and every edge type skipped |
| `search_pubmed.py [--limit N] [--max-abstracts N] [--refresh]` | Step 3: one PubMed search per pair (names, synonyms, MeSH), the abstracts it finds and those the edges already cite |
| `write_sentences.py` | Step 4: each statement as an English sentence, in Biolink's reading order for qualifiers |
| `label_abstracts.py [--model M] [--prompt FILE] [--sample N --seed S] [--gold FILE] [--dry-run]` | Step 5: one call per pair labels every abstract against every statement and proposes qualifiers; quotes are checked in code |
| `review_labels.py [--model M]` or `--export FILE --reviewer NAME` / `--import FILE --reviewer NAME` | Step 5b: a stronger model reviews the contradictions and qualifier proposals, which become edges |
| `apply_labels.py [--infores CURIE] [--no-review]` | Step 6: the cached labels into the graph |
| `evaluate_models.py --gold FILE [--run MODEL[@PROMPT_VERSION] ...]` | Score cached labels against a hand-labeled gold set |
| `run_all.py` | Every step but evaluate, in order: what the pipeline runs |

`uv run kgb-augment STEP` (steps `select`, `pubmed`, `sentences`, `label`, `review`, `apply`,
`evaluate`) is the same thing under the pipeline's name.

## Before you start

- `RENCI_AZURE_API_KEY` must be set for labeling and review (not for `--dry-run`). If it isn't,
  stop and say so; don't look for a key.
- `NCBI_EMAIL` should be set (NCBI asks for a contact address); `NCBI_API_KEY` raises the rate
  from 3 to 10 requests a second. Never fill either in yourself with an address or key you
  found; if they're unset, the script warns and runs at the lower rate.
- Large PubMed jobs belong off-peak: weekends, or 9pm–5am ET on weekdays. `search_pubmed.py`
  warns when a big uncached job starts at peak hours.

## Workflow

1. **Select the edges.** `select_edges.py`. Read `work/scope_report.json`: pairs kept per rule,
   pairs dropped by the gene–gene and gene–phenotype caps, and the `skipped` list of edge types
   with the skip rule that covered them or `unmatched`. Look at the largest `unmatched` types:
   one that states a claim a paper could contradict (a predicate or category the rules don't
   cover) may deserve a rule. To change the scope, write `work/rules.json` (`reference/rules.md`)
   and rerun with `--rules work/rules.json`; say why in `notes.md`. Record the pair count per
   rule in `notes.md`: it sets the cost.

2. **Search PubMed.** `search_pubmed.py`. One search per pair plus one fetch per ~200
   abstracts: about 30,000 pairs an hour with an API key, 10,000 without. For a long job, run
   it as a background task with stderr redirected to `work/pubmed.log`, and wait for it to
   finish rather than polling. Failed pairs are listed on stderr (it returns 1 when more than
   5% fail); rerun it to retry them, since what succeeded is cached. Check its summary and
   `work/pubmed.jsonl`: many pairs with zero hits usually means a naming problem, not missing
   literature. Pairs whose node has no usable name aren't searched; long ontology names
   (phenotypes especially) rarely appear in abstracts. Note what you find in `notes.md`.

3. **Write the sentences.** `write_sentences.py`. Skim `work/sentences.jsonl`: each statement
   should read as a plain claim ("imatinib causes decreased activity of ABL1"). A predicate
   without a hand-written phrase falls back to its Biolink name; if one reads badly, note it in
   `notes.md` for the code (`PREDICATE_PHRASES` in `scripts/pubmed_evidence/sentences.py`).

4. **Estimate the cost.** `label_abstracts.py --dry-run`. It makes no calls; it prints the
   number of calls and the estimated tokens and cost of a full run with the default model
   (`--model` for another), and writes the first request to `work/label_request_example.txt`.
   Read that file once to see exactly what the model gets. Unless you were given a budget,
   don't start a full labeling run estimated above $100: tune the prompt on samples, then stop
   and report the estimate.

5. **Tune the prompt on a sample.** Copy `prompts/label.md` to `work/prompts/label.md` and run
   `label_abstracts.py --prompt work/prompts/label.md --sample 25 --seed 1`.
   Read `work/label_sample.md` and judge every `confirmed` and `contradicted` label and every
   proposal against its abstract yourself. Look for:
   - quotes that failed the check (`quote_valid: false`): paraphrased or stitched quotes;
   - `contradicted` used for absence of evidence, for an opposite direction (that should be
     `ambiguous` plus a qualifier proposal), or for a negative finding limited to one variant,
     population, species or comparator;
   - labels about a different entity: a related drug, a gene family member, another species
     when the edge is about human;
   - `no_evidence` where the abstract plainly states the relationship (missed synonyms?);
   - qualifier proposals that only restate the edge, that the abstract doesn't support, or that
     are invalid (their `errors` say why). Proposals become edges, so many near-identical ones
     across pairs are a prompt problem.
   Edit `work/prompts/label.md` to fix what you find, and rerun the same sample (any change to
   the prompt is a new prompt version, so it starts fresh labels), then a new seed so you don't
   tune to one sample. Three rounds are usually enough. Log each change and the error it fixed
   in `notes.md`. Don't edit the skill's own prompt; propose the change in `notes.md`.

6. **Choose the model.** If there is a gold set (hand-labeled (statement, PMID) lines,
   `reference/formats.md`, e.g. `data/augmentation/gold.jsonl`), label it with each candidate
   model and score them:
   ```
   label_abstracts.py --gold GOLD --prompt work/prompts/label.md --model gpt-6-luna
   label_abstracts.py --gold GOLD --prompt work/prompts/label.md --model gpt-5.6-luna
   evaluate_models.py --gold GOLD --run gpt-6-luna --run gpt-5.6-luna
   ```
   Candidates for the bulk pass: `gpt-6-luna`, `gpt-5.6-luna`, `DeepSeek-V4-Flash`,
   `gpt-oss-120b`. Read `work/evaluate_report.md`. Precision on `confirmed` and `contradicted`
   matters most (those labels change the graph); choose the cheapest model that is close to the
   best on them, and say why in `notes.md`. Without a gold set, keep the default model, say in
   `notes.md` that the choice is unvalidated, and point to `work/label_sample.jsonl`: it is in
   the gold format, so a person can fill in `gold_label` to make one.

7. **Label everything.** `label_abstracts.py --prompt work/prompts/label.md --model MODEL`, as
   a background task (stderr to `work/label.log`). When it succeeds it writes
   `work/label_run.json`, which names the model and prompt version that review, step 6 and the
   pipeline's full run use (`--sample` and `--gold` runs never change it). If more than 5% of
   calls fail it returns 1; rerun the same command to retry, since labels already made are
   cached.

8. **Review the labels that add edges.** A stronger model checks only the `contradicted`
   labels and qualifier proposals, which become new edges.
   - Directly (the default, and the cheaper way): `review_labels.py`
     (default model `gpt-5.6-terra`; `--model gpt-6.1-sol` is the other candidate).
   - Or as a Pi subagent, for a few hundred items or fewer:
     `review_labels.py --export work/review_items.jsonl --reviewer pi:MODEL` writes the items
     that reviewer hasn't reviewed and `work/review_items.instructions.md`. Give a subagent (on
     `gpt-6.1-sol` or `gpt-5.6-terra`) both files; it writes one verdict line per item to
     `work/review_verdicts.jsonl` as the instructions say (`reference/formats.md`). Then
     `review_labels.py --import work/review_verdicts.jsonl --reviewer pi:MODEL`.
   Read a few rejections: a reviewer that rejects most items means the first pass is too loose,
   so go back to step 5.

9. **Apply.** `apply_labels.py`. Read `work/apply_report.json`: edges given PMIDs, PMIDs added,
   negated and qualified edges added, and what was skipped (invalid quotes, unreviewed, already
   in the graph). Check that the output has every input edge plus the new ones, and look up two
   or three new edges' PMIDs in the abstracts to confirm they say what the edge says.

10. **Write `work/notes.md`:** the commands you ran, counts from each step's summary, the
    prompt changes and why, the model choice and its basis, the total cost (from the label and
    review summaries), and questions for other teams (scope rules, naming problems, sentences
    that read badly).

## The full run without you

`run_all.py` (and `kgb-augment` with no subcommand, which `shared/pipeline.py` calls) runs
select → pubmed → sentences → label → review → apply. With no `work/label_run.json` it uses the
skill's prompts and the default models; with one, it continues the run you left there (its
model, prompt file and reviewer), reusing every cached label. So leave `work/label_run.json`
naming the run you chose, and keep the tuned prompt where it says. Tuning and model choice
happen here, in the skill.

## Rules

- Change the graph only through `apply_labels.py`. Don't edit `edges.jsonl`, the caches or
  `label_run.json` by hand.
- Don't apply unreviewed contradictions or qualifiers (`--no-review`) unless you were told to;
  say so in `notes.md` if you were.
- Don't delete the caches: they hold paid-for labels and are shared.
- If a step fails in a way rerunning doesn't fix, stop and report its output rather than working
  around it.

## Reference

- `DESIGN.md` — what each step does and why: scope, the PubMed query, sentences, labels and
  qualifiers, applying them, caching, open items
- `reference/rules.md` — step 2's scope rules and the `--rules` file format
- `reference/work-files.md` — every work file and cache, their fields, and how ids and prompt
  versions are formed
- `reference/formats.md` — the gold set, the model reply formats, the review export and import
  lines
- `prompts/label.md`, `prompts/review.md` — the two system prompts
