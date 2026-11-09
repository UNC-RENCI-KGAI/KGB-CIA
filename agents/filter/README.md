# Filter and integration agent

**Team:** @UNC-RENCI-KGAI/filter · **Existing asset:** none

Apply user choices with strong defaults (affinity cutoffs, edge types) and merge triples into one connected baseline graph.

## Run it alone

```bash
uv run kgb-filter --input <previous stage output> --output <path>
```

## Input / output

Uses the record types in `shared/schema.py`. Describe the exact files here once decided.

## Status

The `kgb-filter` command is still a stub. The `describe-graph` skill
(`.agents/skills/filter/describe-graph/`) does the first half of the filter's job: given the
user's own source, it measures how much of it the pre-built sources already share and
recommends which sources to focus on, with that list as the default.
