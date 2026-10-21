# Metadata agent

**Team:** @UNC-RENCI-KGAI/metadata · **Existing asset:** RoboCroissant

Describe each source file and its columns as Croissant metadata. Purely descriptive; builds no graph.

## Semantics

[`semantics/`](semantics/) adds what the data means (Biolink classes and associations,
identifier prefixes, units, code values, file dialects) to the Croissant files RoboCroissant
produces, as a Claude Code skill with checking scripts. See its README.

## Run it alone

```bash
uv run kgb-metadata --input <previous stage output> --output <path>
```

## Input / output

Uses the record types in `shared/schema.py`. Describe the exact files here once decided.

## Status

See the tracking issue for this team on GitHub. Replace this section with what works, what
doesn't, and how to try it.
