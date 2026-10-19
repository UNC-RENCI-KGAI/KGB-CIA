# Transformation agent

**Team:** @UNC-RENCI-KGAI/transformation · **Existing asset:** none agent-wise; ORION for deterministic parts

Decide which columns map to which Biolink class and predicate, then write and run the transform code. Full transform; selection happens in the filter stage.

## Run it alone

```bash
uv run kgb-transform --input <previous stage output> --output <path>
```

## Input / output

Uses the record types in `shared/schema.py`. Describe the exact files here once decided.

## Status

See the tracking issue for this team on GitHub. Replace this section with what works, what
doesn't, and how to try it.
