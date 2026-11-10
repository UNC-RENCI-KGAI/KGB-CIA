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

On `main`, `kgb-filter` is a stub that prints "not implemented yet". In progress:
[#47](https://github.com/UNC-RENCI-KGAI/KGB-CIA/pull/47) (describe-graph skill: a plain-language
overview of the graph) and [#50](https://github.com/UNC-RENCI-KGAI/KGB-CIA/pull/50) (cut the
subgraph a user asks for).
