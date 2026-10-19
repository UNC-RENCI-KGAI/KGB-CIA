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

See the tracking issue for this team on GitHub. Replace this section with what works, what
doesn't, and how to try it.
