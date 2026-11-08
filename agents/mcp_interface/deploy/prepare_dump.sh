#!/usr/bin/env bash
# Turn ORION KGX output into a Neo4j 5.26.31 dump and MCP artifact tarball.
# Requires Docker, uv, and the repository checkout that supplies kgbcia-mcp.
set -euo pipefail

ORION_IMAGE="ghcr.io/robokopu24/orion@sha256:913006ca778f3279e1563078bb25801714db62778e3e35d6e33062eb057b6994"
NEO4J_IMAGE="neo4j:5.26.31-community"
DATABASE="neo4j"

usage() {
  cat <<'EOF'
Usage: prepare_dump.sh --graph-dir DIR --reports-dir DIR --graph-metadata FILE --output-dir DIR [--graph-version VERSION]

DIR graph-dir contains nodes.jsonl and edges.jsonl. reports-dir contains one or more
transform_report.json files. Output receives kgb.dump and artifacts.tar.gz.
EOF
}

graph_dir=""
reports_dir=""
graph_metadata=""
output_dir=""
graph_version="development"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --graph-dir) graph_dir="$2"; shift 2 ;;
    --reports-dir) reports_dir="$2"; shift 2 ;;
    --graph-metadata) graph_metadata="$2"; shift 2 ;;
    --output-dir) output_dir="$2"; shift 2 ;;
    --graph-version) graph_version="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

for required in graph_dir reports_dir graph_metadata output_dir; do
  if [[ -z "${!required}" ]]; then
    echo "missing --${required//_/-}" >&2
    usage >&2
    exit 2
  fi
done
for required_file in "$graph_dir/nodes.jsonl" "$graph_dir/edges.jsonl" "$graph_metadata"; do
  [[ -f "$required_file" ]] || { echo "missing required file: $required_file" >&2; exit 2; }
done
[[ -d "$reports_dir" ]] || { echo "missing reports directory: $reports_dir" >&2; exit 2; }
command -v docker >/dev/null || { echo "docker is required" >&2; exit 2; }
command -v uv >/dev/null || { echo "uv is required to run kgbcia-mcp hubs" >&2; exit 2; }

repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
mkdir -p "$output_dir"
output_dir=$(cd "$output_dir" && pwd)
graph_dir=$(cd "$graph_dir" && pwd)
reports_dir=$(cd "$reports_dir" && pwd)
graph_metadata=$(cd "$(dirname "$graph_metadata")" && pwd)/$(basename "$graph_metadata")
work_dir=$(mktemp -d)
data_volume="kgbcia-dump-data-$(date +%s)-$$"
container_name="kgbcia-dump-$(date +%s)-$$"
dump_container="${container_name}-export"
password="kgbcia-$(openssl rand -hex 18)"

cleanup() {
  docker rm -f "$container_name" >/dev/null 2>&1 || true
  docker rm -f "$dump_container" >/dev/null 2>&1 || true
  docker volume rm "$data_volume" >/dev/null 2>&1 || true
  rm -rf "$work_dir"
}
trap cleanup EXIT

printf 'Building ORION dump with %s\n' "$ORION_IMAGE"
# ORION's third argument is an output *directory*: it writes nodes.temp_csv,
# edges.temp_csv, import.report, and neo4j.dump under that directory.
mkdir "$work_dir/orion"
docker run --rm \
  -v "$graph_dir:/input:ro" \
  -v "$work_dir:/work" \
  "$ORION_IMAGE" \
  orion-neo4j-dump /input/nodes.jsonl /input/edges.jsonl /work/orion
[[ -f "$work_dir/orion/neo4j.dump" ]] || {
  echo "ORION did not produce /work/orion/neo4j.dump" >&2; exit 1;
}

printf 'Loading temporary writable Neo4j %s\n' "$NEO4J_IMAGE"
docker volume create "$data_volume" >/dev/null
docker run --rm \
  -v "$data_volume:/data" \
  -v "$work_dir/orion:/dumps:ro" \
  "$NEO4J_IMAGE" \
  neo4j-admin database load "$DATABASE" --from-path=/dumps --overwrite-destination=true

docker run -d --name "$container_name" \
  -e "NEO4J_AUTH=neo4j/$password" \
  -e NEO4J_server_databases_default__to__read__only=false \
  -v "$data_volume:/data" \
  -p 127.0.0.1::7687 \
  "$NEO4J_IMAGE" >/dev/null
bolt_port=$(docker port "$container_name" 7687/tcp | sed 's/.*://')
for _ in $(seq 1 60); do
  if docker exec "$container_name" cypher-shell -u neo4j -p "$password" -d "$DATABASE" \
    'RETURN 1' >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec "$container_name" cypher-shell -u neo4j -p "$password" -d "$DATABASE" \
  'RETURN 1' >/dev/null || { echo "temporary Neo4j did not start" >&2; exit 1; }

docker exec -i "$container_name" cypher-shell -u neo4j -p "$password" -d "$DATABASE" \
  < "$script_dir/extra_indexes.cypher"

# Neo4j range relationship indexes are per type. Generate safe, idempotent DDL here
# rather than relying on APOC (which Community images do not include).
while IFS= read -r relationship_type; do
  [[ -n "$relationship_type" ]] || continue
  index_suffix=$(printf '%s' "$relationship_type" | tr -cs '[:alnum:]' '_' | tr '[:upper:]' '[:lower:]')
  index_suffix="${index_suffix#_}"
  index_suffix="${index_suffix%_}"
  escaped_type=${relationship_type//\`/\`\`}
  printf 'CREATE RANGE INDEX kgbcia_relationship_id_%s IF NOT EXISTS FOR ()-[r:`%s`]-() ON (r.id);\n' \
    "$index_suffix" "$escaped_type" \
    | docker exec -i "$container_name" cypher-shell -u neo4j -p "$password" -d "$DATABASE"
done < <(docker exec "$container_name" cypher-shell --format plain -u neo4j -p "$password" -d "$DATABASE" \
  'CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType ORDER BY relationshipType' \
  | sed '1d')
docker exec "$container_name" cypher-shell -u neo4j -p "$password" -d "$DATABASE" 'CALL db.awaitIndexes()'

artifacts_dir="$work_dir/artifacts"
mkdir -p "$artifacts_dir/reports"
cp "$graph_metadata" "$artifacts_dir/graph_metadata.json"
while IFS= read -r report; do
  relative=${report#"$reports_dir"/}
  destination="$artifacts_dir/reports/$relative"
  mkdir -p "$(dirname "$destination")"
  cp "$report" "$destination"
done < <(find "$reports_dir" -type f -name transform_report.json | sort)
[[ -n "$(find "$artifacts_dir/reports" -type f -name transform_report.json -print -quit)" ]] || {
  echo "no transform_report.json found under $reports_dir" >&2; exit 1;
}
(
  cd "$repo_root"
  NEO4J_URI="bolt://127.0.0.1:$bolt_port" NEO4J_USER=neo4j NEO4J_PASSWORD="$password" \
    uv run kgbcia-mcp hubs --top 100 --output "$artifacts_dir"
)

# Required first-load evidence; retain it beside the release artifacts for later README work.
docker exec "$container_name" cypher-shell --format plain -u neo4j -p "$password" -d "$DATABASE" \
  'SHOW INDEXES' > "$artifacts_dir/neo4j_show_indexes.txt"
docker exec "$container_name" cypher-shell --format plain -u neo4j -p "$password" -d "$DATABASE" \
  'CALL db.schema.nodeTypeProperties()' > "$artifacts_dir/neo4j_node_type_properties.txt"
docker exec "$container_name" cypher-shell --format plain -u neo4j -p "$password" -d "$DATABASE" \
  'CALL db.schema.relTypeProperties()' > "$artifacts_dir/neo4j_rel_type_properties.txt"

python - "$artifacts_dir" "$graph_version" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
files = [str(path.relative_to(root)) for path in sorted(root.rglob("*")) if path.is_file()]
(root / "manifest.json").write_text(
    json.dumps({"graph_version": sys.argv[2], "files": files}, indent=2) + "\n"
)
PY

tar -C "$artifacts_dir" -czf "$output_dir/artifacts.tar.gz" .
# neo4j-admin requires its destination directory to exist, and it must be
# writable by the Neo4j service user that owns the data volume.
docker exec "$container_name" mkdir -p /data/dump-export
docker stop "$container_name" >/dev/null
# The image entrypoint deliberately switches to its internal neo4j user, even
# when Docker is asked to start as root. Dump into the Neo4j-owned data volume,
# then use docker cp (which writes as the invoking host user) to publish it.
docker create --name "$dump_container" \
  -v "$data_volume:/data" \
  "$NEO4J_IMAGE" \
  neo4j-admin database dump "$DATABASE" --to-path=/data/dump-export >/dev/null
docker start -a "$dump_container"
docker cp "$dump_container:/data/dump-export/$DATABASE.dump" "$output_dir/kgb.dump"
docker rm "$dump_container" >/dev/null

printf 'kgb dump SHA-256: '
sha256sum "$output_dir/kgb.dump" | awk '{print $1}'
printf 'artifacts SHA-256: '
sha256sum "$output_dir/artifacts.tar.gz" | awk '{print $1}'
printf 'wrote %s and %s\n' "$output_dir/kgb.dump" "$output_dir/artifacts.tar.gz"
