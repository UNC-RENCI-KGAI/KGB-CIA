# ORION parser workflow

The integration targets `robokop-orion==2.0.13` and its native
[`SourceDataLoader`](https://github.com/RobokopU24/ORION/blob/df1e985afea18c45c5cc64b58fcfdce2ae1894e5/orion/loader_interface.py)
and [`KGXFileWriter`](https://github.com/RobokopU24/ORION/blob/df1e985afea18c45c5cc64b58fcfdce2ae1894e5/orion/kgx_file_writer.py).

## Generated package

```text
OUTPUT/
  registry.json
  generation_report.json
  run_orion.py
  _kgbcia_support_HASH/
    __init__.py
    orion_runtime.py
    schema.py
  parsers/
    Kgbcia_SOURCE/
      src/
        parser.py
        semantic_mapping.json
        Kgbcia_SOURCE.source.json
        generation_report.json
```

Classes extend `SemanticSourceLoader`, which extends ORION's `SourceDataLoader`. They declare
source identity, parsing version, and mapping location; inherited methods acquire and extract
explicitly mapped records using ORION's writer.

## Generate

```bash
uv run python .agents/skills/transformation/generate-graph/scripts/generate_parser.py INPUT OUTPUT
```

Input may be a semantic JSON/gzip file, bundle directory, or collection. For one bundle,
`--infores infores:SOURCE` supplies a confirmed missing identity. Store output under `data/`.
The source ID is `Kgbcia_` plus the normalized bundle directory name. Registry entries survive
incremental regeneration. Metadata, runtime, and schema changes alter the parsing version.
Each parser imports its support snapshot by content hash. Other sources keep their original
snapshots when one source is regenerated. Regenerate packages created before this layout
before using the current runner.

Inspect aggregate and per-source generation reports. Counts distinguish compiled and
unsupported record sets; `skipped` also documents field and relationship gaps. `needs_infores`
requires a confirmed identity at runtime or regeneration. `unsupported` means no extractable
record sets. Generation exit 0 means compilation completed, not that source coverage is complete.

## Files and parsing-only tests

An optional binding JSON maps metadata file IDs to paths relative to that JSON file:

```json
{
  "file_001": "sources/records.tsv",
  "fileset_002": ["sources/part-1.json.gz", "sources/part-2.json.gz"]
}
```

FileSets require explicit members with distinct names. Unbound file objects use their declared
URL or archive parent. Relative member URLs use their declared archive parent. Bindings do
not bypass supplied checksums. Inspect required downloads
before running; they may be large.

```bash
uv run --script OUTPUT/parsers/Kgbcia_SOURCE/src/parser.py PARSED --source-files FILES.json
```

The script dependency metadata selects Python 3.12+ and the pinned ORION package. Optional
`--infores` supplies source identity. ORION writes `PARSED/nodes.jsonl` and `PARSED/edges.jsonl`;
the loader writes `PARSED/transform_report.json` and `PARSED/diagnostics.jsonl`. This output is
raw, not normalized. Exit 0 permits documented skips; structural failure returns 1 and may
leave partial artifacts that must not be treated as a successful graph.

## Native ingestion

```bash
uv run --script OUTPUT/run_orion.py OUTPUT Kgbcia_SOURCE STORAGE --source-files FILES.json
```

The adapter registers the loader and its absolute source metadata in the current process and
creates `STORAGE` and calls ORION's `IngestPipeline.run_pipeline` with lenient normalization,
disabled conflation, and the semantic plan's declared Biolink version. Missing versions fail
before downloading source files. `--strict` excludes unresolved nodes. `--conflation` explicitly opts into
gene/protein and other conflations. Optional `--infores` supplies missing source identity.
ORION owns versioned storage, normalization artifacts, and stage success. Failed stages return
nonzero. A scoped node writer uses ORION's disk merger during parsing and normalization to
retain properties from duplicate source IDs and canonical ID collisions. Registrations are
restored afterward. Combined final graphs use ORION's graph workflow.

Unresolved IDs retained in lenient mode do not constitute parser failure. Report normalization
outcomes, but leave canonicalization quality and downstream evaluation to ORION and the
responsible teams. Validate graph structure, provenance, and lineage independently of the
fraction of identifiers that resolve.

ORION reuses completed stages by version. The current runner does not fingerprint local file
contents, file bindings, or infores overrides. Use a fresh `STORAGE` directory when changing
these runtime inputs to avoid reusing an earlier graph.

To contribute a generated loader to an ORION checkout, register its module and class in
`SOURCE_DATA_LOADER_CLASS_IMPORTS` and keep the source JSON beside it. The local adapter avoids
editing the installation for tests.

## Semantic contract and limitations

Supported constructs include declared table dialects, JSON/JSONL key/index/wildcard paths,
code tables, prefix columns, CSVW file nulls plus field null annotations, boolean mappings,
lists, constants, grouped associations,
and typed entity-role relationships. Header checks omit absent columns. JSON records stay
aligned under a shared array root in both JSON and JSONL. Unknown codes and compound IDs become diagnostics.
Two multivalued endpoints are not cross-joined without explicit pairing semantics.

XML/RDF, SQL, specialized dialects, and joins need reader adapters when explicit extraction
instructions are available. Missing paths, undocumented namespaces, and unspecified nested
separators are metadata issues. Report them with source and field/layout references for the
metadata team; keep parser limitations separate. Do not reconstruct missing mappings, invent
source identities, or modify the semantic inputs. Supported mappings can still complete with
documented omissions.

Records are flat. Every edge has required knowledge-source fields and a nonempty
`kgbcia_source_records` list, with corresponding rules in `TransformReport`. Missing knowledge
level and agent type use `not_provided`. A declared primary key becomes a compact JSON tuple
in the pointer; row number is the fallback. FileSet member names disambiguate members.
ORION merging must retain these list values when assertions collapse.

## Tests

`make test` includes compiler tests; native tests skip when ORION is unavailable. Run native
integration tests against the real package with:

```bash
uv run --no-project --python 3.12 --with robokop-orion==2.0.13 --with pytest \
  python -m pytest -q .agents/skills/transformation/generate-graph/tests
```

Tests use synthetic local files and block downloads. Live acquisition and NodeNorm calls are
not part of the suite. Regenerate packages after runtime or schema updates to invalidate old
ORION parsing caches.
