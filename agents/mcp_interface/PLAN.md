# MCP interface implementation plan

Approved November 6, 2026. Scope for this session is build-order steps 1 and 2: Core and
Tools. Dump, database, Docker, Helm and AWS work are deferred.

## Package layout and wiring

The publishable `kgbcia-mcp` uv workspace package lives under `agents/mcp_interface/`, with
repo-only `cli.py` and `check.py` beside `src/kgbcia_mcp/`. The package contains `config.py`,
`artifacts.py`, vendored `report_models.py`, `lineage.py`, `services/neo4j_service.py`,
`server.py`, and thin modules under `tools/`. Root wiring adds the workspace member and
workspace dependency while excluding the member's source and pyproject from the root wheel.
The root `kgb-mcp` entry point remains the pipeline command; `kgbcia-mcp` is the published
entry point.

FastMCP is pinned to 3.4.11, released October 20, 2026. This was the newest 3.x release at
least two weeks old when selected on November 6, 2026. Runtime dependencies are only FastMCP,
Neo4j 5.x and Pydantic, on Python 3.12 or newer.

## Core public interfaces

- `check.py`: `CheckResult`, `resolve_default_input()`, `discover_reports()`, and
  `check_graph()`. It streams `nodes.jsonl` and `edges.jsonl` line by line and retains only
  report mapping IDs, counters and bounded error samples. Missing `kgbcia_source_records`
  fails the check and is counted by `primary_knowledge_source`. Defaults prefer the cleaning
  output and then `data/transformation/merged`; reports are discovered below
  `data/transformation/**/transform_report.json`.
- `config.py`: environment-backed `Settings`, with CLI overrides and command-specific
  validation.
- `artifacts.py`: resolve and load a local directory or cached, checksum-verified HTTP(S)
  tarball. Artifacts contain `manifest.json` (graph version and file list),
  `graph_metadata.json`, `hubs.json`, and `reports/**/transform_report.json`.
- `report_models.py`: vendored `ColumnRef`, `Mapping`, `Skipped`, and `TransformReport` models.
- `lineage.py`: index reports and resolve source-record references, splitting on the first
  `#`, grouping records by mapping, and retaining record keys verbatim. Missing references and
  unknown mappings use candidate matching by knowledge source, predicate and endpoint category.
- `neo4j_service.py`: Neo4j driver lifecycle, read transactions, bounded results, cache access,
  and one recursive serializer for Neo4j values. Constants capture the assumptions that must be
  confirmed on the first k8s load, each marked `TODO(k8s)`.

## Tools public interfaces

`server.create_server()` builds the FastMCP app and `server.run()` starts stdio or streamable
HTTP. `register_graph_tools()` registers graph information, schema, node search and lookup,
edge summaries and neighbors, direct edges, paths, connected nodes, and edge explanations.
`register_query_tools()` conditionally registers bounded read-only Cypher. Service code owns
Cypher and cached-schema validation; wrappers own argument bounds, response hints and
LLM-oriented documentation.

Every tool is tested through an in-memory `fastmcp.Client` with `neo4j_service` mocked. Tool
responses include labels and provenance needed to avoid extra calls, pagination metadata, and
helpful hints instead of raising for empty or failed lookups.

## Tests

The root test module builds a toy graph from `shared.schema` models (about ten nodes, fifteen
edges and TransformReports) under `tmp_path`; no database or development sample is used.
Coverage includes:

- streaming check success; missing edge IDs; absent, malformed and non-list source records;
  unknown mapping IDs; default input and report discovery; and per-source counts;
- exact, merged, unknown and `#`-containing lineage, candidate and no-report fallbacks;
- vendored report-model parity with `shared.schema`;
- local and mocked-URL artifacts, manifest validation, checksum mismatch, safe extraction and
  cache reuse;
- package independence from `shared` and `agents`;
- Neo4j result serialization and bounded read transactions;
- `main([])` returning without blocking;
- every MCP tool, ambiguity and empty hints, pagination and caps, schema-name validation, hop
  limits and hub handling, lineage inclusion, and optional read-only Cypher enforcement;
- wheel build, clean-venv install and startup smoke test.

At each implementation checkpoint run `make lint`, `make test`, and `make demo`. After Tools,
also build from `agents/mcp_interface/`, install the wheel in a clean virtual environment, and
confirm that its command starts.

## Approved decisions

- An edge without `kgbcia_source_records` fails the offline check; the running server retains
  candidate lineage fallback.
- Artifact names and layout are fixed as described above.
- Until the first real load confirms Neo4j behavior, the full-text property constant includes
  `name`, `synonym`, and `equivalent_identifiers`.
- Unconfirmed ID matching, index names, and full-text list behavior are named constants in
  `neo4j_service.py` with `# TODO(k8s): confirm on first load`.

## Approved build-order step 3 plan

### Files and tests

- Add `deploy/make_dev_artifacts.py`, `deploy/extra_indexes.cypher`,
  `deploy/prepare_dump.sh`, `deploy/Dockerfile`, and `deploy/.env.example`.
- Add the `deploy/helm/kgb-mcp/` chart (`Chart.yaml`, helpers, Secret, Service,
  StatefulSet, optional MCP Deployment/Service/Ingress, and values files), plus
  `deploy/RUNBOOK.md`. Extend `tests/test_mcp_interface.py` with deterministic
  unit coverage for synthetic development artifacts and index-script inputs; keep
  Docker and cluster operations out of CI. Add conditional Helm lint/template
  coverage when `helm` is available.

### Dump preparation

`make_dev_artifacts.py` will read the local sample's KGX files, synthesize one
`TransformReport` per primary knowledge source and mappings per
`(predicate, subject category, object category)`, then write rewritten development
edges with IDs and list-valued `kgbcia_source_records` (including selected multiple
entries), marking all reports `source_version: "synthetic"`.

`prepare_dump.sh` will: validate explicit input/output arguments; run the pinned
ORION image's `orion-neo4j-dump`; load that dump into a temporary writable pinned
Neo4j 5.26 Community container; execute `extra_indexes.cypher` and
`CALL db.awaitIndexes()`; collect the required first-load inspection output; run
`kgbcia-mcp hubs`; stop the temporary database; produce the final kgb dump; create
an artifacts tarball containing manifest, graph metadata, hubs, and reports; and
print SHA-256 values for both artifacts. It will clean up temporary containers and
volumes on success or failure.

`extra_indexes.cypher` will make the idempotent full-text named-thing index and
relationship `id` range indexes generated from `db.relationshipTypes()`, then await
indexes. The Dockerfile will build the optional HTTP MCP image from the standalone
package and run `kgbcia-mcp serve --transport http` as a non-root user.

### Helm chart

The chart will pin one exact Neo4j 5.26 Community patch in `values.yaml`; the
StatefulSet uses a PVC and checksum-marker-based idempotent download and load init
containers. Values expose the dump/artifact URLs and checksums, Neo4j password via
an existing Secret reference, resource/cache/heap/transaction/Bolt limits, and
Service exposure. The default is private `ClusterIP`, read-only from first start,
and MCP disabled. The optional MCP resources receive the in-cluster Bolt URI,
artifacts URL/checksum, health probes, and optional Ingress. `values-dev.yaml` and
`values-aws.yaml` change only environment sizing/exposure inputs and contain no
secrets. Templates will be checked with `helm lint` and `helm template` for all
three values files.

### Runbook outline

1. Build synthetic sample artifacts and run the offline check.
2. Prepare, inspect, checksum, and record a release dump/artifacts tarball.
3. Publish both files to the local HTTP file server (development) or S3 (AWS), and
   update only URLs/checksums in the applicable values inputs.
4. Create the Neo4j password Secret, install/upgrade the chart, wait for the load,
   port-forward Bolt, and run health/read-only/lineage verification.
5. Cover AWS prerequisites and cost hygiene (stop between sessions and delete after
   judging), without performing AWS work in this step.

### Approved deployment decisions

- The chart and dump preparation pin `neo4j:5.26.31-community`.
- ORION is pinned to
  `ghcr.io/robokopu24/orion@sha256:913006ca778f3279e1563078bb25801714db62778e3e35d6e33062eb057b6994`
  (v2.0.13; Neo4j 5.25.1; `orion-neo4j-dump` verified).
- Synthetic mappings derive endpoint categories from `nodes.jsonl`, using each node's
  first category.
- The chart will reference a pre-created Secret with `neo4j-auth` and
  `user-password`. It sets `NEO4J_AUTH` from `neo4j-auth`; a post-install hook
  will idempotently create the values-configured `kgbcia` account in the system
  database. Whether this works while the graph database is read-only remains a
  `TODO(k8s)` for the first cluster load.

## Proposed build-order step 4 (AWS development-sample deployment)

**Status: completed November 12, 2026.** The LitCoin development-sample deployment and laptop
verification passed on the prepared host. This was a maintainer-operated test deployment only;
it used the existing single-node arm64 k3s host,
the LitCoin development-sample `kgb.dump`, and the public artifacts bundle. It does not
publish the package to PyPI, deploy the optional MCP HTTP service, change AWS resources,
or reconfigure k3s or Helm.

### Current chart review

The existing chart already meets the central AWS requirements:

- `neo4j:5.26.31-community` and `alpine:3.20.3` support arm64; the StatefulSet uses the
  required persistent volume, checksum-keyed `.kgbcia-load.sha256` marker, download and
  `neo4j-admin database load` init containers.
- The marker is keyed by `neo4j.dumpSha256`, not the dump URL, so a new presigned URL for
  the same private object does not reload the graph.
- Neo4j is read-only from first start and accepts heap, page cache, transaction, and Bolt
  thread limits through values. The existing Secret remains external to Helm.
- `mcp.enabled` is already `false`; the Neo4j Service supports a Bolt-only
  `LoadBalancer`, which k3s ServiceLB exposes on the instance address. HTTP remains disabled.
- The existing `user-job.yaml` creates/reconciles `kgbcia` through the `system` database;
  local-k8s has already confirmed both raw graph-write rejection and the Community-edition
  user-management risk.

`values-aws.yaml` is deliberately for the eventual real graph, but cannot schedule on the
prepared r7g.large: it requests 4 CPUs/24 GiB and a 500 Gi PVC while the test host has 2
vCPU, 16 GiB RAM, and a 50 GiB root disk. It must not be used for this sample deployment.

The instance runs Helm 4.3.1. The installation sequence will use Helm's supported
`upgrade --install`, repeated `-f`, `--set-string`, `--namespace`, `--wait`,
`--wait-for-jobs`, and `--timeout` flags; it will not rely on a Helm 3-only workflow.
`--wait-for-jobs` is necessary because the post-install/post-upgrade account Job must finish
before laptop verification logs in as `kgbcia`.

### Planned files and changes

1. Add `deploy/aws/verify_host.sh`, read-only and installing nothing. It will check that
   `k3s` is active, the sole node is `Ready`, `uname -m` is `aarch64`, the default storage
   class is `local-path`, Helm reports major version 4, `git` is present, and free root-disk
   space is sufficient. Its README section will document (but never execute) the manual host
   rebuild prerequisites: Amazon Linux 2023 arm64, k3s with only Traefik disabled, ServiceLB,
   local-path storage, Helm 4, and SSM access.
2. Add `deploy/aws/values-aws-test.yaml`. It will overlay the chart defaults with the known
   sample sizing (5 Gi PVC; 2 CPU/4 GiB Neo4j limit; 1 GiB initial / 2 GiB maximum heap; 1 GiB page cache;
   1 GiB transaction cap), `LoadBalancer` Bolt exposure, HTTP disabled, and
   `mcp.enabled=false`. It will use only placeholders for URLs/checksums: no account ID,
   Elastic IP, password, key, or presigned URL is committed.
3. Add `deploy/aws/install.sh`. On the instance, from a checkout of this branch, it will call
   the existing password-prompting `../create_neo4j_secret.sh`, then execute
   `helm upgrade --install` using the test overlay. The maintainer supplies the presigned dump
   URL and dump/artifact checksums as command arguments; the URL is used only as a
   `--set-string` value and is neither logged deliberately nor written to a file. The script
   will wait for the StatefulSet and hook Job, then print the Bolt Service and ServiceLB pod.
   It will create no AWS infrastructure and make no S3/AWS CLI calls.
4. Add `deploy/aws/README.md` with the exact three-part sequence: generate the short-lived
   dump URL on the laptop; SSM to the host and run verification/install; then verify from the
   laptop. It will also document stopping/starting the instance, a checksum-changing upgrade,
   uninstall/PVC deletion, and releasing the Elastic IP after teardown. The public artifact
   URL is used only in the operator's shell environment for the local stdio MCP process, not
   committed as an account-specific chart value.
5. Amend `deploy/RUNBOOK.md` to direct AWS operators to `deploy/aws/README.md` rather than
   instructing them to install k3s/Helm or recreate AWS resources. Amend the stage README
   with a short AWS pointer and a clearly dated deployment/verification Status entry, while
   retaining the existing **Neo4j notes** verbatim.
6. Make only any chart corrections exposed by rendering the sample overlay. At present the
   required chart-side change is procedural rather than template logic: the installer must
   use `--wait-for-jobs`. No template change is planned unless Helm 4 lint/template exposes a
   defect. Run `helm lint` and `helm template` with the new test overlay, then `make lint` and
   `make test` before returning operator commands.

### Checkpoint sequence

- **Checkpoint 1 (this checkpoint):** approve this plan before files change.
- **Checkpoint 2:** after implementation and local validation, provide commands only; the
  maintainer runs all laptop and instance steps and reports results.
- **Checkpoint 3:** after results, guide laptop-only verification: `kgbcia` Bolt login, a
  rejected write, integration tests with the Elastic-IP Bolt URI, and one local-stdio
  `kgbcia-mcp serve` provenance answer. Record only outcomes (never credentials or URLs), fix
  failures, and reconfirm/document the already-observed Community user-management risk.
