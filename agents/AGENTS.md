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

When the CLI's flags or its input/output files change, update the stage's `README.md` ("Run it
alone" and "Input / output").

## What each stage does and does not do

From `docs/agent_plan.md`, which has the reasoning:

- **metadata**: Croissant metadata per source file and column, building on RoboCroissant.
  Purely descriptive; builds no graph.
- **transformation**: decides which columns map to which Biolink class and predicate, then
  writes and runs code to do it. A *full* transform of everything it can convert; choosing a
  subset is the filter stage's job. ORION may supply deterministic pieces, but wrapping ORION
  as the whole stage was ruled out.
- **filter**: the only stage that talks to the user, and every question has a default
  (affinity cutoffs, binding-strength bins, edge types to keep). Merges triples into one
  connected baseline graph. Keep that baseline distinguishable from anything added later.
- **augmentation**: extra datasets (possibly sent back through metadata → filter) and PubMed
  evidence for or against edges.
- **cleaning**: resolves conflicts between sources and prunes to the final graph. No design yet;
  check the team's tracking issue before building.
- **mcp_interface**: an MCP server modeled on the OKN MCP that lets any local agent query the
  final graph: neighbors, paths, and edge lineage from the edge's knowledge
  sources and publications and the `TransformReport`.
