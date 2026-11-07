"""Optional bounded raw-Cypher MCP tool."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from .graph_tools import _safe


def register_query_tools(mcp: FastMCP, svc: Any) -> None:
    """Register raw Cypher only when explicitly enabled by server configuration."""

    @mcp.tool
    def query_cypher(
        cypher: str, parameters: dict[str, Any] | None = None, limit: int = 100
    ) -> dict[str, Any]:
        """Run one EXPLAIN-checked, read-only, timed and row-capped Cypher query.

        Categories are labels such as ``:`biolink:Gene```; node IDs are ``n.id``; predicates
        are relationship types; edge IDs are ``r.id``. Examples: ``MATCH (n:`biolink:Gene`)
        RETURN count(n) AS genes``; or ``MATCH (n:`biolink:Gene`) WHERE n.id=$id RETURN n`` with
        ``parameters={'id':'NCBIGene:1017'}``. Presentation Guidelines: state the query and
        preserve returned IDs, labels and provenance verbatim. Use this only for aggregates or
        patterns fixed tools cannot express; next call ``explain_edge`` for claimed connections.
        """

        return _safe(svc.query_cypher, cypher, parameters, limit)
