---
name: generate-graph
description: Compile semantic Croissant metadata into per-source ORION SourceDataLoader parsers and run them through ORION for Biolink/KGX graph construction. Use for transformation parser generation, coverage reports, and ORION integration.
---

# Generate graph

Generate native ORION parsers from mappings supplied by the metadata agent. Saved code
performs reproducible extraction; ORION owns graph construction, normalization, and merging.
Follow the current flat models in `shared/schema.py` and write a `TransformReport` for each
source. Semantic corrections belong to the metadata team.

Read [DESIGN.md](DESIGN.md) and [reference/orion_workflow.md](reference/orion_workflow.md)
before generating or running loaders. Keep source data, generated packages, and run artifacts
under gitignored `data/`.

## Inspect the semantic inputs

Read the semantic Croissant with its plan, reports, and notes. Use `rcv:planLayout` and source
field IDs. Entity groups, relationship roles, constants, code tables, prefixes, extraction
paths, and dialect declarations determine the generated behavior. Knowledge sources do not
require separate hardcoded parser templates.

Report missing, contradictory, or ambiguous instructions to the metadata team with the source,
record set, field or layout reference, observed problem, and information needed to proceed.
Do not change the supplied semantic mappings or guess XML paths, file associations, entity IDs,
infores identities, local-term equivalences, or endpoint-list pairing. Continue with independent
supported mappings where possible.

Distinguish metadata defects from unsupported parser capabilities. When mappings provide
sufficient extraction instructions but a reader or adapter is missing, report a transformation
limitation. Do not label that limitation as a metadata failure.

## Generate and inspect the package

Run `scripts/generate_parser.py INPUT OUTPUT`. The source's identity is the metadata's
`rcv:infores`; a proposed one (`rcv:inforesProposed`) is used and reported for registering.
Supply `--infores` for a single bundle only when its identity is confirmed but absent from
metadata. Inspect aggregate and per-source
generation reports. Successfully writing Python does not prove a source has supported mappings.
Reports distinguish unsupported sources, missing infores identities, and partial mappings.

Generated classes extend ORION's `SourceDataLoader`, implementing source version retrieval,
acquisition, and parsing. They share an extraction runtime and use ORION's `KGXFileWriter`.
Regenerate after mappings, runtime, or schema changes; parsing versions include their hashes.
Content-addressed support packages preserve the runtime and schema used by older parsers
during incremental generation. Regenerate legacy packages before running the current adapter.

Test representative actual records before accepting a parser. Check actual headers against
declared columns and omit absent fields with diagnostics. Confirm JSON row alignment, code
mappings, prefixes, nulls, and lists. Compound identifiers needing an unspecified additional
split must be reported rather than emitted as fabricated CURIEs.

## Preserve provenance and lineage

Emit flat nodes with category lists. Emit flat edges with `primary_knowledge_source`,
`knowledge_level`, and `agent_type`, plus aggregators and publications when the source specifies
them. Missing knowledge level and agent type use `not_provided`; code performing extraction
does not establish how the underlying knowledge was originally produced.

Every edge must contain a nonempty `kgbcia_source_records` list. Each pointer is
`Mapping.id#source-record-key`, preferring a declared primary key and falling back to a source
row number. Include member names for multi-file record sets. Keep lineage separate from edge
IDs. Write matching `Mapping` entries with distribution and field IDs, graph slots, constants,
code paths, and counts. Preserve all pointers through ORION merges. Use ORION's disk merger
for node writes during parsing and normalization so duplicate IDs retain their properties.
Mapping counts describe emitted source records; file counts describe merged nodes.
Entry-level publications must not become relationship evidence without an explicit
association mapping.

## Run through ORION

Use the generated loader for parsing-only tests or generated `run_orion.py` for native
ingestion and normalization. Raw parser KGX is not a normalized final graph. ORION requires
Python 3.12+; use the generated scripts' pinned environment without changing the repository's
Python compatibility. Bindings support local files and FileSet members. The runner creates
its workspace and passes the plan's declared Biolink version to ORION. Require that version
rather than silently normalizing against ORION's default.

Default to lenient normalization and disabled gene/protein conflation, preserving proteins
and their encoding genes as distinct entities. Explicit flags enable strict normalization or
conflation. Do not reintroduce a custom normalizer or graph merger.

Judge parser success by faithful extraction, ORION compatibility, valid graph records, and
preserved provenance and lineage. Report unresolved identifiers without requiring a minimum
NodeNorm resolution rate or changing identifiers to improve downstream scores. Normalization
quality, filtering, cleaning, and evaluation are outside transformation's responsibility.

Malformed records and unsupported fields produce diagnostics while supported records continue.
Successful partial output with documented skips is valid. Include metadata failure references
in the handover report; reporting does not authorize sending messages to another team.
Missing files, checksum mismatches, parser crashes, and failed ORION stages are structural
failures. Artifacts left by a failed ORION stage do not establish successful graph construction.
ORION reuses completed stages by version. Until runtime input fingerprinting is implemented,
use a fresh storage directory when local source files, bindings, or infores overrides change.

Use synthetic tests and mock network interactions. Native compatibility tests must exercise
the real pinned ORION loader and writer. User selection, affinity cutoffs, augmentation,
conflict cleaning, and evaluation remain in their respective stages. Future stage CLI wiring
must preserve `DESCRIPTION`, `main([])`, and an offline pipeline dry run.
