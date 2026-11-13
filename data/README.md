# data/

Local only. Everything here except this file is gitignored.

## cache/

Every cache lives under `cache/` (`shared/paths.py`; `KGB_CACHE_DIR` moves it, for example to
share it between checkouts or git worktrees):

| Directory | What | Written by |
| --- | --- | --- |
| `croissant/` | Raw RoboCroissant files, the input to the croissant-semantics skill | You |
| `croissant-semantics/` | That skill's downloads, validation results and Biolink vocabulary | The skill's scripts |
| `pubmed/` | `pubmed.sqlite`: PubMed searches, abstracts and NodeNorm entries (`KGB_PUBMED_CACHE` names another file) | `shared.pubmed` |
| `augmentation/` | `labels.sqlite` (every paid claim and review) and the UBERON release (`KGB_AUGMENT_CACHE_DIR` names another directory) | augmentation |

Don't delete `pubmed/` or `augmentation/`: rebuilding them means searching PubMed and paying for
every label call again.

Record each source you download so runs are reproducible:

| Source | File | Version / date | Downloaded by | Notes |
| --- | --- | --- | --- | --- |
| TCRD | | | | MySQL dump, ~7.5 GB compressed, 41 GB uncompressed |
| Pharos | `query results.csv` (IDG Family = Kinase; 635 human kinases, UniProt IDs) | Pharos 3.19.10, TCRD 6.13.5, 2026-11-11 | Hina Shah | CSV exports / GraphQL. Source of the kinase seed list for the filter demo; 633 of 635 are nodes in the 11-09 merged graph (missing: O43930, Q6A1A2). From https://pharos.nih.gov/targets?facet=IDG%2BFamily!Kinase |