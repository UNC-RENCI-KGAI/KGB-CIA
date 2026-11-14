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

Prepare or refresh release artifacts:

```bash
# Full graph-to-dump release: runs ORION, then computes MCP statistics on a temporary Neo4j.
bash agents/mcp_interface/deploy/prepare_dump.sh \
  --graph-dir <dir with nodes.jsonl and edges.jsonl> \
  --reports-dir <dir containing transform_report.json files> \
  --graph-metadata <merge-metadata.json or graph metadata JSON> \
  --description data/describe/description.json \
  --output-dir <prepared-output-dir> \
  --graph-version <version>

# Artifacts-only refresh for an already deployed kgb.dump: skips ORION and copies the dump unchanged.
bash agents/mcp_interface/deploy/prepare_dump.sh \
  --artifacts-only \
  --existing-dump <existing kgb.dump> \
  --reports-dir <dir containing transform_report.json files> \
  --graph-metadata <merge-metadata.json or graph metadata JSON> \
  --description data/describe/description.json \
  --output-dir <prepared-output-dir> \
  --graph-version <version>
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

An artifacts URL is downloaded and unpacked once, by checksum, into `$KGB_CACHE_DIR/mcp-artifacts/`
when `KGB_CACHE_DIR` is set (the pipeline's cache root, `shared/paths.py`), else
`~/.cache/kgbcia-mcp/artifacts/`.

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
have an `id` and a `kgbcia_source_records` list, except an edge the augmentation stage proposed
from the literature (`primary_knowledge_source: infores:kgbcia-augmentor`): it has no
transformation record, so it is accepted without one when it cites `publications` and names the
edges it refines in `original_edge_ids`, and `check` counts it on a line of its own. Plus the
per-source `transform_report.json` files under `data/transformation/`.

**Output:** `check` writes no graph files. `kgbcia-mcp hubs` writes `hubs.json`; the server
returns JSON-serializable, read-only query results. `deploy/prepare_dump.sh` produces the
Neo4j dump and artifact tarball (`manifest.json`, graph metadata, hubs and reports). The graph
metadata now includes precomputed counts for `get_graph_info`, `get_schema`, and argument
validation, so the running server does not scan the whole deployed graph for aggregate answers.
In `--artifacts-only` mode the script recomputes the artifact tarball from an existing dump and
copies that dump to the output unchanged.

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

The MCP interface is deployed against the real cleaned graph on the prepared AWS k3s host. The
loaded graph has **943,412 nodes** and **11,701,010 edges**. Neo4j runs read-only with
`NEO4J_AUTH=none`; the MCP HTTP deployment is disabled (`mcp.enabled=false`), so teammates run
`kgbcia-mcp` locally over stdio and connect to the shared Bolt endpoint. Network exposure is
controlled by the maintainer outside this repository.

Build-order steps 1–4 are complete: the standalone uv workspace package, offline graph check,
artifact loading, TransformReport lineage, bounded Neo4j service, FastMCP tools, optional
Cypher tool, dump preparation, precomputed statistics, indexes, Docker image, Helm chart,
local-k8s sample deployment, AWS sample deployment, and AWS real-graph deployment. Step 5,
publication to PyPI, remains open; until then teammates run from a checkout with
`uv run kgbcia-mcp serve --enable-cypher`.

A teammate can connect from a checkout by adding an MCP entry like this, with placeholders filled
from the current release handoff. Update `KGB_MCP_ARTIFACTS_SHA256` every time a new
`artifacts.tar.gz` is published.

```json
{
  "mcpServers": {
    "kgbcia-mcp": {
      "command": "uv",
      "args": ["run", "kgbcia-mcp", "serve", "--enable-cypher"],
      "env": {
        "NEO4J_URI": "bolt://<aws-bolt-host>:7687",
        "NEO4J_DATABASE": "neo4j",
        "KGB_MCP_ARTIFACTS": "https://<public-artifact-host>/artifacts.tar.gz",
        "KGB_MCP_ARTIFACTS_SHA256": "<64-hex-artifact-checksum>"
      }
    }
  }
}
```

Current verification:

- `get_graph_info()` returns 943,412 nodes and 11,701,010 edges and includes the `build` section
  from `description.json`.
- `get_schema()` returns a 116-row one-category meta-graph, avoiding the former whole-graph
  aggregate timeout.
- Precomputed flag counts are present for `unresolved_node` (1,914,123), `type_violation`
  (178,255), `self_loop` (2,435), and `rare_category_combination` (1,325).
- Flag formatting and `exclude_flags` were verified on `WIKIPATHWAYS:WP4580` -> `CHEBI:10034`
  (`biolink:has_participant`, edge `62c07ced-910e-5540-b2b6-3c4cdd94491f`):
  `get_edges_between(include_lineage=true)` returns `unresolved_node` and
  `rare_category_combination` as `{flag, reason}` pairs, and
  `get_node_edges(..., exclude_flags=["unresolved_node"])` filters those edges out.
- The issue #63 olodaterol (`CHEBI:82700`) -> ADRB2 (`UniProtKB:P07550`) `wrong_mechanism`
  example is **pending a future graph cleaned with #62 and #49**. In the current non-final graph,
  that DrugCentral edge cites PMID:20371707 but carries no `kgbcia_flags` /
  `kgbcia_flag_reasons` properties in the cleaned input, so the server correctly has no flag to
  report.

Known limits:

- The package is not yet on PyPI; use `uv run` from a checkout.
- `explain_edge(edge_id)` still unions over relationship types and can time out on the full
  graph for some edge IDs. Prefer `get_edges_between(include_lineage=true)` when endpoint IDs
  are known; it returns the same lineage for direct edges with an anchored query.
- AWS uses unauthenticated Bolt. Read-only graph settings block graph writes, but Neo4j
  Community does not provide a strong authorization boundary for the `system` database. Keep
  Bolt exposure restricted.
- `query_cypher` is read-only, row-capped and useful for ad hoc aggregates, but expensive raw
  queries can still hit the configured 30-second transaction timeout or 2 GiB transaction memory
  cap.

## Design decisions

- **Separate package:** `kgbcia-mcp` is a uv workspace member with no imports from `shared` or
  `agents`, so it can be published independently from the pipeline code.
- **Local stdio server, shared Neo4j:** each client runs the MCP server locally over stdio while
  all clients share one read-only Neo4j service. This avoids deploying and authenticating a
  public MCP HTTP endpoint for the demo.
- **Precomputed artifacts for aggregate answers:** `prepare_dump.sh` computes graph summary,
  schema, property examples and flag counts on a temporary build-time Neo4j and writes them to
  `graph_metadata.json`. Runtime tools and validation use those artifacts and never scan the
  full graph for `get_graph_info()` or `get_schema()`.
- **One category per node in the meta-graph:** nodes carry ancestor labels, so the meta-graph
  uses the rarest graph-wide category label per node to avoid cross-products over all ancestors.
- **Indexes baked into the dump:** the deployed database starts read-only, so the full-text node
  index and relationship-id indexes are created before the final `kgb.dump` is produced.
- **Checksum-keyed loads:** Helm init containers key their PVC marker on the dump checksum, not
  the URL, so changing presigned URLs does not reload an unchanged dump.
- **AWS auth disabled:** Neo4j Community allowed the authenticated sample user to administer
  `system`; the AWS deployment therefore uses `NEO4J_AUTH=none` plus maintainer-controlled
  network exposure, graph read-only mode and resource limits rather than a reusable plaintext
  Bolt password.
- **Build description is an artifact:** `description.json` is generated on the VM that has the
  Nov 9 transformation output and cleaning report, then sanitized and packed into the artifact
  tarball so user-facing tools can mention failed or missing sources.
