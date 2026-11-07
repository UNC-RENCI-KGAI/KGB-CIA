# MCP interface

**Team:** @UNC-RENCI-KGAI/mcp-interface

A read-only MCP server over a Neo4j copy of the final graph. Any MCP client, Pi included, can use
it to search nodes, explore neighbors and paths, and explain any edge down to the source file,
columns and record it came from ("why does A connect to B?").

The implementation spec is [`AGENTS.md`](AGENTS.md) in this directory.

## How it fits together

```
final graph (nodes.jsonl, edges.jsonl) + transform_report.json files
  -> orion-neo4j-dump + deploy/prepare_dump.sh -> kgb dump + artifacts tarball
  -> Helm chart (deploy/helm/kgb-mcp): Neo4j 5 Community, read-only
  <- kgbcia-mcp (this server), run locally over stdio by each MCP client
```

The server ships as its own package, `kgbcia-mcp`, so it can be installed from PyPI without the
rest of the pipeline. Lineage beyond Biolink fields comes from each edge's
`kgbcia_source_records`, resolved against the `TransformReport`s (see `agents/AGENTS.md`).

## Run it alone

Pipeline stage: an offline check that the final graph is ready to be dumped into Neo4j.

```bash
uv run kgb-mcp check --input <dir with nodes.jsonl and edges.jsonl>
```

MCP server, against a running Neo4j:

```bash
export NEO4J_URI=neo4j://<host>:7687 NEO4J_USER=<user> NEO4J_PASSWORD=<password>
export KGB_MCP_ARTIFACTS=<dir or URL of the artifacts tarball>
uv run kgbcia-mcp serve --enable-cypher            # stdio
uvx kgbcia-mcp serve --enable-cypher               # once published to PyPI
```

The dump preparation and deployment files are intentionally deferred to the next build-order
step; `deploy/RUNBOOK.md` does not exist yet.

## Input / output

**Input:** `nodes.jsonl` and `edges.jsonl` in ORION KGX format (`shared.schema.Node` / `Edge`),
from the cleaning stage or, until that exists, `data/transformation/merged/`. Every edge must
have an `id` and a `kgbcia_source_records` list. Plus the per-source `transform_report.json`
files under `data/transformation/`.

**Output:** `check` writes no graph files. `kgbcia-mcp hubs` writes `hubs.json`; the server
returns JSON-serializable, read-only query results. A later build-order step will produce the
Neo4j dump and artifact tarball (`manifest.json`, graph metadata, hubs and reports).

## Neo4j notes

Facts about the loaded database that the server relies on (index names, how IDs and lists
appear, read-only behavior), filled in after the first load.

## Status

Build-order steps 1 and 2 are implemented: the standalone uv workspace package, streaming
pre-dump check, configuration and artifact loading, TransformReport lineage, bounded Neo4j
service, stdio/HTTP FastMCP server, fixed graph tools, optional read-only Cypher, and pipeline
CLI. Unit tests use an in-code toy graph and a mocked Neo4j service; they do not need a database
or network. The server starts without Neo4j settings and creates the driver only on the first
tool query; unconfigured or unreachable Neo4j returns a structured error and hint without
breaking the MCP session. Pi registration is intentionally deferred until an instance exists.

The package pins FastMCP 3.4.11 (released October 20, 2026). Build-order step 3 remains: dump
preparation, development artifacts, indexes, Docker, Helm, local-k8s validation and the real
Neo4j integration smoke tests. Until that load confirms the assumptions, the constants marked
`TODO(k8s)` in `services/neo4j_service.py` and this README's Neo4j notes remain provisional.
