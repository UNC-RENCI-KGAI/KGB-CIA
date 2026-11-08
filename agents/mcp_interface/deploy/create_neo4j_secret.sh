#!/usr/bin/env bash
# Usage: ./create_neo4j_secret.sh <namespace>
set -euo pipefail
NS="${1:?usage: $0 <namespace>}"

read -rsp 'Neo4j admin password: ' ADMIN_PASSWORD; echo
read -rsp 'kgbcia user password: ' USER_PASSWORD; echo

kubectl -n "$NS" create secret generic kgb-mcp-auth \
  --from-literal="neo4j-auth=neo4j/${ADMIN_PASSWORD}" \
  --from-literal="user-password=${USER_PASSWORD}" \
  --dry-run=client -o yaml | kubectl apply -f -

unset ADMIN_PASSWORD USER_PASSWORD
echo "Secret kgb-mcp-auth created or updated in namespace $NS"
