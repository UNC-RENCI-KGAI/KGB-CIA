# data/

Local only. Everything here except this file is gitignored.

Record each source you download so runs are reproducible:

| Source | File | Version / date | Downloaded by | Notes |
| --- | --- | --- | --- | --- |
| TCRD | | | | MySQL dump, ~7.5 GB compressed, 41 GB uncompressed |
| Pharos | `query results.csv` (IDG Family = Kinase; 635 human kinases, UniProt IDs) | Pharos 3.19.10, TCRD 6.13.5, 2026-11-11 | Hina Shah | CSV exports / GraphQL. Source of the kinase seed list for the filter demo; 633 of 635 are nodes in the 11-09 merged graph (missing: O43930, Q6A1A2). From https://pharos.nih.gov/targets?facet=IDG%2BFamily!Kinase |