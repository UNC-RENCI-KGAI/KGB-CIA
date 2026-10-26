# Augmentation agent

**Team:** @UNC-RENCI-KGAI/augmentation · **Existing asset:** PubMed; Bagel / LitCoin for text

Enrich the baseline graph with extra datasets and PubMed evidence for or against edges.

## Initial plan: PubMed evidence for existing edges

1. Read the JSON file of the baseline graph produced by the filter stage.
2. Find the node pairs joined by one or more edges.
3. Query PubMed for each pair of entities and retrieve the matching abstracts with their PMIDs.
4. From the abstracts, extract any evidence for or against the existing edges between that pair.
5. Label each retrieved publication, for each edge between the pair, with one of four
   categories:

   | Category | Meaning |
   | --- | --- |
   | confirmed | The abstract explicitly supports the edge |
   | contradicted | The abstract explicitly refutes the edge |
   | ambiguous | The abstract discusses the pair, but its evidence is unclear or mixed |
   | no evidence | The abstract does not bear on the edge |

6. Decorate each edge with a score computed from the number of publications in each category.
   The function is still to be decided.

The PMIDs behind a score go in the edge's `Provenance.evidence`, added to what is there rather
than replacing it. Where the score lives (an `Edge.properties` key or a new schema field) is
still to be decided.

## Step 3: PubMed query

### Which edges to query

Choose edges by (subject category, predicate, object category), matching on predicate ancestry
so that child predicates the transform picks are covered. A relationship qualifies when
abstracts state it as a claim and a paper could contradict it.

| Tier | Subject → predicate → object | Main TCRD sources |
| --- | --- | --- |
| Query | ChemicalEntity/Drug → `treats`, `contraindicated_in`, `treats_or_applied_or_studied_to_treat` → Disease | DrugCentral |
| Query | ChemicalEntity → `affects`, `directly_physically_interacts_with`, `binds` → Gene/Protein | DrugCentral, ChEMBL, IUPHAR |
| Query | Gene → `gene_associated_with_condition`, `genetically_associated_with`, `causes`, `contributes_to` → Disease | DisGeNET, JensenLab, OMIM, Monarch, GWAS Catalog |
| Query, capped | Gene ↔ `physically_interacts_with`, `interacts_with` ↔ Gene | STRING, BioPlex, Reactome |
| Query, capped | Gene → `has_phenotype` → PhenotypicFeature | IMPC, MGI, HPO via Monarch |
| Skip | Gene → `participates_in` → Pathway; Gene → `enables`, `participates_in`, `located_in` → GO terms | Reactome, KEGG, WikiPathways, GO |
| Skip | Gene → `expressed_in` → AnatomicalEntity/Cell | GTEx, HPA, Jensen TISSUES |
| Skip | `orthologous_to`, `has_part`, `subclass_of`, `member_of` | Orthologs, Pfam, PANTHER, DTO |

- **Capped:** gene–gene interactions are numerous and symmetric, and many come from a single
  high-throughput screen. Query only pairs above a confidence cutoff, and dedupe unordered
  pairs. Phenotype names are long and varied, so expect low recall.
- **Skipped:** pathway and GO names rarely appear verbatim in abstracts, and GO annotations
  already carry their own references and evidence codes. Expression edges are measurements
  rather than claims and are very numerous. The rest are true by construction.

### Source PMIDs

Every in-scope pair is searched, even when its edges arrive with PMIDs from their source
(likely GeneRIF, ChEMBL activity references and DrugCentral references; the TCRD columns still
need checking). Those source PMIDs are always fetched and labeled as well, even when they fall
outside the search's top results. This matters most for ChEMBL compound–target edges, where a
name search finds little.

### What step 3 writes for each pair

- The query string, retrieval date, and ESearch total hit count.
- Each fetched PMID with its origin (`search`, `source` or `source+search`, so the score counts
  it once) and its relevance rank.
- The title and abstract of each fetched PMID.

The cap on abstracts per pair (default 20 by relevance, configurable) limits how many
confirmations an edge can collect. Compute the score over the fetched set, and treat the total
hit count as a separate signal.

### API: NCBI E-utilities

ESearch finds the PMIDs and EFetch retrieves the abstracts.

- **Complete:** it searches all of current PubMed, and its `[tiab]` and `[mh]` field tags let a
  query combine synonyms from our ID libraries with MeSH terms where NodeNorm supplies MESH IDs.
- **Covers every entity type above.** PubTator3 indexes only genes, chemicals, diseases,
  variants, species and cell lines.
- **Favors recall:** step 4 links the entities in each abstract back to IDs and discards the
  misses, so the search can be broad. Matching by ID, PubTator3's main advantage, is something
  we already have.

Query shape:

```text
("<A synonym>"[tiab] OR "<A synonym>"[tiab] OR <A MeSH>[mh])
  AND ("<B synonym>"[tiab] OR <B MeSH>[mh])
  AND hasabstract
sort=relevance, retmax=20
```

- **Rate limits:** 3 requests a second without an API key and 10 with one (one key per NCBI
  account). Send `tool` and `email` with every request; read them from `NCBI_API_KEY` and
  `NCBI_EMAIL`.
- **Throughput:** one ESearch per pair plus one EFetch per ~200 deduplicated PMIDs, so roughly
  30,000 pairs an hour with a key. ESearch returns at most 10,000 IDs per query.
- **Off-peak:** NCBI asks that large jobs run on weekends or between 9pm and 5am ET on weekdays.
- **Reproducibility:** cache raw responses in SQLite under `data/` so reruns make no requests,
  and keep the query string and retrieval date with the evidence, since PubMed changes over time.

Kept in reserve:

- **Europe PMC REST:** the same abstracts plus open-access full text, with abstracts returned by
  the search itself. Switch if we need full text or the NCBI limits start to hurt.
- **A local PubMed baseline index:** no rate limits and a fixed snapshot, but worth building only
  if the pair count reaches the millions.
- **PubTator3:** a possible cross-check for gene, chemical and disease pairs. Its precomputed
  relation types (treat, inhibit, cause, associate and others) could seed step 4.

### Open items

- Filter team: which of these edge types the filter keeps by default, and how many there are.
  This sets the scale.
- Transformation team: whether source PMIDs are carried into `Provenance.evidence`.
- Who holds the NCBI API key.

## Run it alone

```bash
uv run kgb-augment --input <previous stage output> --output <path>
```

## Input / output

Uses the record types in `shared/schema.py`.

- **Input:** a JSON file of the filtered baseline graph (a `Graph`).
- **Output:** the same graph with every edge scored and its PMIDs recorded.

Describe the exact files and paths here once decided.

## Status

See the tracking issue for this team on GitHub. Replace this section with what works, what
doesn't, and how to try it.
