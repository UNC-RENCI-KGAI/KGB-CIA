# Augmentation agent

**Team:** @UNC-RENCI-KGAI/augmentation · **Existing asset:** PubMed; Bagel / LitCoin for text

Find PubMed evidence for edges: add the papers that support an edge to its `publications`, and
propose more specific edges where the papers support them. Proposed edges sit beside the
originals, never in place of them.

## Run it alone

```bash
uv run kgb-augment --input <previous stage output> --output <path>
```

## Input / output

Uses the record types in `shared/schema.py`. Describe the exact files here once decided.

## Status

On `main`, `kgb-augment` is a stub that prints "not implemented yet". In progress:
[#22](https://github.com/UNC-RENCI-KGAI/KGB-CIA/pull/22) (the pubmed-evidence skill). PubMed
search, fetch and cache are already on `main` in `shared/pubmed/`, shared with the cleaning
stage's evidence check.
