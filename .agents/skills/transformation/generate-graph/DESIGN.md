# Generate graph design

The common builder and parser API version 1 are implemented in `scripts/`. Automated parser
generation and pipeline CLI integration remain pending. The parser signature, per-source output
layout, and failure defaults described below are the current implementation contract.
Repository requirements remain the shared graph models, NodeNorm normalization, edge
provenance, fixed Biolink, full transformation, and the stage CLI contract.

## Architecture

Separate the workflow into three components:

1. The agent interprets metadata bundles and generates a parser per knowledge source.
2. Each saved parser reads source files and emits candidate records with source identifiers.
3. A common graph builder runs the parsers, normalizes identifiers, validates records, and
   serializes the graph for the filter stage.

Only parser generation requires an LLM. Parser execution and graph assembly are deterministic
for fixed source files, parser code, mappings, and recorded NodeNorm responses.

## Metadata interpretation

Use semantic Croissant record sets and field extraction paths to identify the source layout.
Use entity groups, identifier prefixes, classes, predicates, code tables, and constants to
define graph mappings. Consult the semantics plan for rationale and confidence, and the
reports, notes, and supplied answers for corrections and limitations.

Some `semantics_plan.json` layouts identify their record set only by an opaque reference
created in an earlier tool run. When the available bundle has no column-based or name-based
way to associate that layout with a physical file, the association cannot be recovered from
these inputs. Skip the layout and record the missing association; do not infer or guess a file.

Plans can also describe columns from an older source version. Before generating a parser for
each identified layout, inspect the actual downloaded file's headers and compare them with
the plan's declared columns. Drop every declared column absent from the real file, remove
dependent extraction logic, and report mappings that can no longer be produced. Do not guess
replacement columns or retain stale declarations. For headerless or nested formats, make the
equivalent check against the actual record structure using the declared dialect or paths.

For each supported layout, record which fields create nodes, which create relationships,
and which populate properties or evidence. Specify relationship direction and any conditional
predicate mapping explicitly. Validate the mapping against representative source records.
Record unmapped fields and unsupported layouts with reasons. Do not convert a local semantic
term into a Biolink class or predicate without a supported mapping.
When metadata lacks a mapping, the agent may derive one from source records and documentation,
recording its rationale and confidence. Existing metadata annotations are a starting point,
not the limit of the transformation stage's work.

## Parser contract

Each source module declares `PARSER_API_VERSION = 1` and exposes `parse(source_files, metadata)`,
yielding dictionary envelopes with `kind`, `data`, and `location`. The executable envelope
models are in [scripts/parser_contract.py](scripts/parser_contract.py); the exact payloads
and examples are in [reference/parser_contract.md](reference/parser_contract.md).
`source_files` resolves metadata file IDs to local files; `metadata` contains the loaded
documents under their manifest keys. Mapping decisions are encoded in the generated parser.
Imports must not start parsing or network requests.
Use one module per knowledge source, with helper functions for its distinct layouts.

The parser must include logic to:

* Open the declared file format and compression, using streaming readers where practical.
* Handle headers, comments, column order, quoting, nested extraction paths, list separators,
  missing values, and code tables according to the source metadata.
* Construct CURIEs from documented prefixes and native IDs without guessing namespaces.
  Preserve source-native identifiers when no documented CURIE form is available.
* Group fields describing the same entity and emit its ID, category, name, and properties.
* Construct directed subject, predicate, object relationships from explicit mappings,
  preserving measurements, units, evidence, and contributing columns.
* Expand list values only according to documented relationships. Do not create arbitrary
  Cartesian products of independent lists or turn every cross-reference into an edge.
* Emit diagnostics for malformed records, missing required fields, and unsupported mappings,
  including the source file and row number or record path.

Candidate records form an internal interface, separate from the final shared schema:

| Kind | Required content |
| --- | --- |
| Node | Original identifier, mapped Biolink category, optional name, properties, source location |
| Edge | Original subject and object identifiers, Biolink predicate, properties, `Provenance`, source location |
| Diagnostic | Code, message, severity, skipped flag, and source location |

Source location includes the file, record set, and row number or record path. Preserve that
detail in the report and use `Provenance.notes` for edge details beyond source, file, columns,
and evidence. Candidate nodes referenced by edges must also be emitted.

Keep normalization and final graph writing in the common builder so parsers share one policy.

## Graph assembly and normalization

[scripts/build_graph.py](scripts/build_graph.py) accepts a JSON build manifest listing parser
module files, metadata documents, local source files, a pinned Biolink vocabulary export, and
a normalization policy per source: strict or lenient, with lenient as the default for full
transformation. Collect distinct candidate identifiers and call `shared.nodenorm.normalize`
in batches. Use `preferred_id` for canonical IDs and inspect raw responses for preferred labels, equivalent
identifiers, and compatible types. Respect `NODENORM_URL` and record the endpoint, request
settings, and responses for replay.

The vocabulary export provides the pinned version, class parent hierarchy, and allowed
predicates. All parent references must resolve within the export and cycles are rejected.
The builder validates membership and applies the category rule below, but does not validate
predicate domain/range constraints. Preparing the export from the pinned Biolink model belongs
to the workflow. An incomplete export can cause category diagnostics; it must cover the types
and predicates needed by the source. See the reference for its format.

Construct final records using [shared/schema.py](../../../../shared/schema.py):

* For resolved nodes, `Node.id` is the NodeNorm-preferred CURIE. For unresolved nodes, apply
  the source's strict or lenient policy below. Preserve input identifiers in `original_ids`
  for emitted nodes. Prefer the normalized label when present, falling back to the source name.
* Apply one category rule across all generated parsers and the common builder: for a resolved
  node, prefer its source-mapped category when that exact category is among NodeNorm's returned
  types; otherwise choose the most specific returned Biolink type. Determine specificity using
  the recorded Biolink class hierarchy by removing types that are ancestors of another returned
  type. If multiple incomparable types remain, choose the lexicographically first category CURIE
  for a deterministic tie. Parsers preserve the source-mapped category for the builder to apply
  this rule. Unresolved nodes retained in lenient mode keep their locally mapped category.
* Coalesce repeated canonical nodes within a source graph, union original IDs, and retain
  compatible properties. For incompatible categories or values, skip only the conflicting
  candidate and preserve it in a diagnostic; continue building the source. Keep already accepted
  records using stable source-file and record order, without emitting duplicate node IDs.
  The filter stage integrates sources; semantic conflict resolution belongs to the cleaning stage.
* Rewrite `Edge.subject` and `Edge.object` through the map from original identifiers to emitted
  canonical or retained identifiers. Omit edges whose required endpoint was excluded.
  Preserve predicate direction, measurements, and every distinct source assertion.
* Attach `Provenance` from source records. NodeNorm contributes identity information,
  not relationship evidence. Deduplicate only identical edges with identical provenance.
* Keep property values within the shared models' scalar, null, and scalar-list types.
  Preserve naturally multi-valued properties as lists. Dedicated fields such as `original_ids`
  and `evidence` retain their existing meanings; nested property objects remain unsupported.

Build a separate graph per knowledge source using the same builder. Combining sources into
the baseline graph belongs to the filter stage.

## Failure policy

These policies are implemented by the builder and preserve the existing stage exit-code contract.

An unknown identifier is different from a failed NodeNorm request. No preferred ID is a normal
outcome for sources with internal record identifiers and does not inherently require data loss.
The builder accepts and records one of these policies for each source:

* Strict: exclude unresolved nodes and edges whose required endpoints are excluded.
* Lenient: retain unresolved nodes under their original identifiers, include those identifiers
  in `original_ids`, and keep the locally mapped category and available source name. Preserve
  their valid edges using the retained IDs. Report them as unresolved, not normalized.

Every candidate identifier still goes through NodeNorm in either mode. Do not invent canonical
IDs or use lenient mode to hide a failed service request.

Skip malformed or conflicting nodes, edges, or records individually and report their source
locations and reasons. Continue processing other records and exclude affected edges when their
required nodes are absent. A source with real output and some skipped records is a normal,
successful partial result with exit status 0. Record exclusions and documented unsupported
content in the coverage report, distinguishing partial coverage from a complete transform.

Return a nonzero builder status only for structural failures: a required source file cannot be
downloaded or read, the normalization service is unreachable or fails its request, or a parser
crashes or violates its interface. These stop the affected source build and halt subsequent
pipeline stages when the runtime CLI forwards the status under the existing runner contract.
The standalone builder still processes other listed sources and writes their statuses.
Row-level diagnostics and unresolved IDs handled by the configured policy do not cause a
nonzero status. Invalid global configuration fails before source processing begins.

## Validation and artifacts

Validate classes and predicates against the recorded Biolink version, instantiate the shared
Pydantic models, and check graph integrity separately: unique node IDs, existing endpoints,
and populated source, file, and contributing columns for each edge. Serialization must round
trip through `Graph.model_validate_json`.

Builder tests cover CSV parser execution, gzip metadata, list-valued properties, provenance,
normalization collisions, strict and lenient handling of unknown IDs, category selection,
record-level diagnostics, structural failures, endpoint rewriting, and schema round trips.
Generated source parsers must add tests for their own layouts, list expansion, stale columns,
and unresolvable layout references. Use canned NodeNorm responses so tests run without network
access. Re-running a saved parser with the same source files and normalization responses must
reproduce graph content.

Pytest `testpaths` includes root `tests/` and this skill's `tests/` directory, so `make test`
collects the builder tests. Keep test examples synthetic and source dumps out of git.

Deliver parser code, graph JSON per source, a coverage and diagnostic report, and a run manifest.
The manifest records source and metadata checksums, source versions, parser checksum or version,
generation model ID, Biolink version, and per-source normalization policies and settings.
Store source data, graphs, reports, and normalization snapshots under gitignored `data/`.

The builder writes `graph.json`, `report.json`, and `normalization.json` in a directory per
source, plus `build_report.json` for the run. Paths in the input manifest resolve relative to
that manifest, independent of the shell working directory. The source report records checksums,
source version and generation model ID when supplied, vocabulary version, and normalization
settings. Normalization snapshots allow an offline replay that must cover every requested ID.

Each artifact is replaced atomically. A failed source rebuild does not overwrite its prior
graph; the current build summary marks that source failed with no usable graph path. Consumers
must check this summary. Concurrent builds use separate output directories. Candidate records
and the final graph currently reside in memory; large-scale streaming remains future work.

Connect the runtime through `agents/transformation/cli.py`, keeping `DESCRIPTION` and
`main(argv: list[str] | None = None) -> int`. The pipeline calls `main([])`, so provide strong
defaults without interactive prompts or required CLI arguments. Return 0 for successful builds,
including partial results with record-level diagnostics, and nonzero only for structural
failures as defined above; do not call `sys.exit` inside `main`.

Keep CLI orchestration thin and preserve an offline pipeline dry run. Update the runtime
stage's `agents/transformation/README.md` when flags or input/output files change. Changes to
shared contracts require the repository's integration review. Add dependencies through
`uv add` and run `make test` and `make demo` before submitting an implementation PR.
Evaluation is outside the stage.
