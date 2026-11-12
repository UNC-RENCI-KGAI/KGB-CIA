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
# For a local development graph whose reports are outside data/transformation/:
uv run kgb-mcp check --input <graph-dir> --report <transform_report.json>
```

MCP server, against a running Neo4j:

```bash
# Authenticated Neo4j (the local-k8s default): set both variables.
export NEO4J_URI=bolt://<host>:7687 NEO4J_USER=<user> NEO4J_PASSWORD=<password>
export KGB_MCP_ARTIFACTS=<dir or URL of the artifacts tarball>
export KGB_MCP_ARTIFACTS_SHA256=<required when artifacts is a URL>
uv run kgbcia-mcp serve --enable-cypher            # stdio
uvx kgbcia-mcp serve --enable-cypher               # once published to PyPI
```

For the local authenticated cluster, port-forward Bolt and source the local ignored deployment
environment before starting Pi or the server:

```bash
kubectl -n <namespace> port-forward svc/kgb-mcp-neo4j 7687:7687
set -a; . agents/mcp_interface/deploy/.env; set +a
uv run kgbcia-mcp serve --enable-cypher
```

For the prepared AWS final host, use [`deploy/aws/README.md`](deploy/aws/README.md). It deploys
only Neo4j from the same chart with the LitCoin test overlay and keeps `mcp.enabled=false`; run
`kgbcia-mcp` locally over stdio against the host's Bolt Service.

## Input / output

**Input:** `nodes.jsonl` and `edges.jsonl` in ORION KGX format (`shared.schema.Node` / `Edge`),
from the cleaning stage or, until that exists, `data/transformation/merged/`. Every edge must
have an `id` and a `kgbcia_source_records` list. Plus the per-source `transform_report.json`
files under `data/transformation/`.

**Output:** `check` writes no graph files. `kgbcia-mcp hubs` writes `hubs.json`; the server
returns JSON-serializable, read-only query results. `deploy/prepare_dump.sh` produces the
Neo4j dump and artifact tarball (`manifest.json`, graph metadata, hubs and reports).

## Neo4j notes

Confirmed on the local-k8s Neo4j 5.26.31 load in November 2026:

- Every graph node has the ``biolink:NamedThing`` anchor label. Categories are labels, not a
  `category` property; node CURIEs are in `id`.
- Relationship IDs are stored in `r.id`; provenance lists, including
  `kgbcia_source_records`, return as native Neo4j string arrays.
- `kgbcia_node_fulltext` is online over `name`, `synonym`, and the StringArray
  `equivalent_identifiers` property. The generated `kgbcia_relationship_id_*` range indexes are
  online for relationship `id` properties.
- The chart's `server.databases.default_to_read_only=true` rejects a graph `CREATE` through a
  raw driver session as well as through `query_cypher`.
- Neo4j Community permits the separate `kgbcia` account to create and drop users in `system`
  despite the graph database being read-only. AWS sets `NEO4J_AUTH=none`, so there is no
  application account or reusable password, but an unauthenticated raw client can still create
  system users. Network exposure remains the maintainer's responsibility; graph writes remain
  blocked by the database-level read-only setting and resource limits.

## Status

Build-order steps 1 through 3 are implemented: the standalone uv workspace package, streaming
pre-dump check, configuration and artifact loading, TransformReport lineage, bounded Neo4j
service, stdio/HTTP FastMCP server, dump preparation, indexes, Docker image, Helm chart, and
local-k8s deployment. The live integration smoke test exercises graph information, schema,
node and edge lookup, paths, connected nodes, lineage, Cypher reads, and raw-driver write
rejection against the 200-abstract development graph.

The package pins FastMCP 3.4.11 (released October 20, 2026). Pi registration is configured for
local stdio use; source the ignored deployment environment before launching Pi.

AWS build-order step 4 was completed on November 12, 2026 against the LitCoin development
sample on the prepared arm64 k3s host. The checksum-marker retry path preserved the loaded PVC
after an interrupted Helm client, and the account hook was corrected to treat Neo4j's
unchanged-password response as idempotent success. Laptop verification confirmed Bolt login as
`kgbcia`, raw graph-write rejection, the AWS integration smoke suite, and a local-stdio
`kgbcia-mcp serve` answer with edge lineage. `kgbcia` can create and drop system users on AWS,
confirming the Neo4j Community security risk. The selected AWS chart configuration now disables
Neo4j authentication (`NEO4J_AUTH=none`); laptop clients set only the Bolt URI and artifacts
variables, while local k8s remains authenticated.

Checkpoint 3 AWS verification completed after Helm revision 2 deployed the auth-disabled sample
release. An unauthenticated graph read returned 1,192 nodes, and a graph `CREATE` was
rejected with `Neo.ClientError.General.WriteOnReadOnlyAccessDatabase`. An anonymous `CREATE USER`
in `system` succeeded, then its uniquely named temporary probe user was dropped successfully.
This confirms the no-auth service still permits raw system-user administration, even though
Graph writes remain read-only. The initial dynamic-setting probe did not execute because its
preliminary `SHOW SETTINGS` query requested a non-existent `dynamic` column. The corrected
probe confirmed the configured 30-second transaction timeout, but
`dbms.setConfigValue('db.transaction.timeout', '31s')` was rejected because the procedure is not
registered; no setting changed. Both `STOP DATABASE neo4j WAIT 60 SECONDS` and the exact
`STOP DATABASE neo4j` command were rejected as unsupported administration commands, with no
database state change, so no restart or recovery was needed. Read-only mode protects the graph,
but it does not prevent anonymous `CREATE USER`; no-auth AWS is safe only while the maintainer
keeps Bolt exposure restricted.

Note that on `main`, `kgb-mcp` is a stub that prints "not implemented yet". A first MCP server backed by Neo4j is in [#64](https://github.com/UNC-RENCI-KGAI/KGB-CIA/pull/64) (in review), and is being deployed on AWS for the hosted demo.
