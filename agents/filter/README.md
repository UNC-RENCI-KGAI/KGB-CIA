# Filter agent

**Team:** @UNC-RENCI-KGAI/filter · **Existing asset:** none

Turn what the user asks for into a subgraph of the merged graph, with strong defaults; with no
request, the whole graph. Transformation already merges the sources, so this stage doesn't.

## Run it alone

```bash
uv run kgb-filter --input <previous stage output> --output <path>
```

## Input / output

Uses the record types in `shared/schema.py`. Describe the exact files here once decided.

## Status

The `kgb-filter` command is still a stub that prints "not implemented yet". The
`describe-graph` skill (`.agents/skills/filter/describe-graph/`) does the first half of the
filter's job: given the user's own source, it measures how much of it the pre-built sources
already share and recommends which sources to focus on, with that list as the default. In
progress: [#50](https://github.com/UNC-RENCI-KGAI/KGB-CIA/pull/50) (cut the subgraph a user asks
for).
