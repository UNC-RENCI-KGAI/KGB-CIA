---
name: pubmed-evidence
description: "Check a Biolink knowledge graph's edges against PubMed — choose the edges worth checking, search PubMed for each node pair, have an LLM extract the claims each abstract makes about the pair, ground their anatomy to UBERON, have a stronger model review every claim that would count, then refine every checked edge in code: add the PMIDs of the abstracts that support it to its publications, and propose the more specific edges the abstracts state beside the originals. Use when asked to run, tune or rerun the augmentation agent, or to add literature evidence to the KGB-CIA graph."
---

# PubMed evidence for the graph's edges

`DESIGN.md` explains each step and the choices behind it; read it before a first run. In short:
**you decide, the code transforms.** You run the steps one at a time, read what each wrote,
tune the extraction prompt on a sample, choose the model, and then run the full set. You never
edit the graph, the caches or the claims by hand: every change to the graph comes from
`augment.py apply` refining each edge from the cached claims, so it can be rebuilt.

## Inputs and outputs

- **Input:** the filter stage's graph, a directory with `nodes.jsonl` and `edges.jsonl` (or
  ORION's `normalized_nodes.jsonl` and `normalized_edges.jsonl`; `.gz` works). Default
  `data/filter`; `--input DIR`.
- **Output:** the augmented graph in `--output DIR` (default `data/augmentation`): the same
  nodes, then every input edge with an `id`, each checked one with its findings in `lit_*`
  fields (its support score, the PMIDs that support it, the more specific edges, every
  abstract's claims), then the proposed edges. A checked edge's `publications` gain the
  supporting PMIDs its source didn't cite, listed again in `lit_added_publications`. Originals
  are never removed: each names the more specific edges its supporting claims make in
  `lit_refined_by`, and each proposed edge names the originals it refines in
  `original_edge_ids`.
- **Work directory** `OUT/work` (`--work DIR`): each step's files, the run manifest
  `label_run.json`, each refined pair's record (`refinements.jsonl`), the sample and evaluation
  reports, your tuned prompts and rules, and your `notes.md`. `reference/work-files.md`
  describes every file.
- **Caches:** PubMed responses and anatomy groundings in the PubMed cache, which every agent
  that reads PubMed shares (`KGB_PUBMED_CACHE`, default `data/cache/pubmed/pubmed.sqlite`); the
  pinned UBERON release and every claim, check, review and repair in `data/cache/augmentation`
  (`--cache-dir`, or `KGB_AUGMENT_CACHE_DIR` to share them across a team). Reruns reuse them, so
  rerunning a step is cheap. If a step warns that it is using a cache at its old default under
  `data/`, tell the user and give them the warning's `mv` command; don't move it yourself. Only
  one process should write to a cache at a time.

Pass the same `--input`, `--output`, `--work` and `--cache-dir` to every step, with the same
`KGB_PUBMED_CACHE`. Put downloads, helper scripts and other scratch work in `OUT/work/`, not
elsewhere.

## Scripts

One script, `scripts/augment.py`, runs every step: `augment.py STEP` for one, `augment.py` alone
for all of them. Run it from the repository root (it's a `uv run --script` file and installs
its own dependencies; it reads the repository's `shared/schema.py`). The models it can call,
with their prices and default settings, are `MODELS` in `scripts/pubmed_evidence/config.py`.
Use `--help`, or `augment.py STEP --help` for a step's options.

| Command | Use |
|---|---|
| `augment.py select [--rules FILE] [--capped-limit N] [--min-score X]` | Step 1: keep the edges worth checking, by scope rule (`reference/rules.md`), grouped by node pair; `scope_report.json` counts what each rule kept and every edge type skipped |
| `augment.py pubmed [--limit N] [--max-abstracts N] [--refresh] [--without-key]` | Step 2: one PubMed search per pair (names, synonyms, MeSH), the abstracts it finds and those the edges already cite |
| `augment.py label [--model M] [--prompt FILE] [--sample N --seed S \| --pairs FILE] [--dry-run [--budget USD]] [--no-repair]` | Step 3: one request per pair; the model gives each abstract's study design and its claims about the pair. Code checks every claim against Biolink and the abstract, and the review model repairs evidence sentences that weren't found |
| `augment.py ground [--refresh]` | Step 4: the anatomy the claims name, to UBERON terms; pins the UBERON release. No model calls |
| `augment.py review [--model M] [--review-support-only]` | Step 5: a stronger model upholds or rejects, in one pass, each claim that would count: one that would add a PMID to an edge or propose a more specific edge. A support-only statement (`ameliorates_condition` under `treats`, `config.SUPPORT_ONLY`) isn't sent unless `--review-support-only` |
| `augment.py apply [--no-review] [--species-in-proposals] [--review-support-only]` | Step 6: refine each checked edge from its pair's claims (its supporting PMIDs, and the more specific edges they propose) and write the graph, `refinements.jsonl` and `apply_report.json` |
| `augment.py evaluate [--gold FILE] [--run MODEL[@PROMPT_VERSION] ...] [--species-in-proposals]` | Score a run's claims, and the support, PMIDs and proposals they lead to, against a curator's gold set |
| `augment.py evaluate --draw N [--seed S]` | Draw N pairs for a gold set from `refinements.jsonl`, stratified by outcome (unsupported, supported, refined) and predicate family, into `work/gold_pairs.jsonl` |
| `augment.py` | Every step but evaluate, in order: what the pipeline runs |

`uv run kgb-augment` takes the same arguments under the pipeline's name.

## Before you start

- Labeling and review (not `--dry-run`) need the model's credentials: `AZURE_OPENAI_API_KEY`
  for the Azure OpenAI models (`AZURE_OPENAI_BASE_URL` for your own endpoint), and for Claude
  models (`claude-...`) `ANTHROPIC_API_KEY` or an
  `ant auth login` profile. If they're missing, the step stops and says so; stop too, and
  don't look for a key. Labeling also uses the review model's, for the sentence repair pass;
  the label summary says if the pass was skipped for lack of them.
- `NCBI_EMAIL` should be set (NCBI asks for a contact address); `NCBI_API_KEY` raises the rate
  from 3 to 10 requests a second. Never fill either in yourself with an address or key you
  found. If they're unset the script says so at the start of the pubmed step and runs at the
  lower rate, but a job of more than 500 uncached searches without the key stops before
  sending anything (the Nov 12 2026 run took 42 minutes for 2,957 pairs without it, 75 seconds
  for 500 with it). Stop too and ask for the key; only if the slow run is wanted, rerun with
  `augment.py pubmed --without-key`, after which the full run finds the searches cached.
- The ground step needs no key: it calls the RENCI Name Resolver (`NAMERES_URL`) and NodeNorm,
  and fetches UBERON's release once.
- Earlier runs kept the PubMed cache in the cache directory. If `--cache-dir` holds a
  `pubmed.sqlite` and `KGB_PUBMED_CACHE` (default `data/cache/pubmed/pubmed.sqlite`) names no
  file yet, stop and ask whether to move it there or point `KGB_PUBMED_CACHE` at it: an empty cache
  searches every pair again and grounds every anatomy name again.
- Large PubMed jobs belong off-peak: weekends, or 9pm–5am ET on weekdays. `augment.py pubmed`
  warns when a big uncached job starts at peak hours.

## Workflow

1. **Select the edges.** `augment.py select`. Read `work/scope_report.json`: pairs kept per rule,
   pairs dropped by the caps, the negated edges kept, and the `skipped` list of edge types with
   the skip rule that covered them or `unmatched`. Look at the largest `unmatched` types: one
   that states a claim a paper could contradict (a predicate or category the rules don't
   cover) may deserve a rule. Edges mined from the literature (`knowledge_level:
   text_co_occurrence`, `agent_type: text_mining_agent`) that a query or capped rule keeps are
   capped whatever their predicate, in caps of their own (`text-co-occurrence`, `text-mined`,
   provenance rules): they carry no publications or supporting sentence, so no other step can
   check them, and the label step asks whether an abstract states the claim, not whether it
   mentions both entities; the cap bounds the cost. Their statements keep the rule that chose
   them (`gene-disease`), whose counts in the report leave them out. To change the scope, write
   `work/rules.json` (`reference/rules.md`: rules match on categories, predicates, knowledge
   level and agent type; a provenance rule can also skip) and rerun with
   `--rules work/rules.json`; say why in `notes.md`. If `kept.misdirected_edges` isn't 0, note
   those edges for the transformation team: claims rarely reach them. Record the pair count per
   rule in `notes.md`: it sets the cost.

2. **Search PubMed.** `augment.py pubmed`. One search per pair plus one fetch per ~200
   abstracts: about 25,000 pairs an hour with an API key, 4,000 without (and above 500 uncached
   searches the step won't start without one; see above). For a long job, run
   it as a background task with stderr redirected to `work/pubmed.log`, and wait for it to
   finish rather than polling. Failed pairs are listed on stderr (it returns 1 when more than
   5% fail); rerun it to retry them, since what succeeded is cached. Check its summary and
   `work/pubmed.jsonl`: many pairs with zero hits usually means a naming problem, not missing
   literature. Pairs whose node has no usable name aren't searched; long ontology names
   (phenotypes especially) rarely appear in abstracts. Note what you find in `notes.md`.

3. **Estimate the cost.** `augment.py label --dry-run`. It makes no calls; it prints the
   number of calls and the estimated tokens and cost of a full run with the default model
   (`--model` for another), and writes the first request to `work/label_request_example.txt`.
   Read that file once to see exactly what the model gets. The estimate assumes 2,000 output
   tokens a call, a guess until a sample measures it, and it covers labeling alone: the review
   and quote repair passes that follow aren't estimated (on the Nov 12 2026 run they were 35%
   and 7% of the total, with labeling 58%). `--budget USD` makes the dry run exit 1 when the
   estimate is over it. Unless you were given a budget, don't start a full labeling run
   estimated above $100: tune the prompt on samples, then stop and report the estimate.

4. **Tune the prompt on a sample.** Copy `prompts/label.md` to `work/prompts/label.md` and run
   `augment.py label --prompt work/prompts/label.md --sample 25 --seed 1`. The prompt includes
   the shared claim rules (`{{include claim-rules.md}}`), which the review prompt includes too;
   a copy reads `claim-rules.md` from beside it if there is one, else the skill's. Read
   `work/label_sample.md` and judge every claim against its abstract yourself. Look for:
   - evidence sentences `NOT FOUND`: paraphrased or stitched ones, and each one's repair beside
     it, which should be the sentence the claim rests on or `none`, never a loosely related
     one;
   - `INVALID` claims and their errors (the summary lists the most common): qualifiers the
     predicate doesn't take, `causes` without an aspect or direction, a predicate that "fits
     neither way" between the nodes, `also_supports` entries that add nothing;
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
   Edit `work/prompts/label.md` to fix what you find, and rerun the same sample (any change to
   the prompt is a new prompt version, so it starts fresh claims), then a new seed so you don't
   tune to one sample. Three rounds are usually enough. Log each change and the error it fixed
   in `notes.md`. Don't edit the skill's own prompts; propose the change in `notes.md`, and say
   there if you changed a copy of `claim-rules.md`, since the review keeps reading the skill's.

5. **Choose the model.** A gold set (`reference/formats.md`, e.g.
   `data/augmentation/gold.jsonl`) holds a curator's claims for the abstracts of some pairs and
   the edges of those pairs to score; the pairs file it was drawn from (`gold_pairs.jsonl`,
   below) names those pairs. Label exactly those pairs with each candidate model, and score
   them:
   ```
   augment.py label --pairs PAIRS --prompt work/prompts/label.md --model gpt-6-luna
   augment.py label --pairs PAIRS --prompt work/prompts/label.md --model gpt-5.6-luna
   augment.py evaluate --gold GOLD --run gpt-6-luna --run gpt-5.6-luna
   ```
   Candidates for the bulk pass: `gpt-6-luna`, `gpt-5.6-luna`, `DeepSeek-V4-Flash`,
   `gpt-oss-120b`, and `claude-haiku-5-5` if Claude credentials are set (its refused calls
   show in the label summary's failures). Read `work/evaluate_report.md`. First the gold side,
   step 6's refinement on the curator's claims: if it leaves unsupported an edge the curator's
   abstracts plainly state, or proposes an edge they don't, the claims or the gold edges are
   wrong, not the model. Then the comparison: PMID and proposal precision matter most (a wrong
   PMID cites a paper for what it doesn't say, and a wrong proposal is a false edge), then
   support accuracy and claim precision. Choose the cheapest model that is close to the best on
   them, and say why in `notes.md`. Without a gold set, keep the default model, say in
   `notes.md` that the choice is unvalidated, and once apply has refined the edges, draw the
   pairs for one: `augment.py evaluate --draw 150 --seed 1` writes `work/gold_pairs.jsonl`,
   stratified by outcome and predicate family so that unsupported edges and edges with
   proposals are both in it, and `augment.py label --pairs work/gold_pairs.jsonl` writes
   `work/label_sample.jsonl` for its pairs. A curator makes the gold set by adding
   `gold_claims` to its lines, and an edge line for each edge to score; say so in `notes.md`.

6. **Label everything.** `augment.py label --prompt work/prompts/label.md --model MODEL`, as a
   background task (stderr to `work/label.log`). When it succeeds it writes
   `work/label_run.json`, which names the model, prompt version, effort and sentence repairs
   that ground, review, step 6 and the pipeline's full run use (`--sample`, `--pairs` and
   `--dry-run` runs never change it). If more than 5% of the calls its pairs need fail (those
   answered by earlier runs count too, so a few calls that keep failing don't stop later runs)
   it returns 1; rerun the same command to retry, since answers already made are cached. A pair
   with an abstract still unanswered stays unchecked until a run answers it.

7. **Ground the anatomy.** `augment.py ground`. Its summary counts the anatomy names grounded
   to UBERON and those that weren't (they stay notes and narrow nothing), and names the UBERON
   release it pinned in the cache and recorded in `label_run.json`. Grounding is exact by
   design, so a plural or a cell type doesn't ground; note any common name that should have. It
   returns 1 if UBERON can't be fetched or more than 5% of lookups fail; rerun it. Until a
   rerun looks up a name whose lookup failed, apply holds its pair.

8. **Review the claims that would count.** `augment.py review` (default model
   `gpt-5.6-terra`; the other candidates are `gpt-6.1-sol`, `claude-sonnet-5-5` and
   `claude-opus-5-5`). In one pass it sends every claim that would count and has no verdict of
   this reviewer and prompt yet: each one that would add a PMID to an edge or propose a more
   specific edge. Read a few rejections: a reviewer that rejects most claims means the
   extraction is too loose, so go back to workflow step 4. Claims whose call failed, or that a
   reply left out, are sent again on the next run; until then step 6 holds their pairs. It
   returns 1 if more than 5% of the calls the claims that would count need fail, those
   reviewed before included.

9. **Apply.** `augment.py apply`. Read `work/apply_report.json`: edges checked, supported,
   unsupported and refined by more specific edges; the PMIDs added to `publications`; proposed
   edges, links to edges that already make their statement, and the species-qualified
   proposals folded into ones without a species; the pairs held (claims no review has judged,
   or anatomy never looked up) and the edges in scope left unchecked (pairs not fully labeled
   or not searched); the claims used, counting, upheld, rejected and unreviewed; the repaired
   sentences; and the UBERON release. Held pairs mean a review or ground run is still owed:
   rerun it, then apply. Check that the output has every input edge plus the proposed ones.
   Read a few lines of `work/refinements.jsonl`, and look up two or three added PMIDs and
   proposed edges' PMIDs in their abstracts to confirm they say what the edge says. Proposed
   edges carry no species unless you pass `--species-in-proposals`; the originals' support is
   the same either way. `ameliorates_condition` is never proposed under a `treats` edge; such a
   claim only supports it, unreviewed unless review and apply both got
   `--review-support-only`.

10. **Write `work/notes.md`:** the commands you ran, counts from each step's summary, the
    prompt changes and why, the model choice and its basis, the total cost (from the label and
    review summaries), and questions for other teams (scope rules, naming problems, misdirected
    edges).

## The full run without you

`augment.py` with no step (and `kgb-augment` with none, which `shared/pipeline.py` calls) runs
select → pubmed → label → ground → review → apply. With no `work/label_run.json` it uses the
skill's prompts and the default models; with one, it continues the run you left there (its
model, prompt file and reasoning effort, and its reviewer and the review's effort), reusing
every cached claim and verdict. So leave `work/label_run.json` naming the run you chose, and
keep the tuned prompt where it says. Tuning and model choice happen here, in the skill.

The full run caps what labeling costs. Before step 3 it estimates the cost with label's dry run
(pairs already labeled cost nothing), and if every pair won't fit the budget
(`FULL_RUN_LABEL_BUDGET` in `config.py`, $20) it labels only the first pairs that do, and prints
a flag saying how many. Use `--max-label-cost USD` when you have agreed a different budget for
the run (`inf` for none), and `--limit N` when you want exactly the first N pairs instead; with
`--limit` the budget isn't checked. A rerun with a larger budget labels only the pairs not yet
labeled. The budget covers labeling alone: review runs on those pairs' claims, so it shrinks
with them, but its cost isn't estimated, nor is quote repair's. `--budget USD` is the stricter
cap for a run that must not be trimmed: when the whole estimate is over it, the run stops with
exit 1 before any model call and says so, instead of labeling what fits.

When the label step ends with pairs not fully labeled (a call failed, or a reply left an
abstract out; its summary warns), the full run runs label once more, since the answers already
made are cached. If pairs are still not fully labeled after that, it warns with their count and
goes on to ground, review and apply: their edges are written unchecked, and apply's report
counts them as pairs not fully labeled. Rerun it to retry them. It doesn't stop there, since a
call that fails the same way every time would stop every run; label's own rule still fails the
step (exit 1) when more than 5% of the calls the pairs need failed.

`--skip-review` leaves out the review step. Apply then holds every pair with a claim that
would count but has no review, and writes its edges unchanged, so the run adds nothing
unreviewed.

## Rules

- Change the graph only through `augment.py apply`. Don't edit `edges.jsonl`, the caches or
  `label_run.json` by hand.
- `apply --no-review` counts claims no review has judged. It is for testing only: don't use it
  for a graph anyone will read, unless you were told to, and say so in `notes.md` if you were.
- Don't delete the caches: they hold paid-for claims and are shared.
- If a step fails in a way rerunning doesn't fix, stop and report its output rather than working
  around it.

## Reference

- `DESIGN.md`: what each step does and why: scope, the PubMed query, claims and their checks,
  grounding, review, how step 6 refines each edge, the gold set, caching, cost, open items
- `reference/rules.md`: step 1's scope rules, the `--rules` file format and `scope_report.json`
- `reference/work-files.md`: every work file and cache, their fields, and how ids and prompt
  versions are formed
- `reference/formats.md`: the gold set and the model reply formats
- `prompts/label.md`, `prompts/review.md`, `prompts/repair.md`: the three system prompts, and
  `prompts/claim-rules.md`, the rules the label and review prompts share
