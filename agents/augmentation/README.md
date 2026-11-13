# Augmentation agent

**Team:** @UNC-RENCI-KGAI/augmentation · **Existing asset:** PubMed; Bagel / LitCoin for text

Find PubMed evidence for edges: add the papers that support an edge to its `publications`, and
propose more specific edges where the papers support them. Proposed edges sit beside the
originals, never in place of them.

The agent is the **pubmed-evidence** skill,
[`.agents/skills/augmentation/pubmed-evidence/`](../../.agents/skills/augmentation/pubmed-evidence/):

- [`SKILL.md`](../../.agents/skills/augmentation/pubmed-evidence/SKILL.md): the workflow an
  agent (Pi or Claude Code) follows;
- [`DESIGN.md`](../../.agents/skills/augmentation/pubmed-evidence/DESIGN.md): each step and the
  choices behind it;
- [`README.md`](../../.agents/skills/augmentation/pubmed-evidence/README.md): how to run it,
  step by step;
- `reference/`, `prompts/`, `scripts/` (the library is `scripts/pubmed_evidence/`) and `tests/`.

This package keeps only `cli.py`, the stage's entry point: `kgb-augment` and
`shared/pipeline.py` call it, and it hands its arguments to the skill's library.

In short: choose the edges whose relationships papers state as claims, search PubMed for each
node pair, have an LLM write down the claims each abstract makes about the pair (each with a
checked verbatim sentence), ground their anatomy to UBERON, and have a stronger model review
every claim that would count. Then code refines every checked edge from its pair's claims, as
step 6 of the skill's `DESIGN.md` says: an edge an abstract supports gains that abstract's PMID,
and a more specific statement an abstract makes is proposed as a new edge beside the originals.
The LLM only extracts and reviews claims; code refines, and the claims are cached, so the graph
can be rebuilt without calling a model.

## Run it alone

```bash
uv run kgb-augment --input <filter output dir> --output <path>   # every step, with defaults
uv run kgb-augment STEP --help    # STEP: select, pubmed, label, ground, review, apply, evaluate
```

The skill's `scripts/augment.py` takes the same arguments; the skill's README lists each
step's options. The model SDKs (`openai`, `anthropic`) are the project's `augmentation`
extra: `make setup` installs them, or `uv sync --extra augmentation`; the other stages don't
need them. Set `AZURE_OPENAI_API_KEY` (and `AZURE_OPENAI_BASE_URL` for your own
endpoint) for labeling and review with the Azure OpenAI models (`ANTHROPIC_API_KEY`, or an
`ant auth login` profile, with Claude models), and `NCBI_EMAIL` (plus `NCBI_API_KEY` for 10
requests a second) for the PubMed search. The search, its client and its cache are
`shared.pubmed`'s; `KGB_PUBMED_CACHE` names the cache file (default
`data/cache/pubmed/pubmed.sqlite`), which all agents reading PubMed share. With a `label_run.json`
in the work directory, the full run continues that run (its model, prompt, reasoning effort,
reviewer and review effort) and reuses its cached claims and verdicts; `--reasoning-effort`
labels at another effort (default: the model's own, low for gpt-6-luna). The full run labels
only as many pairs as its estimated cost allows (default $20, `--max-label-cost` to change it,
`inf` for no cap), and flags how many it left out; the pipeline's run is capped the same way.
`--skip-review` leaves
out the review: step 6 then holds every pair with a claim that would count but has no review,
and writes its edges unchanged. `kgb-augment apply --species-in-proposals` keeps the species in
proposed edges, which `SPECIES_IN_PROPOSALS` in the skill's `config.py` leaves out by default.
`ameliorates_condition` is never proposed under a `treats` edge (`SUPPORT_ONLY`): such a claim
only adds its PMID, and by default without review. `--review-support-only` on both `review` and
`apply` (or `REVIEW_SUPPORT_ONLY`) reviews those claims and counts only the upheld ones.

## Input / output

Uses the KGX records in `shared/schema.py`.

- **Input** (`--input`, default `data/filter`): the filtered baseline graph, `nodes.jsonl` and
  `edges.jsonl` (or ORION's `normalized_*.jsonl`, or `.gz`).
- **Output** (`--output`, default `data/augmentation`): `nodes.jsonl` unchanged, and
  `edges.jsonl` with every input edge in input order, each with an `id`, followed by the
  proposed edges.
  - Each checked edge is an `AugmentedEdge` with its findings in `lit_*` fields: its support
    score (how many abstracts support it), their PMIDs, the more specific edges that refine it
    (`lit_refined_by`), its context notes, and `lit_evidence` (every abstract read for the
    pair, each claim marked with whether it supports the edge). The supporting PMIDs its
    source didn't cite are appended to its `publications` and listed in
    `lit_added_publications`. An unsupported edge is checked with score 0 and its
    `publications` unchanged.
  - Originals are never removed, negated or replaced. A more specific statement that an edge
    of the pair already makes isn't added again: the original names that edge in
    `lit_refined_by`.
  - Each proposed edge names the originals it refines in `original_edge_ids`, and has
    `primary_knowledge_source: infores:kgbcia-augmentor`, `supporting_data_source:
    [infores:pubmed]`, `knowledge_level: knowledge_assertion`, `agent_type: text_mining_agent`,
    and its supporting PMIDs as `publications`.
  - The edges of a held pair (a claim that would count has no review, or names anatomy the
    ground step never looked up) are written unchanged, and counted.
- **Work directory** (`--work`, default `<output>/work`): among each step's files,
  `refinements.jsonl` (each refined pair's edges, proposed edges and links) and
  `apply_report.json`. These and the **caches** (the PubMed cache, `KGB_PUBMED_CACHE`, default
  `data/cache/pubmed/pubmed.sqlite`; the label cache, `--cache-dir` or `KGB_AUGMENT_CACHE_DIR`,
  default `data/cache/augmentation`) are described in the skill's
  [`reference/work-files.md`](../../.agents/skills/augmentation/pubmed-evidence/reference/work-files.md).

## Status

- Every step is implemented and tested without network (the skill's `tests/`, including an
  end-to-end run of this package's `main([])` with PubMed, NodeNorm, the Name Resolver and the
  models faked).
- The claims and the refinement haven't run live yet: a tuning sample, which also measures the
  cost, and a relabel of both graphs (Monarch and DrugCentral) are next. The earlier statement
  labels of both graphs stay in the cache, unread.
- No gold set yet, so the models are still unvalidated defaults.
- Open items (scope with the filter team, `negated` handling team-wide, what's not built yet)
  are in the skill's `DESIGN.md`.
- PubMed search, fetch and cache are in `shared/pubmed/`, shared with the cleaning stage's
  evidence check.
