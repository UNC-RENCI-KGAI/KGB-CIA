# Known limitations of the submitted graph

The submitted graph is the Nov 9, 2026 build of 26 sources. After the build, we chose to fix
problems in code and record them here rather than rebuild before the deadline. The list below
is what that graph is known to get wrong or leave out. Fixes are tracked in #104.

## Record sets that wrote no edges

Each of these record sets read rows but wrote no edges, according to the build's per-source
`transform_report.json` and `diagnostics.jsonl`.

| Source / record set | Rows read → written | Cause |
| --- | --- | --- |
| Monarch `disease_inheritance` | 8,944 → 0 | The metadata gives `knowledge_level` and `agent_type` as one-element lists, so every edge fails validation. |
| Reactome `disease_variants` | 6,294 → 0 | IDs don't match the metadata plan's `item_pattern`. |
| eRAM `disease_symptom` | 1,663 → 0 | The metadata checker and the runtime matched patterns differently. The checker is fixed; the plan fix is #88. |
| eRAM `text_mined_disease_gene` | 2,097 → 0 | A pattern mismatch. Whether these text-mined edges belong in the graph at all is #86. |

## Edges that are in the graph but typed wrongly

- **eRAM `has_phenotype`** (#88): the metadata types the objects as phenotypes, but NodeNorm
  resolves most of them to diseases. Cleaning flags 20,741 eRAM edges as type violations and
  202 as self-loops; it keeps them.

## Coverage

- **Monarch** (#87): the build has 77,357 Monarch edges. The current raw Monarch croissant
  doesn't describe the main Monarch KG files, so rebuilding from it would get only 39,862 edges
  until that's fixed.
- **Unresolved nodes:** 1,914,123 kept edges have one end NodeNorm couldn't resolve. Each
  carries an `unresolved_node` flag; edges with neither end resolved were dropped.

## Not affected

ClinVar's `#GeneID` header bug (fixed in #82) didn't affect this build. The cleaned graph has
10,705 ClinVar `gene_associated_with_condition` edges, the same as a rebuild with the fix.
