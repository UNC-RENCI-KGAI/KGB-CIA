"""FastMCP tool registration modules."""

from .graph_tools import register_graph_tools
from .query_tools import register_query_tools

__all__ = ["register_graph_tools", "register_query_tools"]
