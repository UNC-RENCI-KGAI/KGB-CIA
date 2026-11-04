# Generate graph design

## Architecture

One metadata compiler emits native ORION loaders for all knowledge sources, without
knowledge-base-specific code branches. Generated packages contain a compiled mapping,
content-addressed extraction runtime and shared schema snapshot. ORION supplies `SourceDataLoader`,
`KGXFileWriter`, normalization, and merging. Transformation implements the supplied mappings;
the metadata team owns semantic corrections. Filtering, cleaning, and evaluation remain
outside this agent.

Generation works in the repository's Python 3.11+ environment. Runtime scripts pin
`robokop-orion==2.0.13`, which requires Python 3.12+. The reviewed upstream revision is
`df1e985afea18c45c5cc64b58fcfdce2ae1894e5`. ORION is a dependency rather than vendored code.

## Metadata compilation

Semantic Croissant is the executable description: record sets, grouped entities, properties,
typed IDs, code-table references, literal constants, extraction paths, and CSVW dialects.
`rcv:planLayout` links to plan entries for reporting. Plan prefix declarations supplement the
Croissant context and supply the declared Biolink version and original Croissant reference.
Column-library expansion and file-specific constants are expected in the applied Croissant;
the compiler does not reinterpret plan prose or repair incomplete semantic metadata.

Missing or contradictory instructions are recorded for handover to the metadata team, with
source, record set, field or layout references and the information needed to proceed.
Independent supported mappings continue. Missing parser capabilities for sufficiently specified
mappings are reported separately as transformation limitations.

Each record set must identify one file or FileSet. Grouped subject/object entities become
associations; an entity row and another entity's Biolink role become a directed relationship.
Flat typed role fields support JSON entities that the metadata tools could not group. Each
entity needs one mapped ID field and a Biolink category. Supported entities can still be
emitted when an association is incomplete, with the relationship gap reported.

The compiler supports code tables, dynamic prefix columns, scalar types, nulls, booleans,
declared lists, and constants. It retains flat records and category lists. Local classes or
predicates are not promoted to Biolink terms. Unmapped fields and unsupported readers are
reported. Bundle directory names identify packages because several bundles share `plan.kb`.

A loader file can exist despite incomplete support. Reports distinguish unsupported sources,
missing infores identity, partial mappings, and ready sources. An explicit override, the
metadata's `rcv:infores`, or (for older bundles) an unambiguous mapped knowledge source
establishes identity; source names do not generate infores CURIEs. A proposed id
(`rcv:inforesProposed`) runs and is reported as `infores_proposed`. Sources with no compiled record sets cannot run as successful empty ingests.

## Reader boundaries

The runtime reads delimited text with explicit headers or column order, comments, quoting,
skipped rows, file-level CSVW null markers, and compression. Field null annotations supplement
file defaults. Missing actual columns are dropped with field-level diagnostics.
JSON and JSONL use keys, indices, and wildcards under a shared record-array root. Incompatible
roots are reported rather than joined by position.

File objects use metadata URLs or local bindings. ZIP/TAR members can use declared archive
parents, including members whose content URL is relative to their archive. FileSets require
explicit local members with distinct names. Supplied checksums are
verified. XML/RDF, databases, spreadsheets, parquet, specialized biological dialects, arbitrary
JSONPath, and joins require further reader support when extraction instructions are sufficient.
Absent extraction paths, ambiguous file references, and unspecified compound-list rules require
metadata-team clarification. These cases are reported rather than guessed.

## ORION execution

Generated loaders implement source version retrieval, data acquisition, and `parse_data`.
Versions are pinned to metadata snapshots rather than polling for new releases. Parsing
versions hash mappings, runtime, schema, and the ORION version. Runtime and schema snapshots
live in support packages named by their combined content hash. Generating another source
does not overwrite support code used by earlier parsers. Legacy packages require regeneration.

A scoped writer adapter uses ORION's `DiskGraphMerger` instead of first-occurrence node
deduplication. This applies during parsing and normalization, including canonical ID collisions.
Edge records and their lineage stay intact. Mapping counts retain source emissions while KGX
file counts reflect merged nodes; native merge warnings are included in parsing metadata.

Parsing-only execution calls ORION's loader and writer and produces raw KGX. `run_orion.py`
registers the generated class and source metadata in-process and calls `IngestPipeline`.
It creates the requested workspace and passes the plan's declared Biolink version to
`NormalizationScheme`; missing versions fail explicitly. It does not edit ORION's installation
or implement another graph engine. Writer registrations are restored after each invocation.
ORION owns NodeNorm
calls and normalization policy. Lenient normalization and disabled conflation are defaults;
flags explicitly opt into strict normalization or conflation.

Unresolved identifiers and downstream normalization quality do not determine parser success.
Completion requires faithful extraction, valid graph records, preserved provenance and lineage,
and successful ORION execution for the supported content. Documented skips permit a successful
partial result; they do not claim that all source content was transformed.

Malformed records and unsupported values produce diagnostics while other records continue.
Missing files, changed checksums, crashes, or failed ORION stages return failure. ORION stage
metadata determines whether normalized outputs are usable; raw files alone do not establish
a completed graph build.

ORION caches completed stages by source, parsing, and normalization versions. Runtime local
file bindings and infores overrides are not currently included in those cache keys. Use a fresh
storage directory when those inputs or the bound file contents change. Input fingerprinting
remains a transformation integration task.

## Provenance and reports

Validate flat records with the shared `Node` and `Edge` snapshot. Extra Biolink fields remain
top-level. `kgbcia_source_records` is the explicitly allowed pipeline-specific edge property,
always emitted as a nonempty string list. Pointers use `Mapping.id#source-record-key` with
declared primary keys preferred over row numbers. Member names qualify multi-file records.
Edge IDs remain independent.

Rules appear in `TransformReport.mappings`, including distribution and field IDs, graph slots,
constants, code locations, and counts. `Skipped` documents unsupported content; diagnostics
retain individual record locations. Mapping IDs include source, record set, output kind, and
a stable rule hash. ORION owns the merging of lineage lists. Node-level publications do not
become edge evidence without an association-level mapping.

## Verification and remaining work

Tests cover compilation, package identity, JSON alignment, code tables, real ORION KGX writing,
headers, primary-key and FileSet lineage, record skips, and structural failures. Regressions
cover file null markers, archive-member URLs, JSONL alignment, incremental support isolation,
duplicate node properties, canonical collisions, Biolink versions, and workspace creation. Compiler tests
run without ORION on Python 3.11. Integration tests use the real pinned package in a Python
3.12+ environment with synthetic local data and blocked downloads.

Broader live-source coverage, large downloads, more reader adapters, and runtime stage CLI
wiring are follow-up work. Keep pipeline dry runs offline. Generated packages and run artifacts belong
under `data/`; the generator does not rewrite source bundles or the shared schema.
