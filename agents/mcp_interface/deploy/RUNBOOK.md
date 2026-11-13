# KGB MCP release and deployment runbook

This runbook deploys the same pinned Neo4j 5.26.31 database, dump, artifact bundle,
and `kgbcia-mcp` image to local Kubernetes or the final AWS k3s host. It never stores
passwords, hosts, account IDs, or ARNs in Git.

## 1. Prepare a graph release

Run the offline check and dump preparation from the repository root. The development
sample has synthetic lineage, so pass its report explicitly:

```bash
uv run kgb-mcp check \
  --input data/mcp_interface/dev_artifacts \
  --report data/mcp_interface/dev_artifacts/reports/infores_litcoin/transform_report.json

bash agents/mcp_interface/deploy/prepare_dump.sh \
  --graph-dir data/mcp_interface/dev_artifacts \
  --reports-dir data/mcp_interface/dev_artifacts/reports \
  --graph-metadata data/mcp_interface/sample/LitCoin_ORION_pipeline.meta.json \
  --output-dir data/mcp_interface/prepared \
  --graph-version litcoin-200-synthetic
```

For a real release, use the cleaning output and its actual TransformReports instead. The script
loads the dump into a temporary writable Neo4j and precomputes the graph summary, schema
meta-graph, node-property examples, edge-property examples, predicate/category/source counts,
and quality-flag counts into `graph_metadata.json`; the deployed read-only database is not asked
to run those whole-graph aggregates at startup or per request.

When the deployed dump is already correct and only the MCP artifacts need refreshing, skip ORION
and start from the existing dump:

```bash
bash agents/mcp_interface/deploy/prepare_dump.sh \
  --artifacts-only \
  --existing-dump data/mcp_interface/dev_artifacts/kgb.dump \
  --reports-dir data/transformation/graphs \
  --graph-metadata data/transformation/merged/merge-metadata.json \
  --output-dir data/mcp_interface/prepared-artifacts-only \
  --graph-version real-cleaned-2026-11-09
```

`--artifacts-only` still loads the dump into temporary Neo4j to recompute hubs and statistics,
but it copies the input dump to the output unchanged. Publishing only the new
`artifacts.tar.gz` and checksum is therefore enough; do not reinstall the dump or change the
Helm dump checksum for an artifacts-only refresh.

For the real graph's build description follow-up, generate `data/describe/description.json` on
the VM that has the Nov 9 transformation workspaces and cleaning report, for example by running
`.agents/skills/filter/describe-graph/scripts/describe_graph.py` there against the 11-09
transformation output and `data/cleaning/cleaning_report.json`. A later checkpoint wires that
file into `prepare_dump.sh` through an explicit argument (defaulting to
`data/describe/description.json`); when the file is supplied, the dump-preparation script will
copy and sanitize it rather than re-running the skill.

Record the graph version, dump checksum, artifact checksum, source version, and download
date in `data/README.md`. The current sample release is:

```text
kgb.dump:         5f3e7f0fdad1074ff0ff0eaea23ce95eb98a2a04639cc7b38d15bbe4809b0cc5
artifacts.tar.gz: 3ec97998f4716dffe7be69c13327b79ad1529c1b974c3ff228453486702cae56
```

`kgb.dump` is private and only fetched once by an init container. `artifacts.tar.gz`
contains no secrets and must have a public, durable URL because each local stdio MCP
server downloads it without credentials.

## 2. Manually publish the two release files

Publishing is a maintainer-operated release action, not a chart or repository script. Copy
`kgb.dump` and `artifacts.tar.gz` manually to the local-cluster HTTP file server, then verify
that Kubernetes can fetch the resulting URLs. Do not use `localhost` in chart values: it
would refer to a Kubernetes node or pod, not the host running the file server.

For AWS, manually upload the artifact tarball as a public, durable S3 object (or GitHub
Release asset). Upload the dump privately, then manually generate a short-lived presigned
URL immediately before an install or upgrade. A changing presigned URL with the same checksum
does not reload the database: the chart's marker is keyed only on the checksum.

## 3. Optional MCP HTTP image

The image is only needed when `mcp.enabled=true`; users of the primary local stdio MCP
workflow do not need it. The development values keep it disabled, so skip this step for
local Pi testing. If an HTTP-only client is added later, build the image from
`agents/mcp_interface/` with:

```bash
docker build -f deploy/Dockerfile -t kgbcia-mcp:dev .
```

## 4. Configure authentication

`neo4j.auth.enabled` controls authentication for one chart rather than maintaining chart forks.
It defaults to `true`, so local k8s uses the existing Secret named `kgb-mcp-auth`. Create or
rotate it with the maintainer-operated helper; it stores `neo4j-auth` (the complete
`neo4j/<password>` value) and `user-password` (for the `kgbcia` post-install Job):

```bash
bash agents/mcp_interface/deploy/create_neo4j_secret.sh <namespace>
```

For a password rotation, supply the existing Neo4j admin password and the new `kgbcia`
password, then run `helm upgrade`. The post-upgrade Job reconciles the database account to
`user-password`; updating the Secret alone does not change an already-created Neo4j user.

Both AWS values overlays set `neo4j.auth.enabled: false`. In that mode the StatefulSet receives
`NEO4J_AUTH=none`; the chart renders neither a Secret reference nor the user-creation Job, and
AWS `install.sh` skips Secret creation. The existing `kgbcia` account is not used. AWS exposes
an unauthenticated Bolt endpoint, so the maintainer alone controls its network exposure. Its
remaining safeguards are Neo4j's database-level read-only setting, transaction and Bolt limits,
and the MCP server's read-transaction, `EXPLAIN`, timeout, and row-cap protections.

Neo4j Community lets the separate `kgbcia` account administer users in `system` despite graph
read-only mode. No-auth AWS avoids a reusable password over plain Bolt but does **not** remove
system administration: verification confirmed that an unauthenticated raw Bolt client can
create a user (the temporary probe was removed). It is not safe to expose broadly; retain strict
maintainer-controlled Bolt network exposure, and reassess authentication before widening it.

## 5. Install or upgrade the local cluster deployment

`values-dev.yaml` contains the manually published development release URLs and checksums.
Set `NAMESPACE` to the existing namespace, then lint, render, and install:

```bash
export NAMESPACE=<existing-namespace>

helm lint agents/mcp_interface/deploy/helm/kgb-mcp \
  -f agents/mcp_interface/deploy/helm/kgb-mcp/values-dev.yaml

helm template kgb-mcp agents/mcp_interface/deploy/helm/kgb-mcp \
  -f agents/mcp_interface/deploy/helm/kgb-mcp/values-dev.yaml \
  > /tmp/kgb-mcp-rendered.yaml

helm upgrade --install kgb-mcp agents/mcp_interface/deploy/helm/kgb-mcp \
  --namespace "$NAMESPACE" \
  -f agents/mcp_interface/deploy/helm/kgb-mcp/values-dev.yaml \
  --wait --timeout 20m

kubectl -n "$NAMESPACE" get pods,svc,jobs
kubectl -n "$NAMESPACE" logs statefulset/kgb-mcp-neo4j -c neo4j
```

The init containers skip download/load on a restart when `.kgbcia-load.sha256` matches the
configured dump checksum. A changed checksum deliberately downloads and loads a new dump.
For local inspection and later integration tests:

```bash
kubectl -n "$NAMESPACE" port-forward svc/kgb-mcp-neo4j 7687:7687
export NEO4J_URI=bolt://127.0.0.1:7687
export NEO4J_USER=kgbcia
export NEO4J_PASSWORD='<the user-password used above>'
export KGB_MCP_ARTIFACTS='<the public artifacts.tar.gz URL>'
export KGB_MCP_ARTIFACTS_SHA256=3ec97998f4716dffe7be69c13327b79ad1529c1b974c3ff228453486702cae56
```

## 6. AWS development-sample deployment

For the prepared final AWS host, follow [`aws/README.md`](aws/README.md). It is the
maintainer-operated sequence for the arm64 single-node k3s host: verify the already-prepared
host, generate a two-hour presigned URL for the private dump, use the sample-sized
`aws/values-aws-test.yaml` overlay, and verify from a laptop over Bolt.

Do not install or reconfigure k3s or Helm, recreate AWS resources, put the presigned URL in a
values file, or enable the optional MCP HTTP deployment. Both AWS overlays use unauthenticated
Neo4j (`NEO4J_AUTH=none`); see the AWS README for its exposure and unused-Secret notes. The
final real-graph deployment will use the same chart and process with `values-aws.yaml` only
after this sample deployment passes.
