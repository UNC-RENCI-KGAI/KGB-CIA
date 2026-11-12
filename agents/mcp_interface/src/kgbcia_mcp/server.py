"""Build and run the FastMCP application."""

from __future__ import annotations

import sys
from typing import Any

from fastmcp import FastMCP

from .artifacts import load_artifacts
from .config import Settings
from .lineage import LineageIndex
from .services.neo4j_service import Neo4jService
from .tools import register_graph_tools, register_query_tools

SERVER_INSTRUCTIONS = """Resolve names with search_nodes. Orient with get_schema or
summarize_node_edges, then explore with get_node_edges, find_paths or find_connected_nodes.
Justify every claimed connection with get_edges_between(include_lineage=true) or explain_edge,
citing knowledge sources, publications and source records verbatim. Use query_cypher only for
aggregates that fixed tools cannot answer."""


def create_server(
    service: Any | None = None,
    settings: Settings | None = None,
    *,
    enable_cypher: bool | None = None,
) -> FastMCP:
    """Create the in-process app; dependency injection keeps unit tests database-free."""

    configured = settings or Settings.from_env()
    svc = service or _create_service(configured)
    mcp = FastMCP("KGB-CIA Knowledge Graph", instructions=SERVER_INSTRUCTIONS)
    register_graph_tools(mcp, svc)
    cypher_enabled = configured.enable_cypher if enable_cypher is None else enable_cypher
    if cypher_enabled:
        register_query_tools(mcp, svc)
    return mcp


def run(
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8000,
    *,
    settings: Settings | None = None,
    enable_cypher: bool | None = None,
) -> None:
    """Connect, warm caches, and run over stdio or streamable HTTP."""

    configured = settings or Settings.from_env()
    service = _create_service(configured)
    try:
        mcp = create_server(service, configured, enable_cypher=enable_cypher)
        if transport == "stdio":
            mcp.run(transport="stdio", show_banner=False)
        elif transport == "http":
            mcp.run(transport="http", host=host, port=port, show_banner=False)
        else:
            raise ValueError("transport must be stdio or http")
    finally:
        service.close()


def _create_service(settings: Settings) -> Neo4jService:
    lineage = LineageIndex()
    graph_metadata: dict[str, Any] = {}
    hubs: list[str] = []
    if settings.artifacts:
        try:
            bundle = load_artifacts(
                settings.artifacts,
                settings.artifacts_sha256,
            )
            lineage = LineageIndex(bundle.reports)
            graph_metadata = bundle.graph_metadata
            hubs = _hub_ids(bundle.hubs)
        except Exception as exc:
            _warn(f"lineage artifacts unavailable: {exc}")
    if not settings.neo4j_uri:
        _warn(
            "Neo4j not configured; tools will return a configuration hint until the server "
            "is restarted with NEO4J_URI"
        )
    elif bool(settings.neo4j_user) != bool(settings.neo4j_password):
        _warn("NEO4J_USER and NEO4J_PASSWORD must be set together when authentication is used")
    return Neo4jService(
        settings.neo4j_uri,
        settings.neo4j_user,
        settings.neo4j_password,
        settings.neo4j_database,
        lineage=lineage,
        graph_metadata=graph_metadata,
        hubs=hubs,
    )


def _warn(message: str) -> None:
    print(f"kgbcia-mcp warning: {message}", file=sys.stderr)


def _hub_ids(value: Any) -> list[str]:
    if isinstance(value, dict):
        value = value.get("hubs", [])
    if not isinstance(value, list):
        raise ValueError("hubs.json must be a list or an object with a hubs list")
    result = []
    for item in value:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict) and isinstance(item.get("id"), str):
            result.append(item["id"])
        else:
            raise ValueError("each hub must be a node ID or an object containing id")
    return result
