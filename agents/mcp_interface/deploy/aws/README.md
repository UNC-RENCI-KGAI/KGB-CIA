# AWS development-sample deployment

This procedure deploys the LitCoin development-sample dump to the prepared single-node AWS
k3s host. The maintainer runs every command. It does not install or reconfigure k3s or Helm,
create AWS resources, deploy the MCP HTTP server, or publish `kgbcia-mcp` to PyPI.

The chart exposes only Neo4j Bolt through k3s ServiceLB. The separate `kgbcia` account and
Neo4j's database-wide read-only setting protect graph data, but Neo4j Community does not have
role-based access control; the known ability of `kgbcia` to manage system users is documented
in the parent [runbook](../RUNBOOK.md).

## Current deployment handoff (November 12, 2026)

The LitCoin development-sample deployment is active as Helm release **`kgb-mcp`** in namespace
**`kgb-mcp`**. It uses the chart at `../helm/kgb-mcp` with its default `values.yaml` plus this
directory's `values-aws-test.yaml`. It does **not** use `../helm/kgb-mcp/values-aws.yaml`,
which is reserved for a future real graph and is too large for this development instance.
The test overlay gives Neo4j a 5 Gi local-path PVC, 2 CPU / 4 GiB limit, 1 GiB initial / 2 GiB
maximum heap, 1 GiB page cache, Bolt-only `LoadBalancer` exposure, and `mcp.enabled=false`.

The private LitCoin `kgb.dump` was supplied only as a two-hour presigned URL at install time.
The `download-dump` init container verified its SHA-256; `load-dump` then ran
`neo4j-admin database load --overwrite-destination`. A checksum-keyed
`.kgbcia-load.sha256` marker resides on PVC `data-kgb-mcp-neo4j-0`. Keep that PVC to preserve
the loaded database: pod restarts and re-installs with the same dump checksum skip both download
and load. If the PVC is lost or deliberately deleted, generate a fresh presigned URL and load
the dump again. The public artifacts tarball is consumed by each laptop-local stdio MCP process,
not by this AWS chart while MCP is disabled.

Verification completed successfully from a laptop: Bolt login as `kgbcia`, raw graph-write
rejection, the configured integration smoke test, and a local `kgbcia-mcp serve` lineage answer
for a direct edge. `kgbcia` was also confirmed able to create and drop system users despite the
read-only graph database. That Neo4j Community risk remains an explicit decision before any
broader/public exposure.

### Interrupted-install recovery already exercised

An SSM inactivity timeout can terminate the local Helm client without rolling back Kubernetes
resources. During this deployment that left revision 1 in `pending-install` after the PVC had
already loaded successfully. Preserve the PVC in this situation. The original account hook also
created `kgbcia` and then failed because Neo4j treats an unchanged `ALTER USER` password as an
error. The corrected `user-job.yaml` accepts only `Old password and new password cannot be the
same.` as idempotent success. If this recovery is needed again: use the corrected checkout,
uninstall the stuck Helm release and delete only its failed hook Job, verify the PVC remains
`Bound`, then rerun `install.sh`. Do **not** delete the PVC merely to recover Helm state.

## 1. On the laptop: create a short-lived dump URL

Confirm that the development-sample checksums are the release values in the parent runbook,
then set them in your shell. The artifact URL is public and long-lived; use the URL supplied by
the AWS administrator. Do not save either URL in a repository file.

```bash
export DUMP_SHA256=5f3e7f0fdad1074ff0ff0eaea23ce95eb98a2a04639cc7b38d15bbe4809b0cc5
export ARTIFACTS_SHA256=3ec97998f4716dffe7be69c13327b79ad1529c1b974c3ff228453486702cae56
export ARTIFACTS_URL='<public artifacts.tar.gz URL supplied by the AWS administrator>'
DUMP_URL="$(aws s3 presign 's3://<private bucket created by the AWS administrator>/neo4j-dumps/kgb.dump' \
  --region us-east-2 --expires-in 7200)"
printf '%s\n' "$DUMP_URL"
```

`DUMP_URL` expires after **two hours**. Generate it immediately before installation. Passing it
as an `install.sh` argument leaves it in the instance shell history; that is accepted for this
short-lived URL. If the PVC/volume is lost before the load completes or before a later restart,
generate a new URL and reinstall: the dump must be downloaded again. A restart with the same
PVC and checksum marker does not need a new URL.

## 2. On the instance: verify and install

Start an SSM session from the laptop, then change to a checkout of this branch. The namespace
must already exist; create it explicitly if this is the first deployment.

```bash
aws ssm start-session --target '<prepared EC2 instance ID>'

cd /path/to/KGB-CIA
bash agents/mcp_interface/deploy/aws/verify_host.sh

export NAMESPACE=kgb-mcp
export DUMP_SHA256=5f3e7f0fdad1074ff0ff0eaea23ce95eb98a2a04639cc7b38d15bbe4809b0cc5
export ARTIFACTS_SHA256=3ec97998f4716dffe7be69c13327b79ad1529c1b974c3ff228453486702cae56
read -r -p 'Paste the fresh presigned dump URL: ' DUMP_URL
kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

bash agents/mcp_interface/deploy/aws/install.sh \
  "$NAMESPACE" "$DUMP_URL" "$DUMP_SHA256" "$ARTIFACTS_SHA256"
```

The installer prompts for the private Neo4j admin password and the `kgbcia` password, creates
or updates the existing-Secret format, waits for the Neo4j StatefulSet and account hook Job,
and prints the Bolt Service plus its `svclb` pod. It receives the dump URL only on the command
line and never writes it to a values file. Keep the SSM session open until the load succeeds;
if the two-hour URL expires first, create a new one on the laptop and rerun the installer.

## 3. On the laptop: prepare later verification

After the ServiceLB pod is ready, use the Elastic IP's Bolt port—not HTTP—and configure the
local stdio server shell. Do not put the password or the Elastic IP in a committed file.

```bash
export NEO4J_URI='bolt://<instance Elastic IP>:7687'
export NEO4J_USER=kgbcia
read -rsp 'kgbcia password: ' NEO4J_PASSWORD; echo
export NEO4J_PASSWORD
export KGB_MCP_ARTIFACTS="$ARTIFACTS_URL"
export KGB_MCP_ARTIFACTS_SHA256="$ARTIFACTS_SHA256"
```

For a new deployment or recovery, then verify Bolt login, read-only enforcement, the integration
tests, and a local `kgbcia-mcp serve` lineage answer as recorded in the handoff section above.

## Operations

### Stop and start

Stop the instance between sessions using the AWS console or administrator-approved AWS CLI.
The Elastic IP stays allocated while attached. On restart, check the StatefulSet and ServiceLB:

```bash
kubectl -n kgb-mcp get pods,svc
kubectl -n kube-system get pods -l svccontroller.k3s.cattle.io/svcname=kgb-mcp-neo4j
```

With the PVC and `.kgbcia-load.sha256` marker intact, the init containers skip the dump download
and load. If the volume was deleted or lost, follow section 1 again to re-presign the dump URL,
then rerun `install.sh`.

### Upgrade to a new dump

Prepare and verify the new dump and artifacts first. Set the new dump checksum (and artifacts
checksum) in the laptop shell, create a fresh short-lived dump URL, and rerun `install.sh`.
A changed dump checksum deliberately bypasses the marker and reloads Neo4j. The checksum, not
the changing presigned URL, controls this behavior.

### Tear down after testing or judging

```bash
helm uninstall kgb-mcp --namespace kgb-mcp
kubectl delete pvc --namespace kgb-mcp data-kgb-mcp-neo4j-0
kubectl delete namespace kgb-mcp
```

Then terminate the instance and its attached storage according to the AWS administrator's
procedure. Release the Elastic IP after termination: an unattached Elastic IP continues to bill.
Do not delete the private dump or public artifact until their retention decision is made.
