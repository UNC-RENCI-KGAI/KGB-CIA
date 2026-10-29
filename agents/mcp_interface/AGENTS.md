# AGENTS.md: agents/mcp_interface/

Read the root [`AGENTS.md`](../../AGENTS.md), [`agents/AGENTS.md`](../AGENTS.md) and
[`shared/AGENTS.md`](../../shared/AGENTS.md) first. Their rules (stage contract, schema
contract, ruff, `make test`/`make demo`, no data in git) apply here unchanged and win over
anything below. Don't add a `CLAUDE.md`.

## Goal

A read-only MCP server over a **Neo4j** copy of the final graph, the cleaning stage's output.
Any LLM agent, including the judges' agent, can use it to search nodes, inspect neighbors, find
paths and, above all, answer **"why does A connect to B?"**. Explainability is 20% of the score.
Lineage comes from two places:

1. **On the edge, in Biolink terms** (`shared/schema.Edge`): `primary_knowledge_source`,
   `aggregator_knowledge_source`, `knowledge_level`, `agent_type`, `publications`,
   `original_subject`, `original_object`, `original_predicate`, plus extra slots such as
   qualifiers, `affinity` and `p_value`.
2. **Off the graph, in each source's `TransformReport`** (`shared/schema.TransformReport`),
   reached through the edge's `kgbcia_source_records`: which Croissant file, record set and
   columns produced the edge, and from which source record. Report content never goes into
   Neo4j.

This stage does **not** build the database. ORION's `orion-neo4j-dump` does that (see
"Preparing the dump"). This stage owns the server, the lineage resolution, the extra indexes,
and the Helm chart that runs Neo4j plus the server.

## Implementation approach

- **FastMCP**, with one `server.py` that builds the app and registers tool modules. Keep the
  layers separate: `services/` talks to Neo4j and returns plain Python data with no MCP imports,
  and `tools/` holds thin MCP wrappers that validate arguments and shape responses.
- **All configuration comes from environment variables**, with CLI flags overriding them. No
  hard-coded hosts or paths. The same image and config run on k8s and on AWS.
- **Tools are written for an LLM reader.** Each docstring gives the purpose, an example CURIE,
  *Presentation Guidelines* (for example, "cite knowledge sources, publications and source
  records verbatim; never paraphrase provenance") and the tool to call next. Empty or failed
  lookups return a `hint` plus suggestions or valid values instead of raising.
- **Every query is bounded:** always anchor nodes with a label, always apply `LIMIT`, always
  paginate, and always pass values as Cypher parameters.

## Environments

One **Helm chart** (`deploy/helm/kgb-mcp/`) runs both Neo4j and the MCP server, in every
environment. Develop and test on the local k8s cluster first. Deploy to AWS only for the final
judged endpoint, once the team has signed off.

```
cleaning stage -> nodes.jsonl + edges.jsonl + TransformReport JSONs
  -> orion-neo4j-dump        -> ORION dump (Neo4j 5, ORION's indexes)
  -> deploy/prepare_dump.sh  -> kgb dump (+ our indexes) + artifacts tarball
  -> HTTP file server (k8s) / S3 (AWS)
  -> helm install: download -> neo4j-admin load -> Neo4j 5 Community (read-only)
                   kgb-mcp Deployment -> MCP over streamable HTTP
       <- team (k8s Ingress), then the judges' agent (AWS)
     kgb-mcp serve --transport stdio, port-forwarded to the k8s Neo4j <- local dev
```

**The chart** is small and specific to this project, not a generic Neo4j chart:
- **Neo4j StatefulSet** (official `neo4j:5.25.x-community` image, the same version as ORION's
  image) with a persistent volume, plus two init containers:
  1. **Download** `neo4j.dumpUrl` and verify `neo4j.dumpSha256`.
  2. **Load** it with `neo4j-admin database load --overwrite-destination`.

  Both are **idempotent**: skip them when a marker file on the volume records the same URL and
  checksum, so a pod restart doesn't re-download or reload. Changing the URL in values
  triggers a fresh load.
- **Neo4j settings** come from values as `NEO4J_*` environment variables: heap, page cache,
  `server.databases.default_to_read_only=true`, `db.transaction.timeout` and
  `db.memory.transaction.total.max`. The password comes from a Secret; there's no `none` auth.
  The Neo4j Service is `ClusterIP` only: Bolt and HTTP are never exposed outside the cluster.
- **kgb-mcp Deployment** (image from `deploy/Dockerfile`) with an init container that downloads
  and unpacks `mcp.artifactsUrl` into an `emptyDir`, plus `NEO4J_URI` pointing at the Neo4j
  Service, readiness and liveness probes on `/health`, and `mcp.enableCypher`.
- **Service + optional Ingress** for the MCP endpoint only.
- `values.yaml` holds safe defaults. Keep environment-specific values files
  (`values-dev.yaml`, `values-aws.yaml`) free of secrets.
- Validate it with `helm lint` and `helm template`. These need no cluster, so they can run in CI.

**Final (AWS):** run **the same chart** on a single EC2 instance running k3s, or on EKS if the
AWS account admin already has a cluster. That gives exact parity with dev. The CDK app
(`deploy/aws/`, Python) creates only what's around the chart: the instance, volume, security
groups and HTTPS (ALB or Ingress TLS). It *references* resources the admin already created (the
S3 bucket, the VPC) instead of recreating them. Ask which resources exist, and whether to use
EKS, before writing it. Don't start until the k8s deployment has been signed off.

**Both environments:**
- **Parity:** the same chart, kgb dump, artifacts and image. Only the values file differs. Any
  other difference between the environments is a bug.
- HTTP runs FastMCP's streamable HTTP in **stateless** mode with a `/health` route.
- Never commit hosts, passwords, account IDs or ARNs. `.env` is gitignored; ship an
  `.env.example`.

## Layout

```
agents/mcp_interface/
├── cli.py                    # argparse + dispatch only; keeps DESCRIPTION and main(argv) -> int
├── config.py                 # NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD, NEO4J_DATABASE,
│                             # KGB_MCP_ARTIFACTS
├── artifacts.py              # load the graph's side artifacts (see below) from a local dir
├── check.py                  # offline pre-dump checks on the final graph (see CLI)
├── lineage.py                # index TransformReports; resolve kgbcia_source_records
├── services/neo4j_service.py # driver, read transactions, result serialization; no MCP imports
├── tools/graph_tools.py      # register_graph_tools(mcp, svc)
├── tools/query_tools.py      # register_query_tools(mcp, svc); only when --enable-cypher
├── server.py                 # create_server(), run(transport, host, port)
├── deploy/
│   ├── Dockerfile            # the kgb-mcp image
│   ├── extra_indexes.cypher  # our indexes, baked into the kgb dump
│   ├── prepare_dump.sh       # ORION dump -> kgb dump + artifacts tarball (see below)
│   ├── helm/kgb-mcp/         # the chart: Neo4j + kgb-mcp
│   ├── aws/                  # CDK app around the chart + final-deployment runbook
│   ├── RUNBOOK.md            # dump -> publish -> helm install / upgrade, for both environments
│   └── .env.example
└── README.md                 # keep "Run it alone", "Input / output" and "Status" current
tests/test_mcp_interface.py   # repo tests live in the root tests/ dir
```

**Side artifacts** are one tarball, published next to the dump. The chart unpacks it into a
local directory (`KGB_MCP_ARTIFACTS`, or `--artifacts`), which the server loads once at startup:
- the `TransformReport` JSONs;
- ORION's graph metadata (graph ID, build and release version), for `get_graph_info()`;
- the hub list for path search (see "Querying Neo4j").

**Fixtures:** `.gitignore` blocks `*.jsonl` everywhere, so don't commit jsonl fixtures. Build a
toy graph (~10 nodes, ~15 edges, plus one `TransformReport`) in a pytest fixture from
`shared.schema` models, in the style of `tests/test_smoke.py::_edge`, and write it to `tmp_path`.

## CLI and the pipeline contract

`shared/pipeline.py` calls `main([])` and expects it to return; it must not block and must not
call `sys.exit`.

- `kgb-mcp check --input <dir>` is the default when no subcommand is given. The default
  `--input` is the cleaning stage's output location, as documented in `data/README.md`. It works
  offline, reads `nodes.jsonl`, `edges.jsonl` and the reports, and returns 0 only if the graph is
  ready for `orion-neo4j-dump`:
  - **every edge has an `id`**;
  - every `kgbcia_source_records` value is a list whose entries parse;
  - every referenced `Mapping.id` exists in some report.

  It prints counts and the exact commands to run next. If the default input is missing, print a
  clear message and return nonzero.
- `kgb-mcp serve [--transport stdio|http] [--host 127.0.0.1] [--port 8000] [--enable-cypher]
  [--artifacts <dir>]`.
- `kgb-mcp hubs --top <n> --output <dir>` connects to Neo4j, computes the hub list (the
  highest-degree nodes) and writes it into the artifacts directory. `prepare_dump.sh` runs it.

Every edge needs an `id` because edge IDs are what `explain_edge` takes and what agents cite, and
the relationship index in "Preparing the dump" is keyed on them. Augmentation and cleaning must give every edge
they add or rewrite an `id`.

## Preparing the dump (ORION, outside this package)

`robokop-orion` requires Python ≥ 3.12 and the repo targets 3.11, so ORION is **not** a
dependency of this package. `orion-neo4j-dump` also needs a local Neo4j installation, so run it
from **ORION's Docker image**, which includes Neo4j 5.25.1. Pin the image or the commit, and
record which one in the runbook.

The deployed database is read-only from its first start, so our extra indexes are **baked into
the dump** rather than created after loading. `deploy/prepare_dump.sh` runs once per graph
release, on a workstation with Docker:

1. Run `orion-neo4j-dump nodes.jsonl edges.jsonl <out>` in ORION's image.
2. Load the ORION dump into a temporary, writable `neo4j:5.25.x-community` container.
3. Run `extra_indexes.cypher` (below), then `CALL db.awaitIndexes()`.
4. Run `kgb-mcp hubs` against the container, writing the hub list into the artifacts directory.
5. Stop the container and run `neo4j-admin database dump`: this is the **kgb dump**.
6. Pack the artifacts (`TransformReport`s, ORION graph metadata, hub list) into a tarball.
7. Print the SHA-256 of the dump and the tarball, for the chart values.

Publishing the files (to the HTTP file server for k8s, to S3 for AWS) and updating the values are
manual steps; document them in `RUNBOOK.md`.

ORION's dump already contains a range index on `biolink:NamedThing.name`, and a uniqueness
constraint on `id` for every node label. `extra_indexes.cypher` adds what the server needs:

- **A full-text node index** for `search_nodes`, over `name`, `synonym` and
  `equivalent_identifiers` on ``:`biolink:NamedThing` ``. Confirm that Neo4j 5.25 full-text
  indexes accept list properties. If they don't, index `name` and `synonym` only, and resolve
  equivalent IDs with `ANY(x IN n.equivalent_identifiers WHERE x = $id)`, restricted to a
  label.
- **A relationship property index on `id`**, so `explain_edge(edge_id)` doesn't scan every
  relationship. Neo4j 5 range indexes on relationships are per type, so generate one statement
  per relationship type (`db.relationshipTypes()`).

Keep the script idempotent (`IF NOT EXISTS`). Record the graph version, the dump checksum and
the load date in `data/README.md`.

## Data format in Neo4j (as written by ORION)

- **Lists are native Neo4j lists.** ORION writes them as `string[]` columns, and `neo4j-admin`
  imports them as lists, so `publications`, `equivalent_identifiers` and `kgbcia_source_records`
  come back as Python lists.
- **Categories are labels only.** A node has no `category` property. Read categories with
  `labels(n)`, and anchor every lookup with ``:`biolink:NamedThing` ``, for example
  ``MATCH (n:`biolink:NamedThing` {id: $id})``, which uses ORION's constraint index.
- **Relationship types are Biolink predicates** (``-[r:`biolink:treats`]->``), and the edge `id`
  is the property `r.id`.
- **Confirm all of this on the first `prepare_dump.sh` run before building on it.** Check with `SHOW INDEXES`,
  `db.schema.nodeTypeProperties()` and `db.schema.relTypeProperties()`. Write the results into
  `agents/mcp_interface/README.md` under "Neo4j notes", and encode them as constants in
  `neo4j_service` so the code has one source of truth.

## Lineage from TransformReports

Exact lineage rests on two upstream decisions, already agreed and owned by other teams; don't
implement them here.

1. **Transformation** puts a top-level, always-a-list property `kgbcia_source_records` on every
   edge it emits. Each entry is `"{Mapping.id}#{source record key}"`, using the source's primary
   key where there is one and the row number only as a fallback. It also writes a
   `TransformReport` whose `Mapping.id`s are unique across all reports (source-prefixed).
2. **The ORION graph spec** sets `add_edge_id: true`, which gives every edge a stable,
   deterministic, citable `id`. Leave `overwrite_edge_ids` at its default (`true`); the MCP
   server doesn't use the pre-merge ID mapping file. Every edge must still have an `id` when the
   final graph is dumped (see "CLI and the pipeline contract").

When ORION merges edges, lists concatenate and deduplicate (`orion/merging.py`), so a merged edge
carries every contributing record. Two conditions keep that true:

- The property is a **top-level** key on the edge. ORION's merge doesn't concatenate lists
  nested inside dicts; it keeps the first value and silently drops the others.
- The value is **always a list**, even for a single entry. When both values are scalars, ORION
  keeps only one.

Filter, augmentation and cleaning must preserve the property, and any Biolink-strict step must
not strip it. Augmentation gives the edges it adds their own entries and reports.

`lineage.py` indexes the `TransformReport`s' `Mapping`s by `id` at startup. For an edge, it
reads `kgbcia_source_records` (a native list in Neo4j) and splits each entry on the **first**
`#` into `Mapping.id` and the record key. It returns, for each entry: the
mapping's `file`, `record_set`, `columns` and `constants`; the report's `source`,
`source_version` and `croissant`; and the record key. Group the entries by mapping, and report
counts when there are many.

**Fallback,** for an edge without the property or with an unknown `Mapping.id`: match on
`report.source` ∈ {pks} ∪ aggregators, plus predicate and categories, and label every result
`match: "candidate"`. If no reports are configured, return the Biolink provenance and say that
file and column lineage is unavailable.

## Querying Neo4j

- Use the official `neo4j` driver. Every call goes through `session.execute_read(...)` with
  `neo4j.Query(text, timeout=...)`.
- Convert results to plain dicts in one serializer: nodes, relationships, paths, and the Neo4j
  temporal and spatial types.
- **`find_paths` and `find_connected_nodes`:** search hop by hop (1, then 2, up to `max_hops`)
  and stop at the first depth that finds results. Exclude hub nodes as intermediates (using the
  hub list from the artifacts). Apply `LIMIT` in Cypher, and return `truncated` rather than
  timing out silently. For `find_paths`, also try ``allShortestPaths((a)-[*..N]-(b))`` with a
  hub filter, and keep whichever performs better on the real graph.
- **Search:** `db.index.fulltext.queryNodes` for names and synonyms. Exact CURIEs go through the
  `id` constraint index.
- Cache the schema and summary stats at startup.

## Read-only enforcement

Neo4j **Community** has no role-based access control, so read-only access comes from three
layers, all required:

1. **Server-level:** `server.databases.default_to_read_only=true`, set by the chart from the
   first start (which is why the indexes are baked into the dump). With this set, Neo4j itself
   rejects writes.
2. **Driver-level:** only read transactions (`execute_read`). For `query_cypher`, also run
   `EXPLAIN` first and reject unless the query type is read-only.
3. **Resource limits:** `db.transaction.timeout` and `db.memory.transaction.total.max` in the
   chart values, plus the per-query timeout and row cap in the server. On a public endpoint,
   these are what protect against expensive queries.

Confirm on k8s that a `CREATE` through `query_cypher` fails at layer 1 even when layer 2 is
bypassed.

## Tools

All tools are read-only and return JSON-serializable dicts. Where results can be large, tools
take `limit` (default 50, hard cap 500) and `offset`, and every list response includes `total`
and `truncated` (see "Implementation approach" for docstrings and hints).

**Fewer round trips:**
- Any `*_id` argument accepts a CURIE, an equivalent identifier or an exact name. It resolves
  through the node lookup and full-text index, and an ambiguous name returns the candidates
  instead of guessing.
- Every node in a response includes `id`, `name` and `categories`.
- Every edge in a response includes `edge_id`, `predicate`, `primary_knowledge_source`, the
  publication count and any qualifiers. The agent never needs a second call just to label a
  result or to know which `edge_id` to explain.

| Tool | Purpose |
|---|---|
| `get_graph_info()` | counts, categories, predicates, knowledge sources, ORION build and release version |
| `get_schema()` | meta-graph `(subject_category, predicate, object_category, count)`, plus property names per category with example values (for example the Pharos target development level) |
| `search_nodes(query, category?, limit)` | by CURIE, equivalent identifier, name or synonym |
| `get_node(node_id)` | full node record |
| `summarize_node_edges(node_id)` | counts of edges by `(direction, predicate, neighbor_category)`: the cheap first look at an unfamiliar node, especially a hub |
| `get_node_edges(node_id, direction="both", predicate?, neighbor_category?, neighbor_where?, limit, offset)` | neighbors with edge summaries; `neighbor_where` is `{property: value}` equality on neighbor properties |
| `get_edges_between(source_id, target_id, include_lineage=false)` | every direct edge between the two nodes, both directions; with `include_lineage`, each edge carries its `explain_edge` result: the one-call answer to "why does A connect to B?" |
| `find_paths(source_id, target_id, max_hops=3, predicates?, limit=10)` | bounded paths between two known nodes, shortest first; reject `max_hops > 4` |
| `find_connected_nodes(source_id, target_category, max_hops=2, predicates?, target_where?, limit)` | nodes of a category reachable from a node, with the connecting path, for example "drugs reaching disease X via a target"; reject `max_hops > 3` |
| `explain_edge(edge_id)` | the "why": Biolink provenance plus each source record (`kgbcia_source_records` → Mapping: file, record set, columns, constants; report: source, version, Croissant) |
| `query_cypher(cypher, parameters?, limit=100)` | raw Cypher for counts, rankings and patterns the other tools can't express; read-only (see "Read-only enforcement"), row-capped, with a timeout. The docstring states the labels-as-categories convention and the property names, and gives two example queries |

`query_cypher` is behind `--enable-cypher`, which is **on for the hosted endpoint** the judges
use (and for team testing on k8s): their questions will include aggregates such as "how many Tclin targets are linked to X"
that no fixed tool covers. It's off by default locally.

Validate every predicate, category and property name against the cached schema before putting
it into a query.

**Server instructions:** set FastMCP's `instructions` to a short workflow guide that clients
show to the model:
1. resolve names with `search_nodes`;
2. orient with `get_schema` or `summarize_node_edges`;
3. explore with `get_node_edges`, `find_paths` or `find_connected_nodes`;
4. justify every claimed connection with `get_edges_between(include_lineage=true)` or
   `explain_edge`, citing sources verbatim;
5. fall back to `query_cypher` only for aggregates.

## Build order

The real graph depends on earlier stages, so build against the toy fixture first:

1. **Core.** `check`, `artifacts`, `lineage` and `neo4j_service`, with unit tests (no database).
2. **Tools.** Implement them and test them through `fastmcp.Client` with the service mocked.
3. **Dump and chart.** Write `extra_indexes.cypher`, `prepare_dump.sh`, the Dockerfile, the
   Helm chart and `RUNBOOK.md`. Make a kgb dump (from a real ORION dump, or one built from the
   toy fixture), settle the "confirm" items, and `helm install` on the local k8s cluster. Run
   the integration tests, then hand the endpoint to the team for testing.
4. **AWS.** Only after the team signs off: the CDK app around the same chart, and a deployment
   with the same dump, artifacts and image. Then point a real agent at it with a few "why does A
   connect to B?" questions.

## Testing

- CI has no network and no database. Integration tests use a module-level
  `requires_neo4j = pytest.mark.skipif(not os.getenv("NEO4J_URI"), ...)`, which needs no custom
  marker in root `pyproject.toml`. Never call NodeNorm.
- Always-run tests:
  - `check` on the built fixture, including a graph with one edge missing its `id`, which must
    fail;
  - lineage resolution: one record, a merged edge with many, an unknown `Mapping.id`, and `#`
    inside the record key;
  - `main([])` returning;
  - result serialization;
  - `helm lint` and `helm template` on the chart with each values file, if `helm` is on the
    path (skip otherwise);
  - every tool through an in-memory `fastmcp.Client(mcp)` with `neo4j_service` mocked.
- Integration tests are a short smoke suite against whatever graph `NEO4J_URI` points at (the
  k8s instance). They discover test IDs at runtime, for example from `get_graph_info()` or the
  first `search_nodes` hit, and never hard-code them. Check that each tool returns sensible
  results, that a write via `query_cypher` is rejected, and that returned edges carry
  provenance. Don't test ORION's dump; that's covered upstream.

## Dependencies and registration

- Use `uv add fastmcp neo4j`, with fastmcp 3.x pinned to an exact release at least two weeks
  old and the `neo4j` driver at 5.x. Add nothing else without asking; downloading the dump and
  artifacts is the chart's job, so the server needs no AWS SDK.
- Keep changes inside `agents/mcp_interface/` and `tests/test_mcp_interface.py`, plus
  `pyproject.toml` / `uv.lock` (dependencies) and `.pi/mcp.json`.
- **Register the server in `.pi/mcp.json`** under `mcpServers` as `kgb-mcp`, a stdio entry that
  runs `uv run kgb-mcp serve`. Neo4j settings come from the environment; put no hosts or
  passwords in the file. Add an entry for the k8s HTTP endpoint, and later the AWS one, in the
  format Pi's MCP adapter expects.

## Out of scope

Producing the ORION dump itself (ORION does it), a generic reusable Neo4j chart, a skill for consuming agents, any tool that adds,
changes or deletes graph data, HTTP auth, NodeNorm calls at query time, putting TransformReport
content into Neo4j, and opening PRs (the maintainer does that).
