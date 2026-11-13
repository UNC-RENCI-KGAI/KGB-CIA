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
       prepare_dump.sh --artifacts-only --existing-dump FILE --reports-dir DIR --graph-metadata FILE --output-dir DIR [--graph-version VERSION]

DIR graph-dir contains nodes.jsonl and edges.jsonl. reports-dir contains one or more
transform_report.json files. Output receives kgb.dump and artifacts.tar.gz. In
--artifacts-only mode, an existing kgb.dump is loaded only to recompute MCP artifacts; ORION is
not run and the input dump is copied to the output unchanged.
EOF
}

graph_dir=""
reports_dir=""
graph_metadata=""
output_dir=""
graph_version="development"
artifacts_only=0
existing_dump=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --graph-dir) graph_dir="$2"; shift 2 ;;
    --reports-dir) reports_dir="$2"; shift 2 ;;
    --graph-metadata) graph_metadata="$2"; shift 2 ;;
    --output-dir) output_dir="$2"; shift 2 ;;
    --graph-version) graph_version="$2"; shift 2 ;;
    --artifacts-only) artifacts_only=1; shift ;;
    --existing-dump) existing_dump="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

for required in reports_dir graph_metadata output_dir; do
  if [[ -z "${!required}" ]]; then
    echo "missing --${required//_/-}" >&2
    usage >&2
    exit 2
  fi
done
if [[ "$artifacts_only" -eq 1 ]]; then
  [[ -n "$existing_dump" ]] || { echo "missing --existing-dump for --artifacts-only" >&2; exit 2; }
  [[ -f "$existing_dump" ]] || { echo "missing existing dump: $existing_dump" >&2; exit 2; }
else
  [[ -n "$graph_dir" ]] || { echo "missing --graph-dir" >&2; usage >&2; exit 2; }
  for required_file in "$graph_dir/nodes.jsonl" "$graph_dir/edges.jsonl"; do
    [[ -f "$required_file" ]] || { echo "missing required file: $required_file" >&2; exit 2; }
  done
fi
[[ -f "$graph_metadata" ]] || { echo "missing required file: $graph_metadata" >&2; exit 2; }
[[ -d "$reports_dir" ]] || { echo "missing reports directory: $reports_dir" >&2; exit 2; }
command -v docker >/dev/null || { echo "docker is required" >&2; exit 2; }
command -v uv >/dev/null || { echo "uv is required to run kgbcia-mcp hubs" >&2; exit 2; }

repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
mkdir -p "$output_dir"
output_dir=$(cd "$output_dir" && pwd)
if [[ -n "$graph_dir" ]]; then
  graph_dir=$(cd "$graph_dir" && pwd)
fi
if [[ -n "$existing_dump" ]]; then
  existing_dump=$(cd "$(dirname "$existing_dump")" && pwd)/$(basename "$existing_dump")
fi
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

mkdir "$work_dir/dump-input"
if [[ "$artifacts_only" -eq 1 ]]; then
  printf 'Using existing dump for artifacts-only preparation: %s\n' "$existing_dump"
  cp "$existing_dump" "$work_dir/dump-input/$DATABASE.dump"
else
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
  cp "$work_dir/orion/neo4j.dump" "$work_dir/dump-input/$DATABASE.dump"
fi

printf 'Loading temporary writable Neo4j %s\n' "$NEO4J_IMAGE"
docker volume create "$data_volume" >/dev/null
docker run --rm \
  -v "$data_volume:/data" \
  -v "$work_dir/dump-input:/dumps:ro" \
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

(
  cd "$repo_root"
  NEO4J_URI="bolt://127.0.0.1:$bolt_port" NEO4J_USER=neo4j NEO4J_PASSWORD="$password" \
    uv run python - "$artifacts_dir/graph_metadata.json" "$DATABASE" <<'PY'
import json
import sys
from pathlib import Path

from neo4j import GraphDatabase

ANCHOR = "biolink:NamedThing"
MAX_STRING = 100
MAX_LIST = 3


def truncate(value):
    if isinstance(value, str):
        return value if len(value) <= MAX_STRING else value[: MAX_STRING - 3] + "..."
    if isinstance(value, list):
        result = [truncate(item) for item in value[:MAX_LIST]]
        if len(value) > MAX_LIST:
            result.append("...")
        return result
    if isinstance(value, dict):
        return {str(key): truncate(item) for key, item in value.items()}
    return value


def rows(session, cypher, **params):
    return [dict(record) for record in session.run(cypher, params)]


def quote_name(value):
    return "`" + value.replace("`", "``") + "`"


def node_property_rows(session):
    schema_rows = rows(
        session,
        """
        CALL db.schema.nodeTypeProperties()
        YIELD nodeLabels, propertyName
        WHERE propertyName IS NOT NULL
        RETURN nodeLabels, propertyName
        ORDER BY propertyName
        """,
    )
    pairs = sorted(
        {
            (label, row["propertyName"])
            for row in schema_rows
            for label in row.get("nodeLabels", [])
            if label != ANCHOR
        }
    )
    result = []
    for category, property_name in pairs:
        example = rows(
            session,
            f"""
            MATCH (n:{quote_name(ANCHOR)}:{quote_name(category)})
            WHERE n.{quote_name(property_name)} IS NOT NULL
            RETURN n.{quote_name(property_name)} AS example
            LIMIT 1
            """,
        )
        result.append(
            {
                "category": category,
                "property": property_name,
                "example": truncate(example[0]["example"]) if example else None,
            }
        )
    return result


def edge_property_rows(session):
    schema_rows = rows(
        session,
        """
        CALL db.schema.relTypeProperties()
        YIELD relType, propertyName
        WHERE propertyName IS NOT NULL
        RETURN relType, propertyName
        ORDER BY propertyName, relType
        """,
    )
    property_names = sorted({row["propertyName"] for row in schema_rows})
    result = []
    for property_name in property_names:
        example = rows(
            session,
            f"""
            MATCH ()-[r]->()
            WHERE r.{quote_name(property_name)} IS NOT NULL
            RETURN r.{quote_name(property_name)} AS example
            LIMIT 1
            """,
        )
        result.append(
            {
                "property": property_name,
                "example": truncate(example[0]["example"]) if example else None,
            }
        )
    return result


metadata_path = Path(sys.argv[1])
database = sys.argv[2]
uri = __import__("os").environ["NEO4J_URI"]
user = __import__("os").environ["NEO4J_USER"]
password = __import__("os").environ["NEO4J_PASSWORD"]

metadata = json.loads(metadata_path.read_text())
if not isinstance(metadata, dict):
    raise SystemExit("graph metadata must be a JSON object")

driver = GraphDatabase.driver(uri, auth=(user, password))
try:
    with driver.session(database=database) as session:
        categories = rows(
            session,
            f"""
            MATCH (n:`{ANCHOR}`)
            UNWIND [label IN labels(n) WHERE label <> $anchor] AS category
            RETURN category, count(*) AS count
            ORDER BY count DESC, category
            """,
            anchor=ANCHOR,
        )
        category_order = [
            row["category"] for row in sorted(categories, key=lambda row: (row["count"], row["category"]))
        ]
        stats = {
            "node_count": rows(session, f"MATCH (n:`{ANCHOR}`) RETURN count(n) AS count")[0]["count"],
            "edge_count": rows(session, "MATCH ()-[r]->() RETURN count(r) AS count")[0]["count"],
            "categories": categories,
            "predicates": rows(
                session,
                """
                MATCH ()-[r]->()
                RETURN type(r) AS predicate, count(*) AS count
                ORDER BY count DESC, predicate
                """,
            ),
            "primary_knowledge_sources": rows(
                session,
                """
                MATCH ()-[r]->()
                RETURN r.primary_knowledge_source AS primary_knowledge_source, count(*) AS count
                ORDER BY count DESC, primary_knowledge_source
                """,
            ),
            "flag_counts": rows(
                session,
                """
                MATCH ()-[r]->()
                UNWIND coalesce(r.kgbcia_flags, []) AS flag
                RETURN flag, count(*) AS count
                ORDER BY count DESC, flag
                """,
            ),
        }
        stats["meta_graph"] = rows(
            session,
            f"""
            MATCH (s:`{ANCHOR}`)-[r]->(o:`{ANCHOR}`)
            WITH head([category IN $category_order WHERE category IN labels(s)]) AS subject_category,
                 type(r) AS predicate,
                 head([category IN $category_order WHERE category IN labels(o)]) AS object_category
            WHERE subject_category IS NOT NULL AND object_category IS NOT NULL
            RETURN subject_category, predicate, object_category, count(*) AS count
            ORDER BY count DESC, subject_category, predicate, object_category
            """,
            category_order=category_order,
        )
        stats["properties"] = node_property_rows(session)
        property_names = {}
        for row in stats["properties"]:
            property_names.setdefault(row["category"], []).append(row["property"])
        stats["property_names"] = {
            category: sorted(set(names)) for category, names in sorted(property_names.items())
        }
        stats["edge_properties"] = edge_property_rows(session)
finally:
    driver.close()

metadata["statistics"] = stats
metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
print(f"precomputed meta_graph rows: {len(stats['meta_graph'])}")
PY
)

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
if [[ "$artifacts_only" -eq 1 ]]; then
  cp "$existing_dump" "$output_dir/kgb.dump"
else
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
fi

printf 'kgb dump SHA-256: '
sha256sum "$output_dir/kgb.dump" | awk '{print $1}'
printf 'artifacts SHA-256: '
sha256sum "$output_dir/artifacts.tar.gz" | awk '{print $1}'
printf 'wrote %s and %s\n' "$output_dir/kgb.dump" "$output_dir/artifacts.tar.gz"
