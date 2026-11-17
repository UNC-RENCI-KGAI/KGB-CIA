# Augmentation agent

Stage 5. Finds PubMed evidence for the edges in the user's subgraph. It adds the papers that
support an edge to that edge's `publications`. Where a paper states something more specific
than the edge does, it proposes a new edge beside the original. Original edges are never removed
or replaced.

## How it works

The agent is the [**pubmed-evidence**](../../.agents/skills/augmentation/pubmed-evidence/)
skill. Its steps:

1. **select:** choose the edges whose relationships papers state as claims.
2. **pubmed:** search PubMed for each node pair and fetch the abstracts.
3. **label:** an LLM writes down the claims each abstract makes about the pair, each with a
   verbatim sentence that is checked against the abstract.
4. **ground:** map the anatomy those claims name to UBERON.
5. **review:** a stronger model reviews every claim that would count.
6. **apply:** code refines each checked edge from its pair's claims. A supported edge gains the
   abstract's PMID, and a more specific statement becomes a proposed edge.
7. **evaluate:** score a run's claims and refinements against a curator's gold set, to compare
   models and prompts. It calls no model.

The LLM only extracts and reviews claims; code makes every change to the graph. Claims are
cached, so the graph can be rebuilt without calling a model again.

More in the skill:
- [`SKILL.md`](../../.agents/skills/augmentation/pubmed-evidence/SKILL.md): the workflow an
  agent (Pi or Claude Code) follows.
- [`DESIGN.md`](../../.agents/skills/augmentation/pubmed-evidence/DESIGN.md): each step and the
  choices behind it.
- [`README.md`](../../.agents/skills/augmentation/pubmed-evidence/README.md): every step's
  options.

This package holds only `cli.py`, the entry point that `kgb-augment` and the pipeline call.

## Run it

```bash
uv run kgb-augment --input <filter output dir> --output <path>   # every step, with defaults
uv run kgb-augment STEP --help    # STEP: select, pubmed, label, ground, review, apply, evaluate
```

`make setup` installs the model SDKs; on its own, use `uv sync --extra augmentation`. Set:

| Variable | For |
| --- | --- |
| `AZURE_OPENAI_API_KEY` (and `AZURE_OPENAI_BASE_URL`) | Labeling and review with Azure OpenAI models |
| `ANTHROPIC_API_KEY`, or an `ant auth login` profile | Labeling and review with Claude models |
| `NCBI_EMAIL` | The PubMed search. NCBI asks for one. |
| `NCBI_API_KEY` | Optional: 10 PubMed requests a second instead of 3. Without it, a large job is slow, and the run says so at the start. |

Options you're most likely to need:
- `--max-label-cost`: a full run only labels as many pairs as its estimated cost allows
  (default $20; `inf` for no cap). It reports how many pairs it left out.
- `--skip-review`: leaves out the review step. Edges whose claims weren't reviewed are then
  written unchanged.
- `--reasoning-effort`: label at a different effort than the model's default.

If the work directory has a `label_run.json`, a full run continues that run with the same model
and settings, and reuses its cached claims and verdicts.

## Input / output

**Input** (`--input`, default `data/filter`): `nodes.jsonl` and `edges.jsonl` in KGX format, or
ORION's `normalized_*.jsonl`, or `.gz`.

**Output** (`--output`, default `data/augmentation`):

- `nodes.jsonl`, unchanged.
- `edges.jsonl`: every input edge, in input order and each with an `id`, followed by the
  proposed edges.
  - A checked edge has its findings in `lit_*` fields: how many abstracts support it, their
    PMIDs, the more specific edges that refine it, and every abstract read for the pair, with
    each claim marked as supporting the edge or not. Supporting PMIDs its source didn't cite
    are added to `publications` and listed in `lit_added_publications`.
  - A proposed edge names the edges it refines in `original_edge_ids`. Its provenance is
    `primary_knowledge_source: infores:kgbcia-augmentor`, `supporting_data_source:
    [infores:pubmed]`, `knowledge_level: knowledge_assertion` and `agent_type:
    text_mining_agent`, with its supporting PMIDs as `publications`.
- **Work directory** (`--work`, default `<output>/work`): each step's files, including
  `refinements.jsonl` and `apply_report.json`.

The caches are kept between runs, so a rerun costs nothing for pairs already done:
- **PubMed:** `KGB_PUBMED_CACHE`, default `data/cache/pubmed/pubmed.sqlite`. It's shared with
  the cleaning stage's evidence check.
- **Labels:** `--cache-dir` or `KGB_AUGMENT_CACHE_DIR`, default `data/cache/augmentation`.

The skill's [`reference/work-files.md`](../../.agents/skills/augmentation/pubmed-evidence/reference/work-files.md)
describes every file.

## Limitations

- No curator gold set exists yet, so the default models haven't been validated.
- `kgb-augment` only runs from a clone of the repo, because the skill's code isn't installed
  with the package (#79).
- Of COMPARTMENTS' roughly 4.8 million text-mined `located_in` pairs, a default run checks only
  the top-scoring ones, inside the 10,000-pair cap shared by all text co-occurrence edges (#97).
- The open design questions are in the skill's `DESIGN.md`.
