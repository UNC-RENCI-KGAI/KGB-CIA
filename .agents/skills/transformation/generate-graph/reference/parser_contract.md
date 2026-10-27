# Parser API version 1

The executable contract is defined in [parser_contract.py](../scripts/parser_contract.py).
Generated parsers yield ordinary Python dictionaries; they do not need to import the contract
module. The common [builder](../scripts/build_graph.py) validates each record.

## Module interface

Declare `PARSER_API_VERSION = 1` and expose `parse(source_files, metadata)` as an iterable
of record dictionaries. Keep the module self-contained, using installed dependencies and
helper functions defined within it. Imports must not read source data or make network requests.

* `source_files` maps metadata file IDs to resolved local `pathlib.Path` objects.
* `metadata` maps the manifest's document names to loaded content. JSON and gzip-compressed
  JSON become Python objects; notes and other text documents become strings.
* Yield nodes, edges, and diagnostics in stable source-file and record order. Edges may precede
  their nodes. Emit every referenced node; the builder does not synthesize missing endpoints.
* Retain source identifiers in node `id` and edge endpoints. Apply documented namespace
  conversions before yielding records. Normalization and final category selection belong to
  the builder. Do not call an LLM or NodeNorm from a parser.

Every envelope has exactly these fields:

```python
{
    "kind": "node",  # node, edge, or diagnostic
    "data": {"id": "SOURCE:123", "category": "biolink:Gene", "name": "Example"},
    "location": {"file": "genes.csv", "record_set": "genes", "record": 2},
}
```

`location.file` and `location.record_set` are nonempty strings. `location.record` is an integer
row number or a string record path, such as a JSON path. Use a descriptive string such as
`layout` for diagnostics about an entire layout.

## Record payloads

For `node`, `data` follows `shared.schema.Node`: required `id` and source-mapped `category`,
optional `name`, `original_ids`, and `properties`. For `edge`, `data` follows
`shared.schema.Edge`: required `subject`, `predicate`, `object`, and `provenance`, with optional
`properties`. Properties support scalars, null, and lists of scalar values under the shared
schema. Unknown top-level payload fields are rejected rather than silently discarded.

Edge provenance must include the manifest's source `name`, a nonempty `file` matching
`location.file`, and nonempty contributing `columns`. Supply `evidence` and `notes` when
available. Do not attach evidence to an edge unless it supports that assertion.

```python
{
    "kind": "edge",
    "data": {
        "subject": "SOURCE:123",
        "predicate": "biolink:related_to",
        "object": "SOURCE:456",
        "properties": {"supporting_ids": ["REF:1", "REF:2"]},
        "provenance": {
            "source": "example",
            "file": "genes.csv",
            "columns": ["subject", "object"],
            "evidence": ["PMID:123"],
        },
    },
    "location": {"file": "genes.csv", "record_set": "relationships", "record": 2},
}
```

For `diagnostic`, `data` contains required strings `code` and `message`, optional `severity`
(`info`, `warning`, or `error`; default `warning`), and `skipped` (boolean, default false).
Set `skipped` to true when a record or layout was excluded, including stale mappings or
unidentifiable layout references. A diagnostic's severity does not control the exit code.

```python
{
    "kind": "diagnostic",
    "data": {"code": "missing_id", "message": "No subject identifier", "skipped": True},
    "location": {"file": "genes.csv", "record_set": "relationships", "record": 3},
}
```

Known envelope kinds with invalid payloads or locations produce record-level diagnostics.
An unknown kind, non-dictionary envelope, unsupported API version, missing `parse`, or uncaught
parser exception is a structural failure. Catch expected bad-row errors inside the parser,
yield a diagnostic, and continue; do not catch unavailable source files or mask a parser crash.

The agent must check actual file headers before generating extraction code. Skip layouts whose
file cannot be identified from available metadata, and drop declared columns missing from the
downloaded file. The builder validates parser output; it does not reconstruct layout mappings.

## Build manifest

Run from the repository environment:

```bash
uv run python .agents/skills/transformation/generate-graph/scripts/build_graph.py build.json
```

All manifest paths resolve relative to the manifest file. Use local source files already
downloaded by the workflow. The builder does not download data or generate parsers. Keep
manifests, source data, and build outputs under gitignored `data/`.

```json
{
  "biolink": "biolink-vocabulary.json",
  "output_dir": "graphs",
  "batch_size": 500,
  "sources": [
    {
      "name": "example",
      "parser": "parsers/example.py",
      "metadata": {
        "croissant": "metadata/croissant.semantic.json.gz",
        "semantics_plan": "metadata/semantics_plan.json",
        "notes": "metadata/notes.md"
      },
      "source_files": {"table": "sources/genes.csv"},
      "normalization_policy": "lenient",
      "source_version": "release identifier",
      "generation_model_id": "model used to generate the parser"
    }
  ]
}
```

Required global fields are `biolink` and a nonempty `sources` list. `output_dir` defaults to
`graphs` beside the manifest. `batch_size` defaults to 500. Optional `nodenorm_url` overrides
the shared helper's endpoint, which otherwise respects `NODENORM_URL`.

Each source requires a unique lowercase `name` using letters, digits, underscores, or
hyphens; `parser`; and the `metadata` and `source_files` mappings. Metadata document names
must match what the generated parser expects. Source file keys must match the metadata file
IDs used during parser generation. Normalization policy defaults to `lenient`; use `strict`
to exclude unresolved nodes and affected edges. Version and generation model fields are
optional but should be supplied when known.

Set a source's optional `normalization_responses` to a saved `normalization.json` file to replay
normalization without network access. Every candidate identifier must appear in the snapshot,
including explicit null entries for unknown IDs. Missing snapshot coverage is a structural
failure; replay never silently contacts the live service.

## Biolink vocabulary input

Supply a JSON export of the pinned Biolink release used by the transformation workflow. It
must have a nonempty `version` string, a `classes` object mapping category CURIEs to lists of
direct parent CURIEs, and a nonempty `predicates` list of allowed predicate CURIEs. Every parent
must also be a key in `classes`; roots have empty parent lists. Include all types returned by
NodeNorm that the workflow needs, their ancestors, and the allowed predicates from the release.
Do not derive the hierarchy by guessing from source columns or NodeNorm response order.

The builder validates membership and hierarchy consistency, not the export's authenticity or
predicate domain/range constraints. It records the export version and checksum. The workflow
must prepare this export from its pinned Biolink model; an exporter is not part of this script.
For resolved nodes, prefer the mapped category when returned; otherwise remove ancestor types
and select the lexicographically first remaining type. A resolved node with no returned type
in the export is skipped with an `invalid_category` diagnostic.

## Outputs and status

The output directory contains `build_report.json` with each source's current status and graph
path. Each successful source has its own directory with:

* `graph.json`: serialized `shared.schema.Graph`, with no dangling edge endpoints.
* `report.json`: counts, diagnostics, normalization policy, and a run manifest with input
  checksums, source version, generation model ID, vocabulary version, and normalization settings.
* `normalization.json`: raw responses for replay, including null entries for unresolved IDs.

The builder appends row/path lineage to `Provenance.notes`. Exact duplicate edges including
provenance are removed; distinct source locations remain distinct assertions. Repeated node
IDs coalesce when categories and overlapping properties agree. Conflicting candidates are
skipped in parser yield order, retaining previously accepted records.

Exit 0 means no structural failures, including successful partial output with skipped records.
Unresolved IDs retained in lenient mode produce informational diagnostics without marking
coverage partial. A source with exclusions has status `partial`; an empty graph remains visible
through counts and diagnostics. The skipped-record count measures rejected candidate records
and parser skip diagnostics, not the unknown number of rows in an unsupported layout.
Structural failures produce a failed source report and exit 1;
other listed sources are still processed. Invalid global configuration fails before sources run.

Check the exit status and current `build_report.json` and source report before consuming graph files.
A failed rebuild does not replace a graph from an earlier successful run; its current summary
entry has `graph` set to null. Invalid global configuration writes no new summary, so an earlier
summary cannot establish success for that invocation. Individual artifacts are replaced atomically. Concurrent builds
must use separate output directories. The current implementation holds graph candidates in
memory; streaming output for very large sources remains future work.
