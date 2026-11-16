#!/usr/bin/env bash
# Run the MCP server's Neo4j smoke test against a toy graph in a throwaway Neo4j container.
#
# 1. Start Neo4j writable and load the toy graph (tests/neo4j_toy_graph.py).
# 2. Restart it read-only, the way the deployment runs it (helm chart's neo4j-statefulset.yaml).
# 3. Run test_integration_smoke_against_configured_neo4j with NEO4J_URI and KGB_MCP_ARTIFACTS set.
#
# Needs Docker. Used by `make test-neo4j` and CI.
set -euo pipefail

IMAGE="neo4j:5.26.31-community"   # the version prepare_dump.sh pins
NAME="kgb-neo4j-smoke-$$"
VOLUME="$NAME-data"
PORT="${KGB_SMOKE_BOLT_PORT:-17687}"
URI="bolt://127.0.0.1:$PORT"
WORK="$(mktemp -d)"

cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker volume rm "$VOLUME" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

start() {  # $1: true for read-only
  docker run -d --name "$NAME" -p "127.0.0.1:$PORT:7687" -v "$VOLUME:/data" \
    -e NEO4J_AUTH=none \
    -e NEO4J_server_databases_default__to__read__only="$1" \
    "$IMAGE" >/dev/null
  for _ in $(seq 1 90); do
    if docker exec "$NAME" cypher-shell -a bolt://localhost:7687 'RETURN 1' >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  docker logs "$NAME" | tail -40
  echo "Neo4j did not start" >&2
  exit 1
}

cd "$(dirname "$0")/.."

start false
uv run python tests/neo4j_toy_graph.py --uri "$URI" --artifacts "$WORK/artifacts"
# A clean shutdown: a read-only database can't run recovery on a killed one.
docker stop -t 120 "$NAME" >/dev/null
docker rm "$NAME" >/dev/null

start true
NEO4J_URI="$URI" KGB_MCP_ARTIFACTS="$WORK/artifacts" \
  uv run pytest -q -rs tests/test_mcp_interface.py -k integration_smoke "$@"
