# AGENTS.md: agents/

One package per pipeline stage, each owned by a different team (see `.github/CODEOWNERS`).
Stay inside the stage you were asked to work on; a change that reaches into another stage or
into `shared/` needs the `integration` label and that team's review.

## The contract every stage keeps

`shared/pipeline.py` and the `kgb-*` console scripts depend on each `cli.py` exposing:

- `DESCRIPTION`: a module-level string. `make demo` prints it, and the smoke test checks every
  stage appears in the dry-run output.
- `main(argv: list[str] | None = None) -> int`: returns 0 on success; nonzero stops the
  pipeline. The pipeline calls it as `main([])`, so a stage must run with no arguments (strong
  defaults), and must not call `sys.exit` itself.

Replace the stub body freely, but keep both names and that signature. Put the real logic in
other modules in the package and keep `cli.py` thin. Adding or renaming a stage means updating
`STAGES` in `shared/pipeline.py` and `[project.scripts]` in `pyproject.toml` together.

Read and write the records in `shared/schema.py`. Every `Edge` names its Biolink
knowledge sources, knowledge level and agent type; augmentation adds to `publications` rather
than replacing them. The transformation agent also writes a `TransformReport` per source: which
Croissant files and columns it used to make which nodes and edges.

Extra keys on a `Node` or `Edge` are Biolink slots, except these, which our stages write:

| Key | Written by | Meaning | Later stages |
| --- | --- | --- | --- |
| `kgbcia_source_records` | transformation, on every edge | The MCP interface's pointer from the edge back to its `TransformReport`: `{Mapping.id}#{source record key}`, using the source's primary key where it has one and the row number only as a fallback. | Carried through untouched. |
| `kgbcia_flags`, `kgbcia_flag_reasons` | cleaning, on a flagged edge | The edge's flags and one plain-language reason for each, in the same order (`agents/cleaning/README.md`). | Carried through; each cleaning pass replaces them, except that it keeps an inherited `rare_category_combination`. |
| `original_edge_ids` | augmentation, on a proposed edge | The ids of the edges it refines. A proposed edge has no `kgbcia_source_records`. | Carried through. |
| `lit_support_score`, `lit_supporting_publications`, `lit_added_publications`, `lit_refined_by`, `lit_context_notes`, `lit_evidence` | augmentation, on a checked edge | What the PubMed check found (the skill's `reference/formats.md`). | Carried through. |

Keep `kgbcia_source_records` a list even with one entry: ORION concatenates lists when it merges
duplicate edges, so every contributing record survives. A new key goes in this table.

When the CLI's flags or its input/output files change, update the stage's `README.md` ("Run it
alone" and "Input / output").

## What each stage does and does not do

In pipeline order (`STAGES` in `shared/pipeline.py`).
The original reasoning is in `docs/agent_plan.md`; where this list differs, this list is current.

- **metadata**: Croissant metadata per source file and column, building on RoboCroissant.
  Purely descriptive; builds no graph.
- **transformation**: follows the metadata's mappings (which columns are which Biolink class
  and predicate) and never changes them; a wrong mapping is fixed in the metadata. It writes
  and runs the code that applies them. A *full* transform of everything it can convert; choosing a
  subset is the filter stage's job. It generates an ORION parser per source from the semantic
  metadata, builds each source, and merges them into one graph with ORION's merge rules
  (`merged/`). Wrapping ORION by hand as the whole stage was ruled out; the agent writes the
  parsers.
- **cleaning**: `kgb-clean` drops edges that can't be right (no provenance, a node that doesn't
  exist, both ends unresolved by NodeNorm or an id that is wrong rather than unknown, a drop
  list, or a `treats` edge to a category that can't be treated), keeps an edge with one
  unresolved end and flags it `unresolved_node`, and flags other suspicious ones without
  removing them (Biolink type violations, rare category combinations, opposing predicates,
  self-loops), writing each flag and its reason onto the edge (`kgbcia_flags`,
  `kgbcia_flag_reasons`) and listing flagged edges in `flagged_edges.jsonl`. It runs twice: on the
  merged graph before the filter, and as `final_cleaning` on augmentation's output, applying the
  drop lists and flag lists the evidence check wrote. The evidence check runs just before it as
  its own stage, `evidence_check` (`kgb-evidence`, `agents/cleaning/evidence_check.py`), which
  runs the evidence-check skill's scripts for every question file. An edge isn't dropped just
  because no abstract supports it. See `agents/cleaning/README.md`. In `STAGES` these are three
  stages: `cleaning`, `evidence_check` (after augmentation) and `final_cleaning`, so the
  pipeline has eight.
- **filter**: the only stage that talks to the user. It turns what the user asks for ("human
  kinases and the drugs that target them", or their own dataset) into a subgraph of the cleaned
  graph; with no request, the whole graph. Every question has a default. It doesn't merge:
  transformation already did.
- **augmentation**: PubMed evidence for edges: adds the papers that support an edge to its
  `publications`, and proposes more specific edges where the papers support them. Proposed
  edges are added beside the originals, never in place of them, with the augmentation agent as
  their primary knowledge source.
- **mcp_interface**: an MCP server modeled on the OKN MCP that lets any local agent query the
  final graph: neighbors, paths, and edge lineage from the edge's knowledge
  sources and publications and the `TransformReport`.
