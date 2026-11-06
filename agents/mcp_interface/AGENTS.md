# AGENTS.md: agents/mcp_interface/

Read the root [`AGENTS.md`](../../AGENTS.md), [`agents/AGENTS.md`](../AGENTS.md) and
[`shared/AGENTS.md`](../../shared/AGENTS.md) first. Their rules (stage contract, schema
contract, ruff, `make test`/`make demo`, no data in git) apply here unchanged and win over
anything below. Don't add a `CLAUDE.md`. The repo requires Python ≥ 3.12.

## Goal

A read-only MCP server over a **Neo4j** copy of the final graph. Any LLM agent, including the
judges' agent, can use it to search nodes, inspect neighbors, find paths and, above all, answer
**"why does A connect to B?"**. Explainability is 20% of the score. Lineage comes from two
places:

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
the Helm chart that runs Neo4j, and the publishable server package.

## Delivery

The server ships two ways from one codebase:

- **Primary: a PyPI package, `kgbcia-mcp`, run over stdio** by any MCP client, Pi included:
  `uvx kgbcia-mcp serve`, with `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD` and
  `KGB_MCP_ARTIFACTS` set. This is the same pattern as the LitCoin MCP server. Each user runs
  the server locally, and it connects to the shared, read-only Neo4j over Bolt.
- **Optional: the same server over streamable HTTP**, run by the Helm chart next to Neo4j
  (`mcp.enabled`), for clients that can't run a local process.

Either way, Neo4j is the one shared service. In the PyPI model its Bolt port is reachable by
users, so the server-side protections in "Read-only enforcement" carry the whole load.

## Implementation approach

- **FastMCP**, with one `server.py` that builds the app and registers tool modules. Keep the
  layers separate: `services/` talks to Neo4j and returns plain Python data with no MCP imports,
  and `tools/` holds thin MCP wrappers that validate arguments and shape responses.
- **All configuration comes from environment variables**, with CLI flags overriding them. No
  hard-coded hosts or paths. The same package, image and config run locally, on k8s and on AWS.
- **Tools are written for an LLM reader.** Each docstring gives the purpose, an example CURIE,
  *Presentation Guidelines* (for example, "cite knowledge sources, publications and source
  records verbatim; never paraphrase provenance") and the tool to call next. Empty or failed
  lookups return a `hint` plus suggestions or valid values instead of raising.
- **Every query is bounded:** always anchor nodes with a label, always apply `LIMIT`, always
  paginate, and always pass values as Cypher parameters.

## Layout and packaging

The server is a **separate distribution** inside the repo, so it can be published to PyPI
without the rest of the pipeline. Make it a uv workspace member; the root project depends on it.

```
agents/mcp_interface/
├── cli.py                    # the pipeline stage: DESCRIPTION, main(argv) -> int; dispatches
│                             # check (repo-only) and delegates serve/hubs to kgbcia_mcp
├── check.py                  # offline pre-dump checks; may use shared.schema
├── pyproject.toml            # distribution "kgbcia-mcp"; script kgbcia-mcp = kgbcia_mcp.cli:main
├── src/kgbcia_mcp/
│   ├── cli.py                # serve, hubs
│   ├── config.py             # NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD, NEO4J_DATABASE,
│   │                         # KGB_MCP_ARTIFACTS, KGB_MCP_ARTIFACTS_SHA256,
│   │                         # KGB_MCP_ENABLE_CYPHER
│   ├── artifacts.py          # local dir, or an http(s) URL to the artifacts tarball,
│   │                         # downloaded once into a user cache dir and checksum-verified
│   ├── report_models.py      # vendored copy of the TransformReport models from shared/schema.py
│   ├── lineage.py            # index TransformReports; resolve kgbcia_source_records
│   ├── services/neo4j_service.py  # driver, read transactions, serialization; no MCP imports
│   ├── tools/graph_tools.py  # register_graph_tools(mcp, svc)
│   ├── tools/query_tools.py  # register_query_tools(mcp, svc); only when --enable-cypher
│   └── server.py             # create_server(), run(transport, host, port)
├── deploy/
│   ├── Dockerfile            # the kgbcia-mcp image (for the optional HTTP deployment)
│   ├── extra_indexes.cypher  # our indexes, baked into the kgb dump
│   ├── prepare_dump.sh       # ORION dump -> kgb dump + artifacts tarball (see below)
│   ├── make_dev_artifacts.py # synthetic reports + source records for the dev sample
│   ├── helm/kgb-mcp/         # the chart: Neo4j (+ optional kgbcia-mcp over HTTP)
│   ├── aws/                  # notes and scripts for the single-EC2 + k3s deployment
│   ├── RUNBOOK.md            # dump -> publish -> helm install / upgrade, for both environments
│   └── .env.example
└── README.md                 # keep "Run it alone", "Input / output", "Neo4j notes" and
                              # "Status" current; the PyPI page gets its own short README
tests/test_mcp_interface.py   # repo tests live in the root tests/ dir
```

- **`src/kgbcia_mcp` must never import `shared`, `agents` or anything else from the repo**, so
  that the published package installs and runs on its own. Its runtime dependencies are
  `fastmcp`, `neo4j` and `pydantic` only.
- `report_models.py` is a copy of `TransformReport`, `Mapping` and the models they use. A repo
  test compares its fields with `shared/schema.py`, so the two can't drift silently.
- Keep the `kgb-mcp` console script in the root project for the pipeline. `kgbcia-mcp` is the
  published entry point.
- Wiring in the root `pyproject.toml`: add `[tool.uv.workspace] members =
  ["agents/mcp_interface"]`, add `kgbcia-mcp` to the root dependencies with
  `[tool.uv.sources] kgbcia-mcp = { workspace = true }`, and exclude
  `agents/mcp_interface/src` and `agents/mcp_interface/pyproject.toml` from the root wheel
  (`[tool.hatch.build.targets.wheel] exclude`). The root wheel packages all of `agents/`, so
  without the exclusion it would also carry a stray copy of the server. Match the member
  package's `requires-python` (≥ 3.12) to the root project's.

**Side artifacts** are one tarball, published next to the dump (the same file server or bucket):
the `TransformReport` JSONs, ORION's graph metadata (graph ID, build and release version), and
the hub list for path search. `KGB_MCP_ARTIFACTS` is a local directory or an `http(s)` URL to the
tarball; URLs are downloaded with the standard library, cached per checksum, and loaded once at
startup. The URL must be **public and long-lived**, because every user's server fetches it with
no credentials. Use a GitHub Release asset on this repo (free, no AWS setup), or an S3 object
the AWS admin makes publicly readable. A presigned S3 URL is **not** suitable: it expires
within days. The tarball holds no secrets, so making it public is fine.

**Fixtures:** `.gitignore` blocks `*.jsonl` everywhere, so don't commit jsonl fixtures. Build a
toy graph (~10 nodes, ~15 edges, plus one `TransformReport`) in a pytest fixture from
`shared.schema` models, in the style of `tests/test_smoke.py::_edge`, and write it to `tmp_path`.

**Development sample (local only, never committed):** the maintainer provides a small, complete
ORION-built graph at `data/mcp_interface/sample/`: LitCoin's graph from 200 PubMed abstracts,
with `nodes.jsonl`, `edges.jsonl` and ORION's Neo4j dump. Use it whole. Run
`prepare_dump.sh` on it too, because ORION's dump lacks our extra indexes and hub list. It has no
`TransformReport`s and no `kgbcia_source_records`, so write `deploy/make_dev_artifacts.py` to:
- synthesize one `TransformReport` per `primary_knowledge_source` found in the sample, with one
  `Mapping` per `(predicate, subject category, object category)` combination;
- write a copy of the edges that adds `kgbcia_source_records` pointing at those mappings
  (including some edges with several entries), and an `id` wherever one is missing.

Mark everything it produces as synthetic (`source_version: "synthetic"`), and keep it out of the
real pipeline's output directories. Use the sample for `check`, `prepare_dump.sh`, the chart and
the integration tests. Unit tests in CI still use the in-code toy fixture, because CI has no
data.

## CLI and the pipeline contract

`shared/pipeline.py` calls `main([])` and expects it to return; it must not block and must not
call `sys.exit`.

- `kgb-mcp check --input <dir>` is the default when no subcommand is given. The cleaning stage
  isn't implemented yet, so the default `--input` is the first of these that exists: the
  cleaning stage's output, then `data/transformation/merged/`. Reports are collected from
  `data/transformation/**/transform_report.json`. The check works offline and returns 0 only if
  the graph is ready for `orion-neo4j-dump`:
  - **every edge has an `id`**;
  - every `kgbcia_source_records` value is a list whose entries parse;
  - every referenced `Mapping.id` exists in some report.

  It prints counts and the exact commands to run next. If no input exists, it prints a clear
  message and returns nonzero.
- `kgbcia-mcp serve [--transport stdio|http] [--host 127.0.0.1] [--port 8000]
  [--enable-cypher] [--artifacts <dir|url>]`; `kgb-mcp serve` delegates to it.
- `kgbcia-mcp hubs --top <n> --output <dir>` computes the hub list (the highest-degree nodes)
  against a running Neo4j. `prepare_dump.sh` runs it.

Every edge needs an `id` because edge IDs are what `explain_edge` takes and what agents cite, and
the relationship index in "Preparing the dump" is keyed on them.

## Environments

Develop and test on the local k8s cluster first, which saves the limited AWS credit
(**$500 total**). Deploy to AWS only for the final judged endpoint, once the team has signed off.

```
transformation (later: cleaning) -> nodes.jsonl + edges.jsonl + transform_report.json files
  -> orion-neo4j-dump        -> ORION dump (Neo4j 5, ORION's indexes)
  -> deploy/prepare_dump.sh  -> kgb dump (+ our indexes) + artifacts tarball
  -> HTTP file server (k8s) / S3 (AWS)
  -> helm install: download -> neo4j-admin load -> Neo4j 5 Community (read-only, auth on)
  <- uvx kgbcia-mcp serve (stdio), on each user's machine, over Bolt
  <- optional: kgbcia-mcp over HTTP in the chart
```

**The chart** (`deploy/helm/kgb-mcp/`) is small and specific to this project, not a generic Neo4j
chart:
- **Neo4j StatefulSet** (official `neo4j:5.26.<patch>-community` image: Neo4j 5's LTS release,
  pinned to an exact patch in values; see "Preparing the dump" for why it's 5.26) with a persistent volume, plus two init containers:
  1. **Download** `neo4j.dumpUrl` and verify `neo4j.dumpSha256`.
  2. **Load** it with `neo4j-admin database load --overwrite-destination`.

  Both are **idempotent**: skip them when a marker file on the volume records the same
  **checksum**, so a pod restart doesn't re-download or reload. Key the marker on the checksum,
  never the URL, because on AWS the URL is a presigned S3 link that differs on every
  `helm upgrade` but points to the same file. A new checksum in values triggers a fresh load.
  The dump stays private: for AWS, the runbook generates a short-lived presigned URL for it at
  install time, which is enough because it's only fetched once.
- **Neo4j settings** come from values as `NEO4J_*` environment variables: heap, page cache,
  `server.databases.default_to_read_only=true`, `db.transaction.timeout`,
  `db.memory.transaction.total.max`, and bolt thread limits. The admin password comes from a
  Secret.
- **Exposure:** `neo4j.service.type` defaults to `ClusterIP`. Environments that serve PyPI users
  set it to `LoadBalancer` or `NodePort` for **Bolt (7687)**, and optionally HTTP (7474) for
  Neo4j Browser. There is **no TLS** in the initial version: Bolt and HTTP are plain, with
  password auth.
- **Optional `kgbcia-mcp` Deployment** (`mcp.enabled`, default `false`) from `deploy/Dockerfile`,
  with `NEO4J_URI` pointing at the Neo4j Service, `KGB_MCP_ARTIFACTS` as the tarball URL,
  probes on `/health`, and `mcp.enableCypher`. Add a Service and an optional Ingress for it.
- `values.yaml` holds safe defaults, with environment files `values-dev.yaml` and
  `values-aws.yaml`. Keep secrets out of all of them.
- **Resources are values,** not constants. The merged graph is large (on the order of 30M edges
  before deduplication), so the heap, page cache and volume size come from the values file of
  each environment.
- Validate it with `helm lint` and `helm template`. These need no cluster, so they can run in CI.

**Final (AWS):** the **same chart** on a single EC2 instance running k3s. The steps are
in `deploy/aws/` and `RUNBOOK.md`:
- the instance (memory-optimized), a gp3 data volume and a security group opening only Bolt
  and, if used, Neo4j HTTP and MCP HTTP;
- installing k3s and Helm;
- fetching the dump and tarball from S3;
- `helm install -f values-aws.yaml`.

The maintainer runs these steps by hand. Write shell scripts and docs, not a CDK app, and
reference what the AWS admin already created (the S3 bucket, the VPC). Include **stopping the
instance** between test sessions and **deleting everything** after judging, since compute is
billed only while the instance runs.

**Both environments:** the same chart, kgb dump, artifacts and package version, with only the
values file differing. Any other difference is a bug. Never commit hosts, passwords, account IDs
or ARNs. `.env` is gitignored; ship an `.env.example`.

## Preparing the dump (ORION, outside this package)

`orion-neo4j-dump` needs a local Neo4j installation, so run it from **ORION's Docker image**,
which includes Neo4j 5.25.1 (check the pinned image), even though the repo now uses Python 3.12
like ORION. The server
package never imports ORION. Pin the image or the commit, and record which one in the runbook.

The deployed database is read-only from its first start, so our extra indexes are **baked into
the dump** rather than created after loading. `deploy/prepare_dump.sh` runs once per graph
release, on a workstation with Docker:

1. Run `orion-neo4j-dump nodes.jsonl edges.jsonl <out>` in ORION's image.
2. Load the ORION dump into a temporary, writable `neo4j` container of **the same pinned 5.26
   version as the chart**. A dump loads into the same or a newer Neo4j, never an older one.
   5.26 is the newest 5.x (the LTS release), so it accepts dumps from ORION's 5.25.1 image and
   the LitCoin sample (built with 5.20, already shown to load into 5.26). If ORION's image ever
   moves past the chart's version, bump the chart.
3. Run `extra_indexes.cypher` (below), then `CALL db.awaitIndexes()`.
4. Run `kgbcia-mcp hubs` against the container, writing the hub list into the artifacts
   directory.
5. Stop the container and run `neo4j-admin database dump`: this is the **kgb dump**.
6. Pack the artifacts (`TransformReport`s, ORION graph metadata, hub list) into a tarball.
7. Print the SHA-256 of the dump and the tarball, for the chart values and
   `KGB_MCP_ARTIFACTS_SHA256`.

Publishing the files (to the HTTP file server for k8s, to S3 for AWS) and updating the values are
manual steps; document them in `RUNBOOK.md`.

ORION's dump already contains a range index on `biolink:NamedThing.name`, and a uniqueness
constraint on `id` for every node label. `extra_indexes.cypher` adds what the server needs:

- **A full-text node index** for `search_nodes`, over `name`, `synonym` and
  `equivalent_identifiers` on ``:`biolink:NamedThing` ``. Confirm that Neo4j 5.26 full-text
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

Exact lineage rests on two upstream pieces, owned by other teams; don't implement them here.

1. **`kgbcia_source_records`** is a top-level, always-a-list property on every edge, documented
   in `agents/AGENTS.md` and already emitted by the transformation agent (`orion_runtime.py`).
   Each entry is `"{Mapping.id}#{source record key}"`. Each source workspace under
   `data/transformation/` has a `transform_report.json`.
2. **Edge IDs from ORION's merge.** `merge_graphs.py` (in the transformation skill) must build
   its `GraphSpec` with `add_edge_id=True`, which gives every edge a stable, deterministic,
   citable `id`. **It doesn't yet**; that's an open upstream fix. Until it lands, `kgb-mcp check`
   fails on the merged graph, which is the intended signal. Leave `overwrite_edge_ids` at its
   default.

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

Neo4j **Community** has no role-based access control, and in the PyPI model users connect to
Bolt directly, so protection has to live in Neo4j itself:

1. **Server-level:** `server.databases.default_to_read_only=true`, set by the chart from the
   first start (which is why the indexes are baked into the dump). With this set, Neo4j itself
   rejects writes from any client.
2. **Resource limits:** `db.transaction.timeout`, `db.memory.transaction.total.max` and the
   bolt thread pool size in the chart values. With Bolt reachable from outside, these are what
   protect the shared instance against expensive queries.
3. **In our server:** only read transactions (`execute_read`), plus an `EXPLAIN` check, a
   per-query timeout and a row cap on `query_cypher`. This protects users of our server; it
   can't stop someone using a raw Neo4j driver.

**Accounts:** give users a separate account (for example `kgbcia`) and keep the admin password
private. Community has no roles, so **confirm on k8s** whether that account can change
passwords or create and drop users while the graph stays read-only. If it can, document the risk
in the runbook and ask the maintainer whether to accept it or switch to `NEO4J_AUTH=none` with
read-only plus limits (the pattern ROBOKOP's public instances use). Also confirm that a `CREATE`
fails through a raw driver session. Plain Bolt sends the password unencrypted; note that in the
runbook too.

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

`query_cypher` is behind `--enable-cypher` (or `KGB_MCP_ENABLE_CYPHER=1`). The configuration we
hand to judges, the `.pi/mcp.json` entry and the README's client setup **turn it on**: judges'
questions will include aggregates such as "how many Tclin targets are linked to X" that no fixed
tool covers. Users with Bolt access can run raw Cypher anyway, so the tool adds convenience, not
exposure.

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

1. **Core.** The workspace package skeleton, `check`, `artifacts`, `report_models`, `lineage`
   and `neo4j_service`, with unit tests (no database).
2. **Tools.** Implement them and test them through `fastmcp.Client` with the service mocked.
   Make sure `uv build` in `agents/mcp_interface/` produces a wheel that installs and starts in
   a clean virtual environment.
3. **Dump and chart.** Write `extra_indexes.cypher`, `prepare_dump.sh`, the Dockerfile, the Helm
   chart and `RUNBOOK.md`. Make a kgb dump from the development sample (after
   `make_dev_artifacts.py`), settle the "confirm" items, and `helm install` on the local k8s cluster. Run the
   integration tests, then hand it to the team: `uvx --from <wheel or path> kgbcia-mcp serve`
   against the k8s Neo4j.
4. **Publish and AWS.** Only after the team signs off: the maintainer publishes to PyPI, then
   follows `deploy/aws/` with the same dump, artifacts and chart. Then point a real agent at it
   with a few "why does A connect to B?" questions.

## Testing

- CI has no network and no database. Integration tests use a module-level
  `requires_neo4j = pytest.mark.skipif(not os.getenv("NEO4J_URI"), ...)`, which needs no custom
  marker in root `pyproject.toml`. Never call NodeNorm.
- Always-run tests:
  - `check` on the built fixture, including a graph with one edge missing its `id`, which must
    fail;
  - lineage resolution: one record, a merged edge with many, an unknown `Mapping.id`, and `#`
    inside the record key;
  - `report_models` matching `shared/schema.py`;
  - `artifacts` from a directory and from a URL (with the download mocked), plus checksum
    mismatch and cache reuse;
  - a check that nothing under `src/kgbcia_mcp` imports `shared` or `agents`;
  - `main([])` returning, and result serialization;
  - `helm lint` and `helm template` on the chart with each values file, if `helm` is on the
    path (skip otherwise);
  - every tool through an in-memory `fastmcp.Client(mcp)` with `neo4j_service` mocked.
- Integration tests are a short smoke suite against whatever graph `NEO4J_URI` points at (the
  k8s instance). They discover test IDs at runtime, for example from `get_graph_info()` or the
  first `search_nodes` hit, and never hard-code them. Check that each tool returns sensible
  results, that a write is rejected both through `query_cypher` and through a raw driver
  session, and that returned edges carry provenance. Don't test ORION's dump; that's covered
  upstream.

## Dependencies and registration

- The package's dependencies are `fastmcp` (3.x, pinned to an exact release at least two weeks
  old), `neo4j` (5.x) and `pydantic`. Add nothing else without asking. Wire the workspace member
  into the root `pyproject.toml` and `uv.lock`.
- Keep changes inside `agents/mcp_interface/` and `tests/test_mcp_interface.py`, plus
  `pyproject.toml` / `uv.lock` and `.pi/mcp.json`.
- **Register the server in `.pi/mcp.json`** under `mcpServers` as `kgbcia-mcp`, a stdio entry.
  Until the package is on PyPI, run `uv run kgbcia-mcp serve`; after that, run
  `uvx kgbcia-mcp serve`. Neo4j settings come from the environment; put no hosts or passwords in
  the file.

## Out of scope

Producing the ORION dump itself (ORION does it), a generic reusable Neo4j chart, TLS and domain
names, publishing to PyPI and running the AWS steps (the maintainer does both), a skill for
consuming agents, any tool that adds, changes or deletes graph data, HTTP auth on the MCP
endpoint, NodeNorm calls at query time, putting TransformReport content into Neo4j, and opening
PRs.
