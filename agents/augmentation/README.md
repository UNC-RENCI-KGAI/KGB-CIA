# Augmentation agent

**Team:** @UNC-RENCI-KGAI/augmentation · **Existing asset:** PubMed; Bagel / LitCoin for text

Enrich the baseline graph with extra datasets and PubMed evidence for or against edges.

The agent is the **pubmed-evidence** skill,
[`.agents/skills/augmentation/pubmed-evidence/`](../../.agents/skills/augmentation/pubmed-evidence/):

- [`SKILL.md`](../../.agents/skills/augmentation/pubmed-evidence/SKILL.md): the workflow an
  agent (Pi or Claude Code) follows;
- [`DESIGN.md`](../../.agents/skills/augmentation/pubmed-evidence/DESIGN.md): each step and the
  choices behind it;
- [`README.md`](../../.agents/skills/augmentation/pubmed-evidence/README.md): how to run it,
  script by script;
- `reference/`, `prompts/`, `scripts/` (the library is `scripts/pubmed_evidence/`) and `tests/`.

This package keeps only `cli.py`, the stage's entry point: `kgb-augment` and
`shared/pipeline.py` call it, and it hands its arguments to the skill's library.

In short: choose the edges whose relationships papers state as claims, search PubMed for each
node pair, have an LLM label every abstract against every edge (with a checked verbatim quote)
and propose Biolink qualifiers, have a stronger model review the labels that would add edges,
then add the supporting PMIDs to the edges' `publications` and new edges for reviewed
contradictions (`negated: true`) and qualifiers. The LLM only labels; code does the rest, and
the labels are cached, so the graph can be rebuilt without calling a model.

## Run it alone

```bash
uv run kgb-augment --input <filter output dir> --output <path>   # every step, with defaults
uv run kgb-augment STEP --help    # STEP: select, pubmed, sentences, label, review, apply, evaluate
```

Each step is also a script in the skill's `scripts/` (`select_edges.py`, `search_pubmed.py`,
...), with the same options; the skill's README lists them. Set `RENCI_AZURE_API_KEY` for
labeling and review, and `NCBI_EMAIL` (plus `NCBI_API_KEY` for 10 requests a second) for the
PubMed search. With a `label_run.json` in the work directory, the full run continues that run
(its model, prompt and reviewer) and reuses its cached labels.

## Input / output

Uses the KGX records in `shared/schema.py`.

- **Input** (`--input`, default `data/filter`): the filtered baseline graph, `nodes.jsonl` and
  `edges.jsonl` (or ORION's `normalized_*.jsonl`, or `.gz`).
- **Output** (`--output`, default `data/augmentation`): `nodes.jsonl` unchanged, and
  `edges.jsonl` with every input edge in input order, supporting PMIDs appended to
  `publications`, followed by the new negated and then qualified edges. Every new edge has
  `primary_knowledge_source: infores:kgbcia-augmentor` (`--infores`), `knowledge_level:
  knowledge_assertion` and `agent_type: text_mining_agent`.
- **Work directory** (`--work`, default `<output>/work`) and **caches** (`--cache-dir`, default
  `data/augmentation/cache`): described in the skill's
  [`reference/work-files.md`](../../.agents/skills/augmentation/pubmed-evidence/reference/work-files.md).

## Status

- Every step is implemented and tested without network (the skill's `tests/`, including an
  end-to-end run of this package's `main([])` with PubMed, NodeNorm and the models faked).
- Tested on Monarch's ORION output (`data/normalized_2026jul22_...`, 24,641 edges): step 2
  kept 4,985 pairs (213 ClinGen gene–disease, 4,772 dictyBase and Xenbase gene–phenotype). A
  40-pair sample ran through real PubMed, gpt-6-luna labeling and gpt-5.6-terra review for
  about $0.08: 133 confirmed labels added 144 PMIDs to 20 edges, and review accepted 12 of 15
  qualifier proposals, which became 7 qualified edges (loss-of-function forms, UBE3A's missing
  expression). Gene–disease searches found papers for 18 of 20 pairs; Xenbase phenotype names
  found none, and dictyBase phenotypes have no names to search.
- No gold set yet, so the models are still unvalidated defaults.
- Open items (scope with the filter team, `negated` handling team-wide) are in
  the skill's `DESIGN.md`.
