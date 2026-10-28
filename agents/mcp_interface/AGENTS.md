# AGENTS.md: agents/mcp_interface/

Read the root [`AGENTS.md`](../../AGENTS.md), [`agents/AGENTS.md`](../AGENTS.md) and
[`shared/AGENTS.md`](../../shared/AGENTS.md) first. Their rules (stage contract, schema
contract, ruff, `make test`/`make demo`, no data in git) apply here unchanged and win over
anything below. Don't add a `CLAUDE.md`.

## Goal

A read-only MCP server over an **Amazon Neptune Database** copy of the final graph, the cleaning
stage's output. Any LLM agent, including the judges' agent, can use it to search nodes, inspect
neighbors, find paths and, above all, answer **"why does A connect to B?"**. Explainability is
20% of the score. Lineage comes from two places:

1. **On the edge, in Biolink terms** (`shared/schema.Edge`): `primary_knowledge_source`,
   `aggregator_knowledge_source`, `knowledge_level`, `agent_type`, `publications`,
   `original_subject`, `original_object`, `original_predicate`, plus extra slots such as
   qualifiers, `affinity` and `p_value`.
2. **Off the graph, in each source's `TransformReport`** (`shared/schema.TransformReport`),
   reached through the edge's `kgbcia_source_records`: which Croissant file, record set and
   columns produced the edge, and from which source record. Report content never goes into
   Neptune.

This stage does **not** convert or load the graph. ORION does that (see "Loading the graph").
This stage owns the server, the lineage resolution, and the deployment runbook.

The authority on how the graph looks inside Neptune is the "Amazon Neptune" section of the
README on [ORION's `opencypher` branch](https://github.com/RobokopU24/ORION/tree/opencypher).

## Implementation approach

- **FastMCP**, with one `server.py` that builds the app and registers tool modules. Keep the
  layers separate: `services/` talks to Neptune and returns plain Python data with no MCP
  imports, and `tools/` holds thin MCP wrappers that validate arguments and shape responses.
- **All configuration comes from environment variables**, with CLI flags overriding them. No
  hard-coded hosts, regions or paths.
- **Tools are written for an LLM reader.** Each docstring gives the purpose, an example CURIE,
  *Presentation Guidelines* (for example, "cite knowledge sources, publications and source
  records verbatim; never paraphrase provenance") and the tool to call next. Empty or failed
  lookups return a `hint` plus suggestions or valid values instead of raising.
- **Every query is bounded:** always anchor nodes with a label, always apply `LIMIT`, and always
  paginate.

## Data flow and deployment

```
cleaning stage -> nodes.jsonl + edges.jsonl + TransformReport JSONs
  -> orion-neptune-dump -> gzipped openCypher CSVs + neptune_load_manifest.json
  -> orion-neptune-load -> S3 -> Neptune bulk loader (nodes job, then edges job)
  -> Neptune Database cluster (Serverless, private VPC, IAM database auth on)
  -> kgb-mcp serve --transport http  (ECS/Fargate in the same VPC, behind an ALB)
                                                          <- judges' agent (URL)
     kgb-mcp serve --transport stdio (local dev)          <- team
```

- The hosted HTTP endpoint is the primary delivery, with no MCP auth. The server's task role
  allows **only `neptune-db:ReadDataViaQuery`** on the cluster (plus `neptune-db:GetGraphSummary`
  if used) and `s3:GetObject` on the reports prefix, so the graph is read-only no matter what
  query arrives.
- HTTP runs FastMCP's streamable HTTP in **stateless** mode, so any task behind the load
  balancer can answer any request. Add a `/health` route for the ALB health check.
- Local stdio needs network access to the cluster, for example an SSM or bastion port forward.
  SigV4 signing and TLS need the real cluster hostname, so map it to `127.0.0.1` in
  `/etc/hosts` rather than pointing `endpoint_url` at `localhost`. Document this in the README.
- The loading role (ORION) is separate: it needs S3 write access, plus the IAM role attached to
  the cluster that can read the bucket.
- The bulk loader only adds data. A new release means a fresh cluster, or a fast reset of the
  existing one followed by a reload. Put that procedure in the runbook.
- Infrastructure as code lives in `deploy/`, written with the **AWS CDK in Python**. Resources the
  AWS account admin already created (the S3 bucket, Bedrock access, and possibly the Neptune
  cluster) are *referenced* by name or ARN from config, never recreated. Ask which resources
  already exist before writing the stacks. Never commit account IDs, ARNs, endpoints or
  credentials. `.env` is gitignored; ship an
  `.env.example` instead.

## Layout

```
agents/mcp_interface/
├── cli.py                    # argparse + dispatch only; keeps DESCRIPTION and main(argv) -> int
├── config.py                 # NEPTUNE_HOST, NEPTUNE_PORT (8182), NEPTUNE_REGION, AWS_PROFILE,
│                             # KGB_MCP_ARTIFACTS; same NEPTUNE_* names as ORION
├── artifacts.py              # load the graph's side artifacts (see below) from a dir or S3
├── check.py                  # offline pre-load checks on the final graph (see CLI)
├── lineage.py                # index TransformReports; resolve kgbcia_source_records
├── search_index.py           # in-memory name / synonym / equivalent-ID index for search_nodes
├── services/neptune_service.py  # neptunedata openCypher wrapper + value decoding; no MCP imports
├── tools/graph_tools.py      # register_graph_tools(mcp, svc)
├── tools/query_tools.py      # register_query_tools(mcp, svc); only when --enable-cypher
├── server.py                 # create_server(), run(transport, host, port)
├── deploy/                   # Dockerfile, infrastructure as code, .env.example,
│                             # runbook incl. ORION commands
└── README.md                 # keep "Run it alone", "Input / output" and "Status" current
tests/test_mcp_interface.py   # repo tests live in the root tests/ dir
```

**Side artifacts** live in one directory or S3 prefix (`KGB_MCP_ARTIFACTS`, or `--artifacts`),
shipped next to the load and loaded once at startup:
- the `TransformReport` JSONs;
- ORION's `neptune_load_manifest.json` (graph ID, release version) and the graph metadata, for
  `get_graph_info()`;
- the final `nodes.jsonl` (or a slimmed `id`/`name`/`synonym`/`equivalent_identifiers`
  extract), for `search_index.py`.

**Fixtures:** `.gitignore` blocks `*.jsonl` everywhere, so don't commit jsonl fixtures. Build a
toy graph (~10 nodes, ~15 edges, plus one `TransformReport`) in a pytest fixture from
`shared.schema` models, in the style of `tests/test_smoke.py::_edge`, and write it to `tmp_path`.

## CLI and the pipeline contract

`shared/pipeline.py` calls `main([])` and expects it to return; it must not block and must not
call `sys.exit`.

- `kgb-mcp check --input <dir>` is the default when no subcommand is given. The default
  `--input` is the cleaning stage's output location, as documented in `data/README.md`. It works offline,
  reads `nodes.jsonl`, `edges.jsonl` and the reports, and returns 0 only if the graph is ready
  for `orion-neptune-dump`:
  - **every edge has an `id`**;
  - every `kgbcia_source_records` value is a list whose entries parse;
  - every referenced `Mapping.id` exists in some report.

  It prints counts and the exact ORION commands to run next. If the default input is missing,
  print a clear message and return nonzero.
- `kgb-mcp serve [--transport stdio|http] [--host 127.0.0.1] [--port 8000] [--enable-cypher]
  [--artifacts <dir|s3://...>]`.

The first check matters because ORION's dump keeps edge IDs **only if every edge has one**. If
even one edge lacks an `id`, ORION renumbers *all* edges sequentially, and edge IDs stop being
stable or citable. Augmentation and cleaning must give every edge they add or rewrite an `id`.

## Loading the graph (ORION, outside this package)

`robokop-orion` requires Python ≥ 3.12 and the repo targets 3.11, so ORION is **not** a
dependency of this package. Run it as a standalone tool, pinned to a commit of the `opencypher`
branch (it isn't released yet; switch to a release tag once it's merged):

```bash
ORION_SPEC="robokop-orion[neptune] @ git+https://github.com/RobokopU24/ORION@<sha>"
uvx --python 3.12 --from "$ORION_SPEC" orion-neptune-dump \
    <final>/nodes.jsonl <final>/edges.jsonl <out> \
    --graph_id kgb_cia_pharos --release_version <version>
# reads NEPTUNE_HOST, NEPTUNE_S3_URI, NEPTUNE_IAM_ROLE_ARN, NEPTUNE_REGION
uvx --python 3.12 --from "$ORION_SPEC" orion-neptune-load <out>
```

Whether the repo moves to Python 3.12 is being decided by the stages that need ORION in-process
(transformation and the merge). If it does, ORION can become an **optional** dependency group
(`neptune-load`) instead of a `uvx` call; the server itself never imports ORION. Don't bump
Python from this stage.

Copy the side artifacts to the artifacts prefix as part of the same runbook step.

Document these commands in `deploy/`, and record the graph version and date in
`data/README.md`. Check the exact CLI flags against the pinned commit; the branch is moving.

## Data format in Neptune (as written by ORION)

The server must read the format exactly as ORION writes it:

- **Arrays are delimited strings.** `publications`, `equivalent_identifiers`,
  `aggregator_knowledge_source`, `kgbcia_source_records` and every other list are stored as
  **one string joined with U+001F** (`chr(31)`). `neptune_service` splits known list properties
  back into lists, so tools always return real lists. In Cypher, use
  `split(r.publications, '\u001f')`.
- **Categories are labels only.** A node has no `category` property. Read categories with
  `labels(n)`, and anchor every lookup with ``:`biolink:NamedThing` ``.
- **`id` is both the node ID and a property.** Prefer
  ``MATCH (n:`biolink:NamedThing`) WHERE id(n) = $id``, which uses Neptune's own ID lookup.
  Confirm on the toy load that this is faster than matching on `{id: $id}`.
- **Edge `id` is the relationship `:ID`.** Confirm how to match it in openCypher on the toy load
  (`id(r)` vs a property).
- **ORION drops some property names.** Properties whose names contain `CHEBI_` are left out
  (a temporary workaround upstream). Don't depend on any such property.

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
   final graph is dumped (see "Loading the graph").

When ORION merges edges, lists concatenate and deduplicate (`orion/merging.py`), so a merged edge
carries every contributing record. Two conditions keep that true:

- The property is a **top-level** key on the edge. ORION's merge doesn't concatenate lists
  nested inside dicts; it keeps the first value and silently drops the others.
- The value is **always a list**, even for a single entry. When both values are scalars, ORION
  keeps only one.

Filter, augmentation and cleaning must preserve the property, and any Biolink-strict step must
not strip it. Augmentation gives the edges it adds their own entries and reports.

`lineage.py` indexes the `TransformReport`s' `Mapping`s by `id` at startup. For an edge, it
splits `kgbcia_source_records` on U+001F (see "Data format in Neptune"), then splits each entry
on the **first** `#` into `Mapping.id` and the record key. It returns, for each entry: the
mapping's `file`, `record_set`, `columns` and `constants`; the report's `source`,
`source_version` and `croissant`; and the record key. Group the entries by mapping, and report counts when there are
many.

**Fallback,** for an edge without the property or with an unknown `Mapping.id`: match on
`report.source` ∈ {pks} ∪ aggregators, plus predicate and categories, and label every result
`match: "candidate"`. If no reports are configured, return the Biolink provenance and say that
file and column lineage is unavailable.

## Querying Neptune Database

- Use `boto3.client("neptunedata", endpoint_url=f"https://{host}:{port}")
  .execute_open_cypher_query(openCypherQuery=..., parameters=json.dumps(...))` with IAM (SigV4)
  auth. Values always go in as parameters.
- Neptune's openCypher is not Neo4j's: there is no APOC and no graph-algorithm library on Neptune
  Database. Test every query on Neptune itself.
- **Timeouts:** set the cluster parameter `neptune_query_timeout`, and give the boto3 client a
  read timeout (`botocore.config.Config(read_timeout=...)`). Check in the docs whether a
  per-query timeout is also available.
- **`find_paths` and `find_connected_nodes`:** search hop by hop (1, then 2, up to `max_hops`) and stop at the first depth
  that finds paths. Apply `LIMIT` in Cypher. Hub nodes make deep paths explode, so allow
  excluding high-degree nodes, and return `truncated` rather than timing out silently. The
  hub list (node IDs above a degree threshold) is computed once at load time and stored with
  the side artifacts, because computing degrees at query time is expensive.
- **Search:** Neptune Database has no built-in full-text index, and `CONTAINS` on names or on
  the U+001F `equivalent_identifiers` string is a full scan. `search_nodes` uses
  `search_index.py`, an in-memory index (normalized name, synonyms, equivalent IDs → node ID)
  built at startup from the side artifacts. Only exact CURIE lookups go to Neptune.
- Cache the schema and summary stats at startup.

## Tools

All tools are read-only and return JSON-serializable dicts. Where results can be large, tools
take `limit` (default 50, hard cap 500) and `offset`, and every list response includes `total`
and `truncated` (see "Implementation approach" for docstrings and hints).

**Fewer round trips:**
- Any `*_id` argument accepts a CURIE, an equivalent identifier or an exact name. It resolves
  through `search_index`, and an ambiguous name returns the candidates instead of guessing.
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
| `query_cypher(cypher, parameters?, limit=100)` | raw openCypher for counts, rankings and patterns the other tools can't express; read-only via IAM, row-capped, with a timeout. The docstring states the U+001F list and labels-as-categories conventions, and gives two example queries |

`query_cypher` is behind `--enable-cypher`, which is **on for the hosted endpoint** the judges
use: their questions will include aggregates such as "how many Tclin targets are linked to X"
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

1. **Spike.** Dump and load the fixture with ORION into a dev cluster. ORION's Neptune export is
   tested upstream, so don't re-test it; only settle the *query* conventions marked "confirm" in
   "Data format in Neptune". Write the answers into `agents/mcp_interface/README.md` under
   "Neptune query notes", and encode them as constants in `neptune_service` so the code has one
   source of truth.
2. **Core.** `check`, `artifacts`, `lineage`, `search_index` and `neptune_service`, with unit tests.
3. **Tools.** Implement them and test them through `fastmcp.Client` with the service mocked;
   then run the Neptune smoke tests.
4. **Deploy.** Infrastructure as code, the runbook, and the hosted HTTP endpoint. Then point
   a real agent at it with a few "why does A connect to B?" questions.

## Testing

- CI has no network and no AWS access. Tests that need Neptune use a module-level
  `requires_neptune = pytest.mark.skipif(not os.getenv("NEPTUNE_HOST"), ...)`. That needs no
  custom marker registered in root `pyproject.toml`. Never call NodeNorm.
- Always-run tests:
  - `check` on the built fixture, including a graph with one edge missing its `id`, which must
    fail;
  - U+001F list decoding;
  - `search_index` lookups by name, synonym and equivalent ID;
  - lineage resolution: one record, a merged edge with many, an unknown `Mapping.id`, and `#`
    inside the record key;
  - `main([])` returning;
  - every tool through an in-memory `fastmcp.Client(mcp)` with `neptune_service` mocked.
- Neptune tests are a short smoke suite against the dev cluster loaded from the fixture: each
  tool returns sensible results, a write via `query_cypher` is rejected by IAM, and returned
  edges carry provenance. Don't test ORION's dump or load; that's covered upstream. Stop or
  delete the dev cluster afterwards; it costs money while it's up.

## Dependencies and registration

- Use `uv add fastmcp boto3`, with fastmcp 3.x pinned to an exact release at least two weeks old.
  Add nothing else without asking.
- Keep changes inside `agents/mcp_interface/` and `tests/test_mcp_interface.py`, plus
  `pyproject.toml` / `uv.lock` (dependencies) and `.pi/mcp.json`.
- **Register the server in `.pi/mcp.json`** under `mcpServers` as `kgb-mcp`, a stdio entry that
  runs `uv run kgb-mcp serve`. Neptune settings come from the environment; put no hosts or
  credentials in the file. Once the hosted endpoint exists, add a second entry for its URL, in
  the format Pi's MCP adapter expects.

## Out of scope

Converting or loading the graph (ORION does it), a skill for consuming agents, any tool that
adds, changes or deletes graph data, HTTP auth, NodeNorm calls at query time, putting
TransformReport content into Neptune, and opening PRs (the maintainer does that).
