"""Thin FastMCP wrappers for fixed graph exploration operations."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP

from ..services.neo4j_service import Neo4jNotConfigured, Neo4jUnavailable


def register_graph_tools(mcp: FastMCP, svc: Any) -> None:
    """Register all fixed, read-only graph tools against a plain service object."""

    @mcp.tool
    def get_graph_info() -> dict[str, Any]:
        """Return graph counts, categories, predicates, sources and release metadata.

        Example use: orient before investigating ``NCBIGene:1017``. Presentation Guidelines:
        identify the graph version and preserve knowledge-source CURIEs verbatim. Next call
        ``get_schema`` to see valid graph patterns and properties.
        """

        return _safe(svc.get_graph_info)

    @mcp.tool
    def get_schema() -> dict[str, Any]:
        """Return the category/predicate meta-graph and properties with example values.

        Example use: find properties available on ``biolink:Gene``. Presentation Guidelines:
        use exact category, predicate and property names; do not invent schema terms. Next call
        ``search_nodes`` or ``summarize_node_edges`` to begin with a concrete entity.
        """

        return _safe(svc.get_schema)

    @mcp.tool
    def search_nodes(query: str, category: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Resolve a CURIE, equivalent identifier, exact name or synonym to graph nodes.

        Example: ``query='NCBIGene:1017', category='biolink:Gene'``. Presentation Guidelines:
        show all candidates when a name is ambiguous and use returned IDs verbatim. Next call
        ``get_node`` for details or ``summarize_node_edges`` for a cheap orientation.
        """

        return _safe(svc.search_nodes, query, category, limit)

    @mcp.tool
    def get_node(node_id: str) -> dict[str, Any]:
        """Return the complete record for one resolved node ID, equivalent ID or exact name.

        Example: ``node_id='NCBIGene:1017'``. Presentation Guidelines: preserve CURIEs and
        categories verbatim and never guess among ambiguous candidates. Next call
        ``summarize_node_edges`` before expanding a potentially high-degree node.
        """

        return _safe(svc.get_node, node_id)

    @mcp.tool
    def summarize_node_edges(node_id: str) -> dict[str, Any]:
        """Count a node's edges by direction, predicate and neighbor category.

        Example: summarize ``NCBIGene:1017`` before fetching neighbors.
        Presentation Guidelines: state direction and exact Biolink predicates; do not imply
        that counts are
        evidence. Next call ``get_node_edges`` with a predicate/category filter.
        """

        return _safe(svc.summarize_node_edges, node_id)

    @mcp.tool
    def get_node_edges(
        node_id: str,
        direction: str = "both",
        predicate: str | None = None,
        neighbor_category: str | None = None,
        neighbor_where: dict[str, Any] | None = None,
        exclude_flags: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Page through labeled neighbors and citable edge summaries for one node.

        Example: outgoing ``biolink:affects`` edges from ``NCBIGene:1017``.
        Presentation Guidelines: report edge IDs, predicates, knowledge sources, publication
        counts and any flags verbatim; if an answer uses a flagged edge, say it is flagged and
        give each reason verbatim, including any PMID. Next call ``explain_edge`` for every
        connection used in an answer.
        """

        return _safe(
            svc.get_node_edges,
            node_id,
            direction,
            predicate,
            neighbor_category,
            neighbor_where,
            exclude_flags,
            limit,
            offset,
        )

    @mcp.tool
    def get_edges_between(
        source_id: str,
        target_id: str,
        include_lineage: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Return every direct edge in either direction between two resolved nodes.

        Example: compare ``CHEBI:45783`` and ``NCBIGene:1017`` with lineage enabled.
        Presentation Guidelines: cite knowledge sources, publications, source records and flags
        verbatim; if an answer uses a flagged edge, say it is flagged and give each reason
        verbatim, including any PMID. With ``include_lineage=true`` this is the one-call answer
        to "why?"; otherwise call ``explain_edge`` next.
        """

        return _safe(
            svc.get_edges_between,
            source_id,
            target_id,
            include_lineage,
            limit,
            offset,
        )

    @mcp.tool
    def find_paths(
        source_id: str,
        target_id: str,
        max_hops: int = 3,
        predicates: list[str] | None = None,
        exclude_flags: list[str] | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Find bounded shortest paths between two known nodes while avoiding hub intermediates.

        Example: paths from ``CHEBI:45783`` to ``MONDO:0005148`` within three hops.
        Presentation Guidelines: preserve path direction, IDs, predicates and flags, and
        distinguish a path from causal evidence. If an answer uses a flagged hop, say it is
        flagged and give each reason verbatim, including any PMID. Next call
        ``get_edges_between(include_lineage=true)`` or ``explain_edge`` for each claimed
        connection.
        """

        return _safe(
            svc.find_paths, source_id, target_id, max_hops, predicates, exclude_flags, limit
        )

    @mcp.tool
    def find_connected_nodes(
        source_id: str,
        target_category: str,
        max_hops: int = 2,
        predicates: list[str] | None = None,
        target_where: dict[str, Any] | None = None,
        exclude_flags: list[str] | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Find category-constrained nodes reachable by bounded, hub-filtered paths.

        Example: diseases reachable from ``CHEBI:45783`` via a target. Presentation Guidelines:
        name every path predicate and flag, do not infer causality from reachability, and if an
        answer uses a flagged edge give each reason verbatim, including any PMID. Next call
        ``get_edges_between(include_lineage=true)`` for each edge supporting the answer.
        """

        return _safe(
            svc.find_connected_nodes,
            source_id,
            target_category,
            max_hops,
            predicates,
            target_where,
            exclude_flags,
            limit,
        )

    @mcp.tool
    def explain_edge(edge_id: str) -> dict[str, Any]:
        """Explain why an edge exists using Biolink provenance and exact source-record lineage.

        Example: ``edge_id='edge-123'`` copied from a neighbor or path result.
        Presentation Guidelines: cite knowledge sources, publications, mapping files, columns,
        constants, flags and source-record keys verbatim; label candidate matches as candidates
        and never paraphrase provenance. If the edge has flags, say it is flagged and give each
        reason verbatim, including any PMID. Next call ``get_edges_between`` if endpoint context
        is needed.
        """

        return _safe(svc.explain_edge, edge_id)


def _safe(function: Callable[..., dict[str, Any]], *args: Any) -> dict[str, Any]:
    try:
        return function(*args)
    except Neo4jNotConfigured:
        return {
            "error": "Neo4j not configured",
            "hint": (
                "Set NEO4J_URI and, for an authenticated Neo4j deployment, set both "
                "NEO4J_USER and NEO4J_PASSWORD; then restart the MCP server."
            ),
        }
    except Neo4jUnavailable as exc:
        return {
            "error": str(exc),
            "cause": exc.cause,
            "detail": exc.detail,
            "hint": (
                "Neo4j returned a classified failure. For connection failures, check the URI, "
                "network access, credentials if enabled, and service health. For timeouts or "
                "query errors, narrow the request or rebuild artifacts so aggregate answers are "
                "precomputed. The MCP server is still running."
            ),
        }
    except (ValueError, LookupError) as exc:
        return {
            "hint": str(exc),
            "suggestion": "Call get_schema for valid values or search_nodes to resolve names.",
        }
    except Exception:
        return {
            "hint": (
                "The graph lookup failed. Verify the server connection and try a narrower query."
            ),
            "suggestion": "Call get_graph_info to confirm that the graph is available.",
        }
