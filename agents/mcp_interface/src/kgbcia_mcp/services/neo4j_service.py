"""Neo4j query service and result serialization. This module has no MCP imports."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from neo4j import GraphDatabase
from neo4j.exceptions import Neo4jError
from neo4j.graph import Node, Path, Relationship
from neo4j.spatial import Point
from neo4j.time import Date, DateTime, Duration, Time

from ..lineage import LineageIndex

# Confirmed on the local-k8s Neo4j 5.26.31 load (November 2026).
NAMED_THING_LABEL = "biolink:NamedThing"
NODE_ID_PROPERTY = "id"
EDGE_ID_PROPERTY = "id"
FULLTEXT_INDEX_NAME = "kgbcia_node_fulltext"
# Neo4j 5.26 accepts the equivalent-identifiers StringArray property in this index.
FULLTEXT_INDEX_PROPERTIES = ("name", "synonym", "equivalent_identifiers")
# Generated per relationship type; SHOW INDEXES confirmed these names and id properties.
RELATIONSHIP_ID_INDEX_PREFIX = "kgbcia_relationship_id_"

_MAX_LIMIT = 500
_MAX_FETCH = _MAX_LIMIT + 1
_FORBIDDEN_CYPHER = re.compile(
    r"\b(CREATE|DELETE|DETACH|DROP|FOREACH|LOAD\s+CSV|MERGE|REMOVE|RENAME|SET|START)\b",
    re.IGNORECASE,
)


class Neo4jNotConfigured(RuntimeError):
    """Raised on a tool call when connection settings were not supplied."""


class Neo4jUnavailable(RuntimeError):
    """Raised when a configured Neo4j read fails with a classified cause."""

    def __init__(self, message: str, *, cause: str, detail: str | None = None) -> None:
        super().__init__(message)
        self.cause = cause
        self.detail = detail or message


@dataclass(frozen=True)
class ReadResult:
    rows: tuple[dict[str, Any], ...]
    truncated: bool


class Neo4jService:
    """Own the driver and expose bounded, read-only graph operations as plain dictionaries."""

    def __init__(
        self,
        uri: str | None,
        user: str | None,
        password: str | None,
        database: str | None = None,
        *,
        driver: Any | None = None,
        lineage: LineageIndex | None = None,
        graph_metadata: Mapping[str, Any] | None = None,
        hubs: Iterable[str] = (),
    ) -> None:
        self._driver = driver
        self._uri = uri
        self._user = user
        self._password = password
        self.database = database
        self.lineage = lineage or LineageIndex()
        self.graph_metadata = dict(graph_metadata or {})
        self.hubs = tuple(hubs)
        self._cache: dict[str, Any] = {}

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()

    def _get_driver(self) -> Any:
        """Construct the official driver only when the first tool executes a query."""

        if self._driver is not None:
            return self._driver
        if not self._uri:
            raise Neo4jNotConfigured("Neo4j not configured")
        if bool(self._user) != bool(self._password):
            raise Neo4jNotConfigured("NEO4J_USER and NEO4J_PASSWORD must be set together")
        try:
            if self._user:
                self._driver = GraphDatabase.driver(
                    self._uri,
                    auth=(self._user, self._password),
                )
            else:
                self._driver = GraphDatabase.driver(self._uri)
        except (Neo4jError, OSError, ValueError) as exc:
            raise _classify_neo4j_failure(exc) from exc
        return self._driver

    def execute_read(
        self,
        cypher: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        timeout: float = 30.0,
        row_limit: int = _MAX_LIMIT,
    ) -> ReadResult:
        """Run through ``session.execute_read`` and cap materialized records."""

        if row_limit < 1 or row_limit > _MAX_FETCH:
            raise ValueError(f"row_limit must be between 1 and {_MAX_FETCH}")
        query_parameters = dict(parameters or {})

        def collect(transaction: Any) -> ReadResult:
            # ManagedTransaction.run intentionally rejects neo4j.Query objects.
            # Server-side transaction limits provide the timeout for managed reads.
            records = transaction.run(cypher, query_parameters)
            rows: list[dict[str, Any]] = []
            truncated = False
            for record in records:
                if len(rows) == row_limit:
                    truncated = True
                    break
                rows.append({key: serialize_neo4j_value(value) for key, value in record.items()})
            return ReadResult(tuple(rows), truncated)

        return self._read_transaction(collect)

    def cache_get(self, name: str) -> Any | None:
        return self._cache.get(name)

    def cache_set(self, name: str, value: Any) -> None:
        self._cache[name] = value

    def _execute_pages(
        self, cypher: str, parameters: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Read a stable ordered schema query in bounded pages until exhausted."""

        rows: list[dict[str, Any]] = []
        while True:
            page_parameters = {
                **dict(parameters or {}),
                "offset": len(rows),
                "page_size": _MAX_LIMIT,
            }
            page = self.execute_read(cypher, page_parameters, row_limit=_MAX_LIMIT).rows
            rows.extend(page)
            if len(page) < _MAX_LIMIT:
                return rows

    def get_graph_info(self, *, refresh: bool = False) -> dict[str, Any]:
        if not refresh and (cached := self.cache_get("graph_info")) is not None:
            return cached
        stats = self._graph_statistics()
        if stats is None:
            return self._missing_statistics("get_graph_info")
        info = {
            "node_count": stats.get("node_count", 0),
            "edge_count": stats.get("edge_count", 0),
            "categories": list(stats.get("categories", [])),
            "predicates": list(stats.get("predicates", [])),
            "knowledge_sources": [
                row.get("primary_knowledge_source")
                for row in stats.get("primary_knowledge_sources", [])
                if row.get("primary_knowledge_source")
            ],
            "primary_knowledge_sources": list(stats.get("primary_knowledge_sources", [])),
            "flag_counts": list(stats.get("flag_counts", [])),
            "graph_metadata": self.graph_metadata,
        }
        self.cache_set("graph_info", info)
        return info

    def get_schema(self, *, refresh: bool = False) -> dict[str, Any]:
        if not refresh and (cached := self.cache_get("schema")) is not None:
            return cached
        stats = self._graph_statistics()
        if stats is None:
            return self._missing_statistics("get_schema")
        property_names = {
            str(category): sorted({str(name) for name in names})
            for category, names in dict(stats.get("property_names", {})).items()
        }
        if not property_names:
            for row in stats.get("properties", []):
                category = row.get("category")
                name = row.get("property")
                if category and name:
                    property_names.setdefault(str(category), []).append(str(name))
            property_names = {
                category: sorted(set(names)) for category, names in property_names.items()
            }
        schema = {
            "meta_graph": list(stats.get("meta_graph", [])),
            "meta_graph_truncated": False,
            "properties": list(stats.get("properties", [])),
            "properties_truncated": False,
            "edge_properties": list(stats.get("edge_properties", [])),
            "categories": sorted(
                row["category"] for row in stats.get("categories", []) if row.get("category")
            ),
            "predicates": sorted(
                row["predicate"] for row in stats.get("predicates", []) if row.get("predicate")
            ),
            "property_names": property_names,
            "flag_values": sorted(
                row["flag"] for row in stats.get("flag_counts", []) if row.get("flag")
            ),
        }
        self.cache_set("schema", schema)
        return schema

    def search_nodes(
        self, query: str, category: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        _validate_limit(limit)
        if category:
            self._validate_name("category", category)
        label_filter = "AND $category IN labels(n)" if category else ""
        exact_query = f"""
        MATCH (n:`{NAMED_THING_LABEL}`)
        WHERE (n.id = $query OR ANY(x IN coalesce(n.equivalent_identifiers, [])
              WHERE x = $query)) {label_filter}
        RETURN n LIMIT $limit
        """
        params = {"query": query, "category": category, "limit": limit + 1}
        exact = self.execute_read(exact_query, params, row_limit=limit + 1).rows
        rows = [{**row["n"], "match": "identifier", "exact_match": True} for row in exact]
        if not rows:
            fulltext_query = """
            CALL db.index.fulltext.queryNodes($index, $query, {limit: $limit})
            YIELD node AS n, score
            WHERE ($category IS NULL OR $category IN labels(n))
            RETURN n, score,
                   (toLower(coalesce(n.name, '')) = toLower($query) OR
                    any(s IN coalesce(n.synonym, []) WHERE toLower(s) = toLower($query)))
                   AS exact_match
            ORDER BY score DESC LIMIT $limit
            """
            found = self.execute_read(
                fulltext_query,
                {**params, "index": FULLTEXT_INDEX_NAME},
                row_limit=limit + 1,
            ).rows
            rows = [
                {
                    **row["n"],
                    "score": row["score"],
                    "match": "exact_name" if row["exact_match"] else "fulltext",
                    "exact_match": row["exact_match"],
                }
                for row in found
            ]
        return _list_response(rows, limit, not rows, "Try an exact CURIE or a broader name.")

    def get_node(self, node_id: str) -> dict[str, Any]:
        resolved = self._resolve_node(node_id)
        if resolved["status"] != "found":
            return resolved
        return {"node": resolved["node"]}

    def summarize_node_edges(self, node_id: str) -> dict[str, Any]:
        resolved = self._resolve_node(node_id)
        if resolved["status"] != "found":
            return resolved
        query = f"""
        MATCH (n:`{NAMED_THING_LABEL}` {{id: $id}})-[r]-(neighbor:`{NAMED_THING_LABEL}`)
        UNWIND [x IN labels(neighbor) WHERE x <> $anchor] AS neighbor_category
        WITH CASE WHEN startNode(r) = n THEN 'outgoing' ELSE 'incoming' END AS direction,
             type(r) AS predicate, neighbor_category, count(*) AS count
        RETURN direction, predicate, neighbor_category, count
        ORDER BY count DESC LIMIT $limit
        """
        rows = self.execute_read(
            query,
            {"id": resolved["node"]["id"], "anchor": NAMED_THING_LABEL, "limit": _MAX_LIMIT},
        ).rows
        return _list_response(list(rows), _MAX_LIMIT)

    def get_node_edges(
        self,
        node_id: str,
        direction: str = "both",
        predicate: str | None = None,
        neighbor_category: str | None = None,
        neighbor_where: Mapping[str, Any] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        _validate_page(limit, offset)
        if direction not in {"incoming", "outgoing", "both"}:
            raise ValueError("direction must be incoming, outgoing, or both")
        if predicate:
            self._validate_name("predicate", predicate)
        if neighbor_category:
            self._validate_name("category", neighbor_category)
        property_name, property_value = self._validate_where(neighbor_category, neighbor_where)
        resolved = self._resolve_node(node_id)
        if resolved["status"] != "found":
            return resolved
        arrows = {
            "incoming": ("<-[r]-", "incoming"),
            "outgoing": ("-[r]->", "outgoing"),
            "both": ("-[r]-", None),
        }
        pattern, fixed_direction = arrows[direction]
        filters = []
        if predicate:
            filters.append("type(r) = $predicate")
        if neighbor_category:
            filters.append("$neighbor_category IN labels(neighbor)")
        if property_name:
            filters.append(f"neighbor.`{property_name}` = $neighbor_value")
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        query = f"""
        MATCH (n:`{NAMED_THING_LABEL}` {{id: $id}}){pattern}
              (neighbor:`{NAMED_THING_LABEL}`)
        {where}
        WITH n, r, neighbor,
             CASE WHEN startNode(r) = n THEN 'outgoing' ELSE 'incoming' END AS direction
        RETURN neighbor, r AS edge, direction,
               size(coalesce(r.publications, [])) AS publication_count
        SKIP $offset LIMIT $fetch
        """
        params = {
            "id": resolved["node"]["id"],
            "predicate": predicate,
            "neighbor_category": neighbor_category,
            "neighbor_value": property_value,
            "offset": offset,
            "fetch": limit + 1,
        }
        rows = list(self.execute_read(query, params, row_limit=limit + 1).rows)
        for row in rows:
            if fixed_direction:
                row["direction"] = fixed_direction
        return _paged_response(rows, limit, offset)

    def get_edges_between(
        self,
        source_id: str,
        target_id: str,
        include_lineage: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        _validate_page(limit, offset)
        source = self._resolve_node(source_id)
        if source["status"] != "found":
            return source
        target = self._resolve_node(target_id)
        if target["status"] != "found":
            return target
        query = f"""
        MATCH (a:`{NAMED_THING_LABEL}` {{id: $source_id}})-[r]-(b:`{NAMED_THING_LABEL}`
              {{id: $target_id}})
        RETURN r AS edge,
               CASE WHEN startNode(r) = a THEN 'outgoing' ELSE 'incoming' END AS direction,
               size(coalesce(r.publications, [])) AS publication_count
        SKIP $offset LIMIT $limit
        """
        rows = list(
            self.execute_read(
                query,
                {
                    "source_id": source["node"]["id"],
                    "target_id": target["node"]["id"],
                    "offset": offset,
                    "limit": limit + 1,
                },
                row_limit=limit + 1,
            ).rows
        )
        if include_lineage:
            for row in rows:
                edge = row["edge"]
                subject_categories = (
                    source["node"]["categories"]
                    if edge.get("subject") == source["node"]["id"]
                    else target["node"]["categories"]
                )
                object_categories = (
                    target["node"]["categories"]
                    if edge.get("object") == target["node"]["id"]
                    else source["node"]["categories"]
                )
                row["lineage"] = self.lineage.resolve_source_records(
                    edge, subject_categories, object_categories
                )
        response = _paged_response(rows, limit, offset)
        if not response["results"]:
            response["hint"] = "No direct edge connects these nodes."
        return response

    def find_paths(
        self,
        source_id: str,
        target_id: str,
        max_hops: int = 3,
        predicates: list[str] | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        if max_hops < 1 or max_hops > 4:
            raise ValueError("max_hops must be between 1 and 4")
        _validate_limit(limit)
        self._validate_predicates(predicates)
        source = self._resolve_node(source_id)
        if source["status"] != "found":
            return source
        target = self._resolve_node(target_id)
        if target["status"] != "found":
            return target
        params = {
            "source_id": source["node"]["id"],
            "target_id": target["node"]["id"],
            "predicates": predicates or [],
            "hubs": list(self.hubs),
            "limit": limit + 1,
        }
        for depth in range(1, max_hops + 1):
            query = f"""
            MATCH p=(a:`{NAMED_THING_LABEL}` {{id: $source_id}})-[*{depth}]-(b:`{NAMED_THING_LABEL}`
                  {{id: $target_id}})
            WHERE (size($predicates) = 0 OR
                   all(r IN relationships(p) WHERE type(r) IN $predicates))
              AND all(n IN nodes(p)[1..-1] WHERE NOT n.id IN $hubs)
            RETURN p AS path LIMIT $limit
            """
            rows = [
                row["path"] for row in self.execute_read(query, params, row_limit=limit + 1).rows
            ]
            if rows:
                response = _list_response(rows, limit)
                response["hops"] = depth
                return response
        return _list_response([], limit, True, f"No path found within {max_hops} hops.")

    def find_connected_nodes(
        self,
        source_id: str,
        target_category: str,
        max_hops: int = 2,
        predicates: list[str] | None = None,
        target_where: Mapping[str, Any] | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        if max_hops < 1 or max_hops > 3:
            raise ValueError("max_hops must be between 1 and 3")
        _validate_limit(limit)
        self._validate_name("category", target_category)
        self._validate_predicates(predicates)
        property_name, property_value = self._validate_where(target_category, target_where)
        source = self._resolve_node(source_id)
        if source["status"] != "found":
            return source
        property_filter = f"AND target.`{property_name}` = $target_value" if property_name else ""
        params = {
            "source_id": source["node"]["id"],
            "target_category": target_category,
            "target_value": property_value,
            "predicates": predicates or [],
            "hubs": list(self.hubs),
            "limit": limit + 1,
        }
        for depth in range(1, max_hops + 1):
            query = f"""
            MATCH p=(source:`{NAMED_THING_LABEL}` {{id: $source_id}})-[*{depth}]-
                    (target:`{NAMED_THING_LABEL}`)
            WHERE $target_category IN labels(target) {property_filter}
              AND (size($predicates) = 0 OR
                   all(r IN relationships(p) WHERE type(r) IN $predicates))
              AND all(n IN nodes(p)[1..-1] WHERE NOT n.id IN $hubs)
            RETURN target, p AS path LIMIT $limit
            """
            rows = list(self.execute_read(query, params, row_limit=limit + 1).rows)
            if rows:
                response = _list_response(rows, limit)
                response["hops"] = depth
                return response
        return _list_response(
            [], limit, True, f"No {target_category} node found within {max_hops} hops."
        )

    def compute_hubs(self, top: int) -> list[dict[str, Any]]:
        """Return the highest-degree node IDs for dump-time path-search artifacts."""

        if top < 1 or top > 100_000:
            raise ValueError("top must be between 1 and 100000")
        query = f"""
        MATCH (n:`{NAMED_THING_LABEL}`)-[r]-()
        RETURN n.id AS id, count(r) AS degree
        ORDER BY degree DESC, id SKIP $offset LIMIT $limit
        """
        hubs: list[dict[str, Any]] = []
        while len(hubs) < top:
            chunk = min(_MAX_LIMIT, top - len(hubs))
            rows = self.execute_read(
                query, {"offset": len(hubs), "limit": chunk}, row_limit=chunk
            ).rows
            hubs.extend(rows)
            if len(rows) < chunk:
                break
        return hubs

    def explain_edge(self, edge_id: str) -> dict[str, Any]:
        schema = self.get_schema()
        if "hint" in schema:
            return schema
        predicates = schema["predicates"]
        if not predicates:
            return {"hint": "The cached schema has no relationship types to search."}
        branches = []
        for predicate in predicates:
            relationship_type = predicate.replace("`", "``")
            branches.append(
                f"MATCH (subject:`{NAMED_THING_LABEL}`)-"
                f"[r:`{relationship_type}` {{id: $edge_id}}]->"
                f"(object:`{NAMED_THING_LABEL}`) "
                "RETURN r AS edge, labels(subject) AS subject_categories, "
                "labels(object) AS object_categories"
            )
        query = "CALL { " + " UNION ALL ".join(branches) + " } RETURN * LIMIT 2"
        rows = self.execute_read(query, {"edge_id": edge_id}, row_limit=2).rows
        if not rows:
            return {"hint": "No edge has this ID. Copy edge_id verbatim from an edge result."}
        if len(rows) > 1:
            return {"hint": "The edge ID is not unique in Neo4j; confirm the dump indexes."}
        row = rows[0]
        return self.lineage.resolve_source_records(
            row["edge"], row["subject_categories"], row["object_categories"]
        )

    def query_cypher(
        self,
        cypher: str,
        parameters: Mapping[str, Any] | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        _validate_limit(limit)
        statement = cypher.strip().removesuffix(";").strip()
        if not statement or ";" in statement:
            raise ValueError("query_cypher accepts exactly one Cypher statement")
        if _FORBIDDEN_CYPHER.search(_strip_literals_and_comments(statement)):
            raise ValueError("write and administrative Cypher is not allowed")
        supplied = dict(parameters or {})
        if "kgb_mcp_limit" in supplied:
            raise ValueError("parameter name kgb_mcp_limit is reserved")
        wrapped = f"CALL {{ {statement} }} RETURN * LIMIT $kgb_mcp_limit"
        supplied["kgb_mcp_limit"] = limit + 1
        self._explain_read_only(wrapped, supplied)
        rows = list(
            self.execute_read(
                wrapped,
                supplied,
                timeout=15.0,
                row_limit=limit + 1,
            ).rows
        )
        return _list_response(rows, limit)

    def _graph_statistics(self) -> dict[str, Any] | None:
        stats = self.graph_metadata.get("statistics")
        if isinstance(stats, Mapping):
            return dict(stats)
        return None

    def _missing_statistics(self, tool_name: str) -> dict[str, Any]:
        return {
            "hint": (
                f"{tool_name} requires precomputed graph statistics in graph_metadata.json. "
                "Rebuild the MCP artifacts with deploy/prepare_dump.sh; whole-graph aggregate "
                "queries are intentionally not run against the deployed Neo4j service."
            ),
            "graph_metadata": self.graph_metadata,
        }

    def _resolve_node(self, value: str) -> dict[str, Any]:
        response = self.search_nodes(value, limit=10)
        rows = response["results"]
        exact = [node for node in rows if node.get("exact_match")]
        if len(exact) == 1:
            return {"status": "found", "node": exact[0]}
        if not rows:
            return {
                "status": "not_found",
                "hint": "No node matched. Call search_nodes with an exact CURIE or broader name.",
                "candidates": [],
            }
        return {
            "status": "ambiguous",
            "hint": "The name is ambiguous. Choose a candidate ID and call the tool again.",
            "candidates": rows,
        }

    def _validate_name(self, kind: str, value: str) -> None:
        schema = self.get_schema()
        if "hint" in schema:
            raise ValueError(schema["hint"])
        valid = schema["predicates" if kind == "predicate" else "categories"]
        if value not in valid:
            raise ValueError(f"unknown {kind} {value!r}; valid values: {', '.join(valid)}")

    def _validate_predicates(self, predicates: list[str] | None) -> None:
        for predicate in predicates or []:
            self._validate_name("predicate", predicate)

    def _validate_where(
        self, category: str | None, where: Mapping[str, Any] | None
    ) -> tuple[str | None, Any]:
        if not where:
            return None, None
        if len(where) != 1:
            raise ValueError("property filters must contain exactly one equality")
        if not category:
            raise ValueError("a category is required when filtering by property")
        property_name, value = next(iter(where.items()))
        valid = self.get_schema()["property_names"].get(category, [])
        if property_name not in valid:
            raise ValueError(
                f"unknown property {property_name!r} for {category}; valid values: "
                f"{', '.join(valid)}"
            )
        return property_name, value

    def _explain_read_only(self, cypher: str, parameters: Mapping[str, Any]) -> None:
        def inspect_plan(transaction: Any) -> None:
            summary = transaction.run(f"EXPLAIN {cypher}", dict(parameters)).consume()
            query_type = str(summary.query_type).lower()
            if query_type not in {"r", "read_only", "querytype.read_only"}:
                raise ValueError(
                    f"only read-only Cypher is allowed (planned as {summary.query_type})"
                )

        self._read_transaction(inspect_plan)

    def _read_transaction(self, callback: Any) -> Any:
        session_kwargs = {"database": self.database} if self.database else {}
        driver = self._get_driver()
        try:
            with driver.session(**session_kwargs) as session:
                return session.execute_read(callback)
        except Neo4jError as exc:
            raise _classify_neo4j_failure(exc) from exc
        except OSError as exc:
            raise _classify_neo4j_failure(exc) from exc


def serialize_neo4j_value(value: Any) -> Any:
    """Recursively convert Neo4j values into JSON-serializable plain Python data."""

    if isinstance(value, Node):
        properties = {key: serialize_neo4j_value(item) for key, item in dict(value).items()}
        properties["categories"] = sorted(value.labels)
        return properties
    if isinstance(value, Relationship):
        properties = {key: serialize_neo4j_value(item) for key, item in dict(value).items()}
        edge_id = properties.pop(EDGE_ID_PROPERTY, None)
        publications = properties.get("publications", [])
        publication_count = len(publications) if isinstance(publications, list) else 0
        result = {
            "edge_id": edge_id,
            "predicate": value.type,
            "publication_count": publication_count,
            **properties,
        }
        if value.start_node is not None:
            result["subject"] = value.start_node.get(NODE_ID_PROPERTY)
        if value.end_node is not None:
            result["object"] = value.end_node.get(NODE_ID_PROPERTY)
        return result
    if isinstance(value, Path):
        return {
            "nodes": [serialize_neo4j_value(node) for node in value.nodes],
            "edges": [serialize_neo4j_value(edge) for edge in value.relationships],
        }
    if isinstance(value, (Date, DateTime, Duration, Time)):
        return value.iso_format()
    if isinstance(value, Point):
        result = {"srid": value.srid, "x": value.x, "y": value.y}
        if hasattr(value, "z"):
            result["z"] = value.z
        return result
    if isinstance(value, Mapping):
        return {str(key): serialize_neo4j_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [serialize_neo4j_value(item) for item in value]
    return value


def _validate_limit(limit: int) -> None:
    if limit < 1 or limit > _MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {_MAX_LIMIT}")


def _validate_page(limit: int, offset: int) -> None:
    _validate_limit(limit)
    if offset < 0:
        raise ValueError("offset must be non-negative")


def _list_response(
    rows: list[Any], limit: int, empty_hint: bool = False, hint: str | None = None
) -> dict[str, Any]:
    truncated = len(rows) > limit
    results = rows[:limit]
    response: dict[str, Any] = {
        "results": results,
        "total": len(results) + (1 if truncated else 0),
        "truncated": truncated,
    }
    if truncated:
        response["total_is_lower_bound"] = True
    if empty_hint and not results and hint:
        response["hint"] = hint
    return response


def _paged_response(rows: list[Any], limit: int, offset: int) -> dict[str, Any]:
    response = _list_response(rows, limit)
    response["offset"] = offset
    response["total"] = offset + len(response["results"]) + (1 if response["truncated"] else 0)
    return response


def _classify_neo4j_failure(exc: BaseException) -> Neo4jUnavailable:
    code = str(getattr(exc, "code", "") or "")
    detail = str(exc) or exc.__class__.__name__
    combined = f"{code} {detail}".lower()
    class_name = exc.__class__.__name__.lower()
    if "timeout" in combined or "timed out" in combined:
        return Neo4jUnavailable("Neo4j query timed out", cause="timeout", detail=detail)
    if isinstance(exc, OSError) or "serviceunavailable" in class_name or "connection" in combined:
        return Neo4jUnavailable(
            "Neo4j connection failure", cause="connection_failure", detail=detail
        )
    return Neo4jUnavailable("Neo4j query error", cause="query_error", detail=detail)


def _strip_literals_and_comments(cypher: str) -> str:
    without_comments = re.sub(r"//.*?$|/\*.*?\*/", " ", cypher, flags=re.MULTILINE | re.DOTALL)
    return re.sub(r"'(?:''|\\.|[^'])*'|\"(?:\"\"|\\.|[^\"])*\"", " ", without_comments)
