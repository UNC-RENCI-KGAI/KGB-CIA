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
- The chart references a pre-created Secret with `neo4j-auth` and `user-password`. It sets
  `NEO4J_AUTH` from `neo4j-auth`; its post-install hook idempotently creates the
  values-configured `kgbcia` account in the system database. This was confirmed on local k8s
  and the AWS development-sample deployment while the graph database remained read-only.

## Build-order step 4 (AWS development-sample deployment)

**Status: completed November 12, 2026.** The LitCoin development-sample deployment and laptop
verification passed on the prepared host. This was a maintainer-operated test deployment only;
it used the existing single-node arm64 k3s host,
the LitCoin development-sample `kgb.dump`, and the public artifacts bundle. It does not
publish the package to PyPI, deploy the optional MCP HTTP service, change AWS resources,
or reconfigure k3s or Helm.

### Deployment handoff record

At the completed test-deployment checkpoint, the active Helm release is **`kgb-mcp`** in
namespace **`kgb-mcp`**. It was installed from
`agents/mcp_interface/deploy/helm/kgb-mcp` using the chart defaults plus
`deploy/aws/values-aws-test.yaml`; the real-graph `values-aws.yaml` was **not** used. The
installer additionally passed the private dump URL and dump checksum through Helm
`--set-string`, and passed the artifacts checksum for the disabled optional MCP deployment.
No URL, password, host, or other account-specific value is recorded in Git.

The test overlay is the LitCoin-sized configuration: a 5 Gi local-path PVC, a 2 CPU / 4 GiB
Neo4j limit, 1 GiB initial / 2 GiB maximum heap, 1 GiB page cache, Bolt-only `LoadBalancer`,
and `mcp.enabled=false`. The server is therefore not deployed to AWS: laptop clients run
`kgbcia-mcp serve` locally over stdio against Bolt.

The dump was downloaded once by the StatefulSet's `download-dump` init container from a
two-hour presigned URL, SHA-256 verified, and loaded by the `load-dump` init container with
`neo4j-admin database load --overwrite-destination`. The `.kgbcia-load.sha256` marker on the
PVC is keyed to the dump checksum. With that PVC and matching marker intact, upgrades and pod
restarts skip both download and load; a lost/deleted PVC needs a newly presigned URL and a
fresh load. The public artifacts tarball is fetched later by each local MCP process, not by the
AWS chart while MCP remains disabled.

One non-obvious recovery occurred during this deployment. An SSM inactivity timeout terminated
the local Helm client after Kubernetes had loaded Neo4j, leaving Helm revision 1 in
`pending-install`; the PVC and database were healthy. The initial account hook then found the
newly created `kgbcia` password unchanged and Neo4j returned `Old password and new password
cannot be the same.` as an error. `user-job.yaml` now accepts only that exact response as the
idempotent success case and still fails all other account-update errors. If a future interrupted
install leaves a pending release, preserve the PVC, uninstall only the Helm release and failed
hook Job, verify the PVC remains `Bound`, then reinstall with the corrected chart. Never delete
the PVC merely to clear Helm state.

Verification passed: `kgbcia` logged in through the external Bolt Service; raw graph `CREATE`
was rejected; the configured AWS integration smoke test passed; and a local-stdio MCP server
answered a direct-edge "why does A connect to B?" question with lineage. `kgbcia` can create
and drop system users on Neo4j Community even though graph writes are rejected. This known risk
must be explicitly accepted before any broader/public exposure, or the documented
`NEO4J_AUTH=none` alternative must be selected.

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

## Proposed checkpoint 1: AWS authentication-free deployment

**Approved decision:** use `NEO4J_AUTH=none` for AWS rather than expose a Neo4j Community
account that can administer users in the `system` database.  This removes authentication from
an externally reachable Bolt service, so the maintainer retains sole control over network
exposure (security groups and all other AWS resources remain out of scope).  Graph protection
continues to be enforced by `server.databases.default_to_read_only=true`, Neo4j transaction and
Bolt limits, and the MCP server's existing read transaction, `EXPLAIN`, timeout, and row-cap
protections.  It also eliminates reusable plaintext Bolt passwords and the Community
user-management escalation observed in the November 12 test deployment.

### Chart and installer changes

1. Add `neo4j.auth.enabled: true` to the chart's `values.yaml`.  Authentication remains the
   local-k8s default, preserving the existing Secret and `kgbcia`-account workflow there.
2. Set `neo4j.auth.enabled: false` in both
   `deploy/helm/kgb-mcp/values-aws.yaml` and `deploy/aws/values-aws-test.yaml`.
3. Make all Secret-dependent chart resources conditional on that setting:
   - render `NEO4J_AUTH=none` as a literal in the Neo4j StatefulSet when disabled; otherwise
     retain the existing `secretKeyRef`;
   - omit `user-job.yaml` completely when disabled; and
   - when optional in-cluster MCP is enabled with auth disabled, omit `NEO4J_USER` and
     `NEO4J_PASSWORD` there as well, so it uses the same unauthenticated connection mode and
     needs no Secret.
4. Update `deploy/aws/install.sh` to accept a values-file argument and determine whether to
   create the Secret from that file's resolved `neo4j.auth.enabled` setting, not from an overlay
   name. When auth is enabled it will create/update, then verify, the required Secret and fail
   clearly if it remains missing; when disabled it skips the Secret step. It retains the precise
   recorded upgrade inputs: release `kgb-mcp`, namespace supplied as `kgb-mcp`,
   `deploy/aws/values-aws-test.yaml`, and the dump URL/checksum plus artifacts checksum passed
   with `--set-string`. It will still use Helm 4's `upgrade --install`,
   `--wait`, `--wait-for-jobs`, and `--timeout 30m`; no PVC, dump-marker, AWS, security-group,
   or other infrastructure behavior changes.

### MCP package behavior

1. Treat `NEO4J_USER` and `NEO4J_PASSWORD` as an all-or-none optional pair.  `NEO4J_URI` remains
   required for a Neo4j operation; neither credential set means an unauthenticated driver, and
   only one set produces a clear configuration error rather than guessing.
2. Change driver creation so the no-credential case calls `GraphDatabase.driver(uri)` with no
   `auth=` argument.  The credentialed case continues to pass the existing tuple.
3. Revise configuration warnings and tool hints to say that `NEO4J_URI` is required and that
   credentials are required only for an authenticated Neo4j deployment.  Do not alter the
   read-transaction, `EXPLAIN`, query timeout, forbidden-Cypher, or row-cap code paths.

### Documentation and tests

- Revise the specification's **Read-only enforcement** section in `AGENTS.md` to make this the
  selected AWS pattern, explain the no-auth exposure trade-off and retained safeguards, and
  replace the former pending Community-account decision.
- Update the stage `README.md`, `deploy/RUNBOOK.md`, and `deploy/aws/README.md` for the two
  explicit modes: authenticated local k8s versus no-auth AWS.  AWS laptop examples will contain
  no `NEO4J_USER` or `NEO4J_PASSWORD`; they will retain the exact active release/namespace and
  sample-overlay handoff information, including that the pre-upgrade Secret and `kgbcia` account
  remain unused and how to delete that Secret. The README holds the authenticated local-k8s
  example. `.pi/mcp.json` has one `kgbcia-mcp` AWS entry containing URI and artifact settings
  only (with its artifact checksum).
- Add focused tests for Settings with URI and no credentials, rejection of a partial credential
  pair, and a mocked driver created without `auth`.  Extend Helm render coverage to render both
  auth settings, asserting the enabled render retains Secret references and the user Job while
  the disabled render has literal `NEO4J_AUTH=none`, no Secret reference, and no user Job.

### Validation and next checkpoint

After approval, run `helm lint` and `helm template` for every supported values file (including
both AWS overlays), then `make lint`, `make test`, and `make demo`.  Stop after reporting those
results and provide the exact AWS `upgrade --install` command based on the recorded active
release (`kgb-mcp`), namespace (`kgb-mcp`), and test values file—without executing it or
changing any AWS resource.

### Checkpoint 3 verification record (completed)

The maintainer upgraded the AWS test release `kgb-mcp` in namespace `kgb-mcp` to Helm revision
2 with `deploy/aws/values-aws-test.yaml`. The installer reported that authentication was disabled
and skipped Secret creation; the StatefulSet rollout and Bolt ServiceLB pod completed.

Unauthenticated laptop verification against the development-sample graph recorded:

- ``MATCH (n:`biolink:NamedThing`) RETURN count(n)`` succeeded with **1,192** nodes.
- A graph `CREATE` was rejected with
  `Neo.ClientError.General.WriteOnReadOnlyAccessDatabase`: the graph database remains read-only.
- `CREATE USER kgbcia_aws_auth_probe_nov2026` succeeded anonymously in `system`; the temporary
  probe user was then successfully dropped. This confirms that no-auth Neo4j Community grants
  system-user administration to a raw Bolt client, although the probe left no user behind.
- The first dynamic-setting check did not execute because its preliminary `SHOW SETTINGS` query
  used a non-existent `dynamic` yield column and raised
  `Neo.ClientError.Statement.SyntaxError`. The corrected probe confirmed
  `db.transaction.timeout` was `30s`, then attempted
  `dbms.setConfigValue('db.transaction.timeout', '31s')`; Neo4j rejected it with
  `Neo.ClientError.Procedure.ProcedureNotFound` because that procedure is not registered. No
  setting changed and no restoration was needed.
- The first stop/start attempt used `STOP DATABASE neo4j WAIT 60 SECONDS` and was rejected with
  `Neo.ClientError.Statement.UnsupportedAdministrationCommand`. The final exact
  `STOP DATABASE neo4j` probe was also rejected with the same code and no database state changed.
  Because stopping was unsupported, no `START DATABASE` or recovery action was needed.

**Checkpoint 3 conclusion:** unauthenticated Bolt can read the graph and execute `CREATE USER`
in `system`, but the tested graph write, dynamic-setting procedure, and database-stop commands
were rejected. `CREATE USER` is not a safe authorization boundary: it created a persistent user
until the test removed it, and such a user would matter if authentication were ever re-enabled.
No-auth AWS is therefore acceptable only while the maintainer restricts Bolt network exposure;
read-only mode protects graph data but not all system administration.
