# Augmentation agent

**Team:** @UNC-RENCI-KGAI/augmentation · **Existing asset:** PubMed; Bagel / LitCoin for text

Find PubMed evidence for edges: add the papers that support an edge to its `publications`, and
propose more specific edges where the papers support them. Proposed edges sit beside the
originals, never in place of them.

The agent is the **pubmed-evidence** skill, in
[`.agents/skills/augmentation/pubmed-evidence/`](../../.agents/skills/augmentation/pubmed-evidence/).
This package keeps only `cli.py`, the stage's entry point: `kgb-augment` and
`shared/pipeline.py` call it, and it hands its arguments to the skill's library
(`scripts/pubmed_evidence/`).

| Skill file | For |
| --- | --- |
| [`SKILL.md`](../../.agents/skills/augmentation/pubmed-evidence/SKILL.md) | the workflow an agent (Pi or Claude Code) follows |
| [`README.md`](../../.agents/skills/augmentation/pubmed-evidence/README.md) | every option and environment variable |
| [`DESIGN.md`](../../.agents/skills/augmentation/pubmed-evidence/DESIGN.md) | each step and the choices behind it |
| [`reference/formats.md`](../../.agents/skills/augmentation/pubmed-evidence/reference/formats.md) | the output graph's fields |
| [`reference/work-files.md`](../../.agents/skills/augmentation/pubmed-evidence/reference/work-files.md) | the work files and caches |

## How it fits the pipeline

The stage runs after the filter and before the evidence check and the final cleaning pass
(`STAGES` in `shared/pipeline.py`). It runs six steps:

1. `select` chooses the edges whose relationships papers state as claims, grouped by node pair.
2. `pubmed` searches PubMed for each pair, through `shared/pubmed/`, which the cleaning stage's
   evidence check shares.
3. `label` has an LLM write down the claims each abstract makes about the pair, each with a
   verbatim sentence that code checks.
4. `ground` grounds the claims' anatomy to UBERON.
5. `review` has a second pass (gpt-6-luna at medium effort) review every claim that would
   count.
6. `apply` refines every checked edge from its pair's claims, in code.

The LLM only extracts and reviews claims, and the claims are cached, so the graph can be
rebuilt without calling a model.

## Run it alone

```bash
uv run kgb-augment --input <filter output dir> --output <path>   # every step, with defaults
uv run kgb-augment STEP --help    # STEP: select, pubmed, label, ground, review, apply, evaluate
```

- **Install:** the model SDKs (`openai`, `anthropic`) are the project's `augmentation` extra.
  `make setup` installs them, or `uv sync --extra augmentation`; the other stages don't need
  them.
- **Credentials:** `AZURE_OPENAI_API_KEY` for the default models (or `ANTHROPIC_API_KEY` with
  Claude models), and `NCBI_EMAIL` plus `NCBI_API_KEY` for the PubMed search.
- **With no step** (as the pipeline calls it) the run continues the label run that
  `label_run.json` in the work directory names, labels only as many pairs as $20 of estimated
  cost allows (`--max-label-cost`), and flags how many it left out. `--skip-review` leaves out
  the review; step 6 then writes the edges of every pair with an unreviewed claim unchanged.

The skill's `scripts/augment.py` takes the same arguments. Its
[README](../../.agents/skills/augmentation/pubmed-evidence/README.md) lists every option and
environment variable, and its
[SKILL.md](../../.agents/skills/augmentation/pubmed-evidence/SKILL.md) says when to use them.

## Input / output

Uses the KGX records in `shared/schema.py`. The skill's
[`reference/formats.md`](../../.agents/skills/augmentation/pubmed-evidence/reference/formats.md)
describes every field.

- **Input** (`--input`, default `data/filter`): the filtered baseline graph, `nodes.jsonl` and
  `edges.jsonl` (or ORION's `normalized_*.jsonl`, or `.gz`).
- **Output** (`--output`, default `data/augmentation`): `nodes.jsonl` unchanged, and
  `edges.jsonl` with every input edge in input order, each with an `id`, followed by the
  proposed edges.
  - A checked edge is an `AugmentedEdge` with its findings in `lit_*` fields. The supporting
    PMIDs its source didn't cite are appended to its `publications`.
  - A proposed edge names the originals it refines in `original_edge_ids`, and has
    `primary_knowledge_source: infores:kgbcia-augmentor`.
  - Originals are never removed, negated or replaced. Edges that weren't checked are written
    as read, with an `id`.
- **Work directory** (`--work`, default `<output>/work`): each step's files, among them
  `refinements.jsonl` and `apply_report.json`.
- **Caches:** the PubMed cache and the label cache, under `data/cache` by default
  (`KGB_CACHE_DIR`).

## Status

- Every step is implemented and tested without network (the skill's `tests/`, including an
  end-to-end run of this package's `main([])` with PubMed, NodeNorm, the Name Resolver and the
  models faked).
- Open items (`negated` handling team-wide, what's not built yet) are in the skill's
  `DESIGN.md`, "Open items".
