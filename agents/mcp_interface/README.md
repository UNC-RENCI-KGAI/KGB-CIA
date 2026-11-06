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

Building the dump and deploying Neo4j: see `deploy/RUNBOOK.md`.

## Input / output

**Input:** `nodes.jsonl` and `edges.jsonl` in ORION KGX format (`shared.schema.Node` / `Edge`),
from the cleaning stage or, until that exists, `data/transformation/merged/`. Every edge must
have an `id` and a `kgbcia_source_records` list. Plus the per-source `transform_report.json`
files under `data/transformation/`.

**Output:** no graph files. This stage produces a Neo4j dump, an artifacts tarball (reports,
graph metadata, hub list), and a running MCP server.

## Neo4j notes

Facts about the loaded database that the server relies on (index names, how IDs and lists
appear, read-only behavior), filled in after the first load.

## Status

Not implemented yet. Implementation follows the build order in `AGENTS.md`.
