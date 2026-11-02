# Transformation agent

**Team:** @UNC-RENCI-KGAI/transformation

Generate per-source ORION parsers from the metadata agent's semantic mappings and run them
to construct graphs with provenance and source-record lineage. Report semantic metadata
failures to the metadata team. ORION handles normalization; filtering, cleaning, and evaluation
remain outside transformation.

Transformation only uses what the supplied semantic metadata declares. It never infers,
guesses, or pattern-matches a value the metadata doesn't contain — including a source's
infores identity: that comes only from a constant already present in the metadata, or an
explicit `--infores` confirmed independently by whoever generates that source's parser, never
derived from a name, URL, or file path. A source with no declared or confirmed identity is
reported as `needs_infores`, not run with a fabricated one.

## Run it alone

```bash
uv run kgb-transform                                  # autodetects --input under data/
uv run kgb-transform --input data/metadata --output data/transformation
```

This generates a parser for every semantic Croissant bundle under `--input`, runs each
compiled one natively through ORION, and writes `data/transformation/transformation_report.json`
as the handoff to the metadata team for anything it could not convert. With no `--input`,
it uses `data/metadata` if populated, otherwise the one directory under `data/` that actually
contains semantic Croissant bundles; if more than one does, it fails with a message asking you
to pass `--input` explicitly, rather than guessing. For first-pass
onboarding of a new source, ambiguous `--infores` confirmation, or parsing-only tests, work
through the standalone
[generate-graph skill](../../.agents/skills/transformation/generate-graph/README.md) directly
instead; its
[workflow reference](../../.agents/skills/transformation/generate-graph/reference/orion_workflow.md)
describes commands, local file bindings, and runtime requirements. `kgb-transform` replays
that same generate-then-run workflow across every source, unattended, once a source's identity
is confirmed.

## Input / output

Inputs are semantic metadata bundles containing `croissant.semantic.json` or its gzip
equivalent, with `semantics_plan.json` and supporting reports when available, under
`--input` (default: autodetected under `data/`, preferring `data/metadata` — see above).

Outputs, under `--output` (default `data/transformation`), are generated ORION loader
packages, KGX node and edge files following `shared/schema.py`, generation reports, per-source
`TransformReport` files, diagnostics, and `transformation_report.json` aggregating every
source's status for the metadata team. Every emitted edge includes `kgbcia_source_records`,
linking its source record to a mapping. Keep source data and generated outputs under
gitignored `data/`.

## Status

The metadata compiler, shared extraction runtime, and ORION runner are implemented and tested.
`kgb-transform` now wires them together: it generates parsers for every bundle under
`--input`, runs each `ready`/`partial` source through ORION in its own `uv run --script`
process (ORION needs Python 3.12+), and reports `needs_infores`/`unsupported` sources and
generation errors without treating them as pipeline failures. `kgb-transform`'s batch run never
passes `--infores`, so a `needs_infores` source stays reported, not guessed, until someone
confirms its identity out-of-band through the generate-graph skill directly. Reader coverage is
partial and disclosed in generation reports. Metadata defects are handed over without changing
the supplied semantics. Unresolved NodeNorm identifiers do not by themselves indicate parser
failure.
Re-running with changed local file bindings or a corrected `--infores` needs a fresh
`--output` directory until ORION's stage cache fingerprints those inputs.
