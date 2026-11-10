# MCP interface

**Team:** @UNC-RENCI-KGAI/mcp-interface · **Existing asset:** OKN MCP as the pattern

Expose the final graph to any local agent through an MCP server modeled on the OKN MCP: neighbors, paths, and edge lineage.

## Run it alone

```bash
uv run kgb-mcp --input <previous stage output> --output <path>
```

## Input / output

Uses the record types in `shared/schema.py`. Describe the exact files here once decided.

## Status

On `main`, `kgb-mcp` is a stub that prints "not implemented yet". A first MCP server backed by
Neo4j is on the `mcp-interface/neo4j-server` branch (no PR yet), and is being deployed on AWS
for the hosted demo.
