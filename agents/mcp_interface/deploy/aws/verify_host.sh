#!/usr/bin/env bash
# Read-only checks for the prepared AWS k3s host. This script installs or changes nothing.
set -euo pipefail

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

for command in systemctl kubectl helm git uname df; do
  require_command "$command"
done

printf '%s\n' 'Checking prepared AWS k3s host (no changes will be made)...'
systemctl is-active --quiet k3s || fail 'k3s service is not active'
printf '%-28s %s\n' 'k3s service:' 'active'

architecture="$(uname -m)"
[ "$architecture" = 'aarch64' ] || fail "expected arm64/aarch64, found $architecture"
printf '%-28s %s\n' 'architecture:' "$architecture"

ready_nodes="$(kubectl get nodes --no-headers | awk '$2 == "Ready" || $2 ~ /^Ready,/ { count++ } END { print count + 0 }')"
[ "$ready_nodes" -ge 1 ] || fail 'no Ready Kubernetes nodes found'
printf '%-28s %s\n' 'Ready Kubernetes nodes:' "$ready_nodes"
kubectl get nodes -o wide

storage_class="$(kubectl get storageclass local-path -o jsonpath='{.metadata.name}' 2>/dev/null || true)"
[ "$storage_class" = 'local-path' ] || fail 'local-path storage class is not available'
default_storage_class="$(kubectl get storageclass local-path \
  -o jsonpath='{.metadata.annotations.storageclass\.kubernetes\.io/is-default-class}')"
[ "$default_storage_class" = 'true' ] || fail 'local-path is not the default storage class'
printf '%-28s %s\n' 'default storage class:' "$storage_class"

helm_version="$(helm version --template '{{ .Version }}')"
case "$helm_version" in
  v4.*) ;;
  *) fail "Helm 4 is required, found $helm_version" ;;
esac
printf '%-28s %s\n' 'Helm version:' "$helm_version"
printf '%-28s %s\n' 'git version:' "$(git --version)"

printf '%s\n' 'Root and k3s local-path free space:'
df -h / /var/lib/rancher/k3s/storage 2>/dev/null || df -h /

cat <<'EOF'
Host checks passed.

Manual rebuild prerequisites (documented only; this script does not perform them):
- Amazon Linux 2023 on arm64, accessed through AWS Systems Manager Session Manager.
- k3s with Traefik disabled only; ServiceLB enabled; local-path as default storage.
- Helm 4 configured for the k3s kubeconfig, plus git.
EOF
