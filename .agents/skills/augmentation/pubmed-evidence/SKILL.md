---
name: pubmed-evidence
description: "Check a Biolink knowledge graph's edges against PubMed — choose the edges worth checking, search PubMed for each node pair, have an LLM extract the claims each abstract makes about the pair, ground their anatomy to UBERON, have a second pass review every claim that would count, then refine every checked edge in code: add the PMIDs of the abstracts that support it to its publications, and propose the more specific edges the abstracts state beside the originals. Use when asked to run, tune or rerun the augmentation agent, or to add literature evidence to the KGB-CIA graph."
---

# PubMed evidence for the graph's edges

In the KGB-CIA repository, read [`agents/AGENTS.md`](../../../../agents/AGENTS.md) first: the
stage contract, the provenance every edge carries, and the non-Biolink keys each stage writes.

**You decide, the code transforms.** Run the steps one at a time, read what each wrote, tune the
extraction prompt on a sample, choose the model, then run the full set. Every change to the
graph comes from `augment.py apply` refining each edge from the cached claims, so it can be
rebuilt. Read `DESIGN.md` before a first run: it explains each step and the choices behind it.

## Commands and files

`scripts/augment.py STEP` runs one step, and `augment.py` alone runs all but evaluate. Run it
from the repository root; it is a `uv run --script` file that installs its own dependencies.
`uv run kgb-augment` takes the same arguments.

| Step | Command | Does |
| --- | --- | --- |
| 1 | `augment.py select` | keeps the edges worth checking, by scope rule, grouped by node pair |
| 2 | `augment.py pubmed` | one PubMed search per pair; fetches its abstracts and those the edges cite |
| 3 | `augment.py label` | one request per pair: the model gives each abstract's study design and claims; code checks each claim and repairs misquoted evidence sentences |
| 4 | `augment.py ground` | grounds the claims' anatomy names to UBERON; no model calls |
| 5 | `augment.py review` | a second pass upholds or rejects each claim that would count |
| 6 | `augment.py apply` | refines each checked edge from its pair's claims and writes the graph; no model calls |
| | `augment.py evaluate` | scores runs against a gold set, or with `--draw` draws the pairs for one |

Every option, default and environment variable is in `README.md` ("Options", "Environment");
`augment.py STEP --help` lists a step's. This file says when to use them.

- **Input:** the filter stage's graph (`--input`, default `data/filter`). **Output:** the
  augmented graph (`--output`, default `data/augmentation`). Both formats are in
  `reference/formats.md`.
- **Work directory** (`--work`, default `<output>/work`, written `work/` below): each step's
  files, described in `reference/work-files.md`. Keep your tuned prompts, rules, logs, scratch
  files and `notes.md` there too, not elsewhere.
- **Caches:** the PubMed cache and the label cache hold everything that cost a request or
  tokens (`reference/work-files.md`, "Caches"). Reruns reuse them, so rerunning a step is cheap.

Pass the same `--input`, `--output`, `--work` and `--cache-dir`, and the same cache environment
variables, to every step.

## Before you start

- **Model credentials.** Label and review (not `--dry-run`) need `AZURE_OPENAI_API_KEY` for the
  Azure OpenAI models, or `ANTHROPIC_API_KEY` or an `ant auth login` profile for `claude-...`
  models. If they're missing the step stops and says so: stop too, and don't look for a key.
  Label also uses the repair model's credentials (gpt-5.6-terra, on Azure); without them
  code's repairs still count and the summary says the model's pass was skipped.
- **NCBI.** `NCBI_EMAIL` should be set, and `NCBI_API_KEY` raises the rate from 3 to 10
  requests a second. Never fill either in with an address or key you found. Without the key,
  a job of more than 500 uncached searches stops before sending anything: stop and ask for the
  key. Only if the slow run is wanted, rerun with `augment.py pubmed --without-key`; the full
  run then finds the searches cached.
- **Off-peak.** Large PubMed jobs belong on weekends or 9pm-5am ET on weekdays; the step warns
  when more than 1,000 uncached searches start at peak hours.
- **Old cache locations.** If a step warns that it is using a cache at its old default, tell
  the user and give them the warning's `mv` command; don't move it yourself. If `--cache-dir`
  holds a `pubmed.sqlite` and the PubMed cache file doesn't exist yet, stop and ask whether to
  move it or point `KGB_PUBMED_CACHE` at it: an empty cache searches every pair and grounds
  every anatomy name again.
- The ground step needs no key (RENCI's Name Resolver and NodeNorm, and UBERON's release once).

## Workflow

1. **Select the edges.** `augment.py select`, then read `work/scope_report.json`
   (`reference/rules.md`): pairs kept per rule, pairs dropped by the caps, negated edges kept,
   and `skipped`, the edge types not kept with the skip rule that covered them or `unmatched`.
   - Look at the largest `unmatched` types: one that states a claim a paper could contradict
     may deserve a rule. To change the scope, write `work/rules.json` and rerun with
     `--rules work/rules.json`; say why in `notes.md`.
   - Edges mined from the literature are capped whatever their predicate, in caps of their own
     (`text-co-occurrence`, `text-mined`). `--capped-limit N` changes every rule's cap, and
     `--min-score X` first drops low-scored and unscored statements from capped rules; use
     them when the pairs cost more than the budget allows.
   - If `kept.misdirected_edges` isn't 0, note those edges for the transformation team.
   - Record the pair count per rule in `notes.md`: it sets the cost.

2. **Search PubMed.** `augment.py pubmed`: about 25,000 pairs an hour with an API key, 4,000
   without. Run a long job as a background task with stderr to `work/pubmed.log`, and wait for
   it rather than polling.
   - Failed pairs are listed on stderr, and it returns 1 when more than 5% fail. Rerun it to
     retry them: what succeeded is cached.
   - Check the summary and `work/pubmed.jsonl`. Many pairs with zero hits usually means a
     naming problem, not missing literature: pairs whose node has no usable name aren't
     searched, and long ontology names (phenotypes especially) rarely appear in abstracts.
     Note what you find in `notes.md`.
   - `--limit N` searches only the first N pairs, for a trial. `--max-abstracts N` changes how
     many search hits a pair keeps (source PMIDs come on top). `--refresh` redoes cached
     searches when newer literature is wanted. `--no-nodenorm` searches by the graph's own
     names only, to see what NodeNorm's synonyms and MeSH headings add.

3. **Estimate the cost.** `augment.py label --dry-run` makes no calls: it prints the calls,
   tokens and cost of a full run with the default model (`--model` for another) and writes the
   first request to `work/label_request_example.txt`. Read that file once to see what the
   model gets.
   - The estimate assumes 2,000 output tokens a call and covers labeling alone, not review or
     quote repair (DESIGN.md, "Cost").
   - `--budget USD` makes the dry run exit 1 when the estimate is over it.
   - Unless you were given a budget, don't start a full labeling run estimated above $100:
     tune the prompt on samples, then stop and report the estimate.

4. **Tune the prompt on a sample.** Copy `prompts/label.md` to `work/prompts/label.md` and run
   `augment.py label --prompt work/prompts/label.md --sample 25 --seed 1`. The prompt includes
   the shared claim rules (`{{include claim-rules.md}}`), read from beside the copy if there is
   one, else from the skill. Read `work/label_sample.md` and judge every claim against its
   abstract yourself. Look for:
   - evidence sentences `NOT FOUND`: paraphrased or stitched ones, and each one's repair with
     who made it (`code` or the repair model), which should be the sentence the claim rests on
     or `none`, never a loosely related one;
   - `INVALID` claims and their errors (the summary lists the most common): qualifiers the
     predicate doesn't take, a predicate that "fits neither way" between the nodes. Some slips
     aren't `INVALID`, since code repairs them after labeling (DESIGN.md, "Step 3: Claims"): a
     bare `causes`, a gene's qualifier on the disease, `also_supports` entries that add nothing;
   - negatives without explicit language: absence of evidence, safety, cost or approval
     status, "not studied", a better drug recommended instead (all no claim);
   - claims stated higher or lower than the abstract: a direction or aspect the text doesn't
     give, `treats` from a mouse study;
   - mixed results not split by context, and anatomy that isn't a tissue or organ (cell types
     and cell lines belong in the notes);
   - claims about a different entity: a related drug, a drug class, a gene family member;
   - the role and hedging: background given as a primary finding, a "may" not marked hedged;
   - the study design against the publication types shown beside it;
   - abstracts with no claims where the abstract plainly states the relationship (missed
     synonyms?).

   Edit `work/prompts/label.md` to fix what you find and rerun the same sample (any prompt
   change is a new prompt version, so it starts fresh claims), then a new seed so you don't
   tune to one sample. Three rounds are usually enough. Log each change and the error it fixed
   in `notes.md`. Don't edit the skill's own prompts: propose the change in `notes.md`, and say
   there if you changed a copy of `claim-rules.md`, since review keeps reading the skill's.

5. **Choose the model.** With a gold set (`reference/formats.md`), label exactly its pairs
   with each candidate and score them:
   ```
   augment.py label --pairs PAIRS --prompt work/prompts/label.md --model gpt-6-luna
   augment.py label --pairs PAIRS --prompt work/prompts/label.md --model gpt-5.6-luna
   augment.py evaluate --gold GOLD --run gpt-6-luna --run gpt-5.6-luna
   ```
   `PAIRS` is the pairs file the gold set was drawn from. Bulk candidates: `gpt-6-luna`,
   `gpt-5.6-luna`, `DeepSeek-V4-Flash`, `gpt-oss-120b`, and `claude-haiku-5-5` if Claude
   credentials are set (its refused calls show as failures). Read `work/evaluate_report.md`:
   - first the gold side, step 6's refinement on the curator's claims: if it leaves unsupported
     an edge the curator's abstracts plainly state, or proposes an edge they don't, the gold
     claims or edges are wrong, not the model;
   - then the comparison: PMID and proposal precision matter most (a wrong PMID cites a paper
     for what it doesn't say, a wrong proposal is a false edge), then support accuracy and
     claim precision. Choose the cheapest model close to the best on them, and say why in
     `notes.md`.

   Without a gold set, keep the default model and say in `notes.md` that the choice is
   unvalidated. Once apply has refined the edges, draw the pairs for one:
   `augment.py evaluate --draw 150 --seed 1` writes `work/gold_pairs.jsonl`, and
   `augment.py label --pairs work/gold_pairs.jsonl` writes `work/label_sample.jsonl` for a
   curator to turn into the gold set; say so in `notes.md`.

6. **Label everything.** `augment.py label --prompt work/prompts/label.md --model MODEL`, as a
   background task (stderr to `work/label.log`).
   - A run that succeeds writes `work/label_run.json`: the model, prompt version, effort and
     sentence repairs that ground, review, apply and the full run use. `--sample`, `--pairs`
     and `--dry-run` runs never change it.
   - It returns 1 when more than 5% of the calls its pairs need failed, those answered by
     earlier runs included. Rerun the same command to retry: answers already made are cached.
     A pair with an abstract still unanswered stays unchecked until a run answers it.
   - `--reasoning-effort E` labels at another effort than the model's own (`omit` sends none).
     It is a new prompt version, so it starts fresh claims: try it on a sample first.
   - If calls fail as incomplete (the output cap was hit), lower `--abstracts-per-call` or
     raise `--max-output-tokens`. Lower `--workers` if the endpoint rate-limits the run.
   - `--limit N` labels only the first N pairs. `--no-repair` skips the quote repair pass, for
     a quick test: a claim whose sentence wasn't found, and that no earlier run repaired,
     counts for nothing.

7. **Ground the anatomy.** `augment.py ground`. Its summary counts the names grounded to
   UBERON and those not (they stay notes and narrow nothing), and names the UBERON release it
   pinned in the cache directory and recorded in `label_run.json`. Grounding is exact by
   design, so a plural or a cell type doesn't ground; note any common name that should have.
   It returns 1 if UBERON can't be fetched or more than 5% of lookups fail: rerun it. Until a
   rerun looks up a name whose lookup failed, apply holds its pair. `--refresh` looks cached
   names up again, the ungrounded ones too: use it after the Name Resolver has changed.

8. **Review the claims that would count.** `augment.py review` sends, in one pass, every claim
   that would add a PMID to an edge or propose a more specific edge and has no verdict of this
   reviewer and prompt yet.
   - The default reviewer is `gpt-6-luna` at `--reasoning-effort medium`. The other candidates
     are `gpt-5.6-terra`, `gpt-6.1-sol`, `claude-sonnet-5-5` and `claude-opus-5-5`; `DESIGN.md`
     compares luna and terra.
   - `augment.py review` with no `--model` reviews with the default and names that review in
     `label_run.json`, whatever reviewed the work directory before. To stay with an earlier
     reviewer, pass it: `--model gpt-5.6-terra`. (The full run continues with the reviewer
     `label_run.json` names.)
   - Read a few rejections. A reviewer that rejects most claims means the extraction is too
     loose: go back to step 4.
   - Claims whose call failed, or that a reply left out, are sent again on the next run; until
     then apply holds their pairs. It returns 1 when more than 5% of the calls the claims that
     would count need failed, those reviewed before included; `label_run.json` is then left
     unchanged.
   - `--prompt FILE` reviews with a tuned copy of `prompts/review.md`; a new prompt or effort
     starts a fresh review. `--limit N` reviews only the first N pairs by id, for a trial.
     `--workers` and `--max-output-tokens` are as in step 6.

9. **Apply.** `augment.py apply`, then read `work/apply_report.json`
   (`reference/work-files.md`): edges checked, supported, unsupported and refined; PMIDs added;
   proposed edges, links and folded species proposals; pairs held; edges in scope left
   unchecked; and the claims used, counting, upheld, rejected and unreviewed.
   - Held pairs mean a review or ground run is still owed: rerun it, then apply.
   - Check that the output has every input edge plus the proposed ones. Read a few lines of
     `work/refinements.jsonl`, and look up two or three added PMIDs and proposed edges' PMIDs
     in their abstracts to confirm they say what the edge says.
   - Proposed edges carry no species unless you pass `--species-in-proposals`; the originals'
     support is the same either way. Give `evaluate` the same flag when you score such a run.
   - `ameliorates_condition` is never proposed under a `treats` edge. Such a claim only adds
     its PMID, unreviewed, unless review and apply both got `--review-support-only`: use it
     when those PMIDs must be reviewed too.
   - `--model`, `--prompt-version`, `--reviewer` and `--review-prompt-version` apply another
     cached run or review than the one `label_run.json` names, for example to compare two
     reviewers' graphs; write each to its own `--output`.

10. **Write `work/notes.md`:** the commands you ran, counts from each step's summary, the
    prompt changes and why, the model choice and its basis, the total cost (from the label and
    review summaries), and questions for other teams (scope rules, naming problems, misdirected
    edges).

## The full run without you

`augment.py` with no step (and `kgb-augment` with none, which `shared/pipeline.py` calls) runs
select, pubmed, label, ground, review and apply.

- **It continues your run.** With a `work/label_run.json` it uses that run's model, prompt
  file, effort, reviewer and review effort, reusing every cached claim and verdict; without
  one, the skill's prompts and the default models. So leave `label_run.json` naming the run you
  chose, and keep the tuned prompt where it says. `--model`, `--reasoning-effort` and
  `--review-model` override it.
- **It caps what labeling costs.** Before step 3 it estimates the cost with label's dry run
  (pairs already labeled cost nothing). If every pair doesn't fit $20, it labels only the first
  pairs that do and prints a flag saying how many.
  - `--max-label-cost USD` sets another cap you have agreed (`inf` for none); a rerun with a
    larger one labels only the pairs not yet labeled.
  - `--limit N` searches and labels exactly the first N pairs; the cap isn't checked.
  - `--budget USD` is for a run that must not be trimmed: when the whole estimate is over it,
    the run stops with exit 1 before any model call.
  - The cap and the budget cover labeling alone, not review or quote repair.
- **It retries labeling once.** When label ends with pairs not fully labeled, it runs label
  again. If pairs still aren't, it warns with their count and goes on: their edges are written
  unchecked and `apply_report.json` counts them. Rerun to retry them. Label's 5% rule still
  stops the run with exit 1.
- **`--skip-review`** leaves out the review step. Apply then holds every pair with a claim
  that would count but has no review and writes its edges unchanged, so nothing unreviewed is
  added.
- These options go before any step name, and only in a full run: before a step name they are
  an error (a step's own options go after its name).

## Rules

- Change the graph only through `augment.py apply`. Don't edit `edges.jsonl`, the caches or
  `label_run.json` by hand.
- `apply --no-review` counts claims no review has judged. It is for testing only: don't use it
  for a graph anyone will read unless you were told to, and say so in `notes.md` if you were.
- Don't delete the caches: they hold paid-for claims and are shared. Only one process should
  write to a cache at a time.
- If a step fails in a way rerunning doesn't fix, stop and report its output rather than
  working around it.

## Reference

- `README.md`: every option and environment variable, and the skill's layout
- `DESIGN.md`: what each step does and why
- `reference/rules.md`: step 1's scope rules, the `--rules` file and `scope_report.json`
- `reference/work-files.md`: every work file and cache, their fields, ids and prompt versions
- `reference/formats.md`: the graph in and out, the gold set and the model reply formats
- `prompts/label.md`, `prompts/review.md`, `prompts/repair.md`: the three system prompts, and
  `prompts/claim-rules.md`, the rules the label and review prompts share
