# Adding semantics to Croissant files

## Purpose

RoboCroissant files describe the structure of each knowledge base: which files exist, which tables are in them, and what each column is called and what basic type it has (text, integer). They don't say what the data *means*. For example, they don't say that a column holds gene identifiers, or that a row says a gene is associated with a disease.

This note describes how that meaning is written into a Croissant file using only what Croissant 1.1 already provides, plus a few conventions of our own built from standard parts. The `croissant-semantics` skill (this directory; see `README.md`) does this. It starts from a croissant we already have and writes a new one with semantics added. Folding it into the main RoboCroissant pipeline can come later.

## What Croissant 1.1 provides

Croissant accepts terms from any vocabulary, as long as the vocabulary's prefix is declared in the file's `@context`. Validators check structure only; interpreting the terms is left to the consumer. The mechanisms the skill uses:

| Mechanism | What it says | Example |
|---|---|---|
| Field `dataType` (several allowed) | What the values in a column are | `["sc:Text", "biolink:Gene"]` |
| Record set `dataType` | What each row is | `biolink:GeneToDiseaseAssociation` |
| `equivalentProperty` | Which property of the row's (or group's) type a field fills | `biolink:subject` |
| `subField` | Groups columns into one entity | gene = ID + symbol |
| `references` | A column's values are keys in another record set | a code column → its code table |
| `sc:Enumeration` record sets with inline `data` | Maps code values to terms | `marker/mechanism` → a predicate |
| Field `value` (new in 1.1) | A constant for every row | taxon `NCBITaxon:7227` |
| `annotation` (new in 1.1) | Facts attached to a column | an ID prefix, a unit |
| Several `encodingFormat` values | Content type, compression and spec of a file | `text/tab-separated-values`, `application/gzip` |

The spec says that when a column has a semantic type, its values should be identifiers that can be read as those entities. In practice that means CURIEs such as `DOID:2907`, with their prefixes declared in `@context`. Where the data holds bare ids, an annotation says which prefix they take.

## The approach

### 1. Group columns into entities

A row usually mentions more than one thing, and each thing is spread over several columns. Using `subField`, the columns for one thing become one field, and that field is typed. This is the mechanism the Croissant spec uses to combine latitude and longitude into a `GeoCoordinates`.

Grouping is the most important step:

- It says which name or symbol belongs to which identifier.
- It puts the type on the entity ("this is a gene") rather than on one column.
- It handles rows that mention several entities, such as a gene, a disease, and orthologs.

When an entity's id can be in one of two columns (a UniProt accession filed under SwissProt for some rows and TrEMBL for others), there are two entities with the same role.

### 2. Type each row

Two patterns:

- **Rows are relationships** (gene–disease pairs). The record set is typed as an association, and its entities fill the association's `subject` and `object`. The association's own constraints matter: `PairwiseMolecularInteraction` only takes molecular entities, so gene-typed interactors need a different class. The predicate is a constant, or comes from a code column.
- **Rows are things** (a variant table, a gene list), each with a unique id. The record set is typed with the thing's class, its own columns are mapped directly, and other things a row mentions become entities with roles like `biolink:has_gene`. If row ids repeat, the rows are really relationships.

A row then reads as a knowledge graph edge (or node) with properties and qualifiers. That is what an ingest needs to be generated from the croissant instead of written by hand.

### 3. Map code columns to terms

Columns that hold codes (association types, evidence codes, source names) point with `references` to an `sc:Enumeration` record set, which gives each code a term: a predicate, an ECO term, an infores id. Code tables are shared: two columns with the same mapping use one table.

Negated and inferred codes are kept apart from direct ones. `is_not_implicated_in` maps to the predicate it negates, and a **derived field** reads the same column again as `biolink:negated`. Croissant allows two fields to read one column, so one column can say two things.

### 4. Use annotations for what the columns leave out

Some facts apply to every value in a column but aren't in the data. These go in an annotation on that column:

- **Identifier prefix** for bare local ids (`50518` in CTD's `GeneID` is `NCBIGene:50518`). When the prefix differs by row and another column holds it (GAF's `DB`), the annotation reads it from that column instead of being a constant.
- **Id form**, when there's no single prefix: full IRIs, a mix of CURIEs and bare ids, OBO short forms (`MONDO_0004989`), or the source's own tagged strings that only look like CURIEs (MITAB's `entrez gene/locuslink:613`, HGVS).
- **Units** (QUDT).
- **List separators**, **null values** (`-`, `N/A`) and **true values** (`NOT` in a negation column).

Constants, such as a taxon for a single-species file, are fields with a `value`. A constant can also be taken from the record set's name, so each of PGS Catalog's 5,450 scoring files can say which score it belongs to.

### 5. Say how to read the file

Many knowledge-base files aren't plain CSV: VCF's `##` lines, GAF's `!` lines and missing header row, CTD's commented field list. If the croissant doesn't say so, no tool can read the file without guessing. Each file gets a **dialect**:

- W3C CSV on the Web dialect properties (`csvw:delimiter`, `csvw:commentPrefix`, `csvw:header`, `csvw:null`, ...).
- The format's published spec in `encodingFormat`. For well-known formats (VCF, GAF, GPAD/GPI, PSI-MI TAB) the spec is the real definition; the CSVW properties are just enough for a generic reader.
- The content type first in `encodingFormat`, then compression. A gzipped table described only as `application/gzip` can't be read; this alone was the cause of many load failures.
- For files without a header row, the column names in file order.

Known formats are kept in a registry with their CSVW properties, media type, spec URL and EDAM format. The skill detects which one fits a file by parsing its first lines with each.

## Which vocabulary to use

We can't count on everything mapping cleanly to Biolink. For each column, entity or row, the skill uses the first option that fits:

1. **Biolink, when it truly fits.** Genes, diseases, chemicals, and the common associations and predicates. It's what knowledge graph builders already use. The skill pins a Biolink version (v4.4.6) and checks every term against it.
2. **Another established vocabulary**, when Biolink doesn't have the term: OBO ontologies (SO, RO, ECO, UBERON, STATO, OBI, BAO), EDAM, QUDT for units, infores for knowledge sources, schema.org.
3. **A term of our own, linked to the nearest real one.** For things specific to one knowledge base, a term in the `rcv:` namespace (`https://roboboulangerie.github.io/vocab/`, provisional), with a label and definition taken from the knowledge base's documentation. It's related to the closest existing term with SKOS (`skos:broadMatch`, `skos:closeMatch`), or says why there is none. These terms go in a `rc_local_terms` record set in the croissant.
4. **No semantic type, just a good description.** Plain `sc:Text` is better than a wrong type. Falling back to `biolink:related_to` or `biolink:NamedThing` loses information and looks more certain than it is.

Structure doesn't have to wait for vocabulary. Grouping, keys, and annotations are worth adding even when an entity only reaches option 3 or 4. Terms that end up at option 3 make a list of what Biolink is missing. In the test runs, local terms ranged from none (CTD, HGNC) to most of the typed fields (PGS Catalog, whose scores and performance metrics have no established class).

## Recording who said so

Each assertion (a type, a mapping, a prefix) records its basis, source and confidence:

- **Basis:** why, in a sentence ("Each row pairs one gene with one disease plus evidence and PubMed ids").
- **Source:** the agent, a maintainer, the knowledge base's documentation, or its data dictionary.
- **Confidence:** high, medium or low. Column names alone justify at most medium.

For now these live in the semantics plan (below), not in the croissant. Answers from a knowledge base's maintainers go in a `semantics_answers.json` file next to its croissant (in `kbs/<name>/`). The scripts merge them into any plan for that knowledge base, overriding the agent's choices with `source: maintainer`, so an answer is given once and holds for every rerun. ROBOKOP's is the first. RoboCroissant already tracks provenance for dataset-level values in `persistent_fields.json`. With the same information for semantics, RoboBoulangerie could ask maintainers to confirm a type ("is this column a `biolink:Gene`?") and lock in their answer, the way field corrections work today.

## How the skill works

The agent doesn't edit the croissant. It writes a **semantics plan** (JSON), and scripts apply and check it:

1. **Survey.** List the record sets grouped into layouts (record sets with the same columns), largest first. Big knowledge bases have hundreds of layouts; the agent works from the top and lists what it leaves out, with reasons.
2. **Look at the data.** Read real rows from several files per layout, detect the file dialect, profile code columns over the whole file, and read the knowledge base's documentation. Lookup tools cover Biolink classes and slots, association classes for a subject/object pair, identifier prefixes (Biolink and Bioregistry), which prefixes fit a set of bare ids, ontology terms (OLS) and infores ids.
3. **Write the plan**, by layout: dialect, row type, entities, column specs, constants, code mappings, local terms.
4. **Apply** it to produce a new croissant.
5. **Check:**
   - The plan: terms exist, prefixes are declared, entity classes fit the association, units exist in QUDT, infores ids are in the catalog, negated or inferred codes aren't merged with direct ones. This takes seconds, so it runs first.
   - The data: sampled values against declared prefixes (Bioregistry patterns), codes against mappings, files against their dialect, ids that are missing or repeat.
   - Croissant: the MLCommons validator, and reading a few records of each layout through mlcroissant. This uses the first couple of MB of each file, not whole downloads. Failures that happen with the original croissant too are reported as pre-existing.
   - Coverage: share of record sets and fields with semantics, by kind of vocabulary.
6. **Write notes** for reviewers: what was covered, choices worth a second look, problems found in the data or the pipeline, and questions for the knowledge base's maintainers.

## Our conventions

Croissant has no native way to say these, so they're conventions. Tools that don't know them still read the file; they just don't act on them.

| Convention | On | Means |
|---|---|---|
| `sh:prefix` annotation | Field | The column holds bare ids; prefix them with this CURIE prefix |
| `rcv:idForm` annotation | Field | `url`, `mixed`, `obo` or `native`: no single prefix applies |
| `qudt:hasUnit` annotation | Field | Unit of a numeric column |
| `rcv:listSeparator` annotation | Field | Cells hold several values joined by this character |
| `rcv:nullValue`, `rcv:trueValue` annotations | Field | Values meaning "no value" / "true" |
| `csvw:dialect` | FileObject, FileSet | How to read the file (CSVW properties) |
| `rcv:columnOrder`, `rcv:columnNames` | FileObject, FileSet | Whether the file has a header row; if not, its column names in order |
| `rcv:dialect` | FileObject, FileSet | The named format the dialect came from (`vcf-4`, `gaf-2.2`) |
| `rc_local_terms` record set | Dataset | Terms defined for this knowledge base, with SKOS links |

## Worked example: CTD curated gene–disease associations

`CTD_curated_genes_diseases.tsv.gz` has a block of `#` comment lines (licence, then the field names on a commented line), no header row, and the columns `GeneSymbol`, `GeneID`, `DiseaseName`, `DiseaseID`, `DirectEvidence`, `OmimIDs`, `PubMedIDs`. Its original croissant says only that it is a gzip file with those columns.

The skill's output, trimmed (field ids shortened, sources shown as `src(...)`):

```json
{
  "@type": "cr:RecordSet",
  "@id": "curated_genes_diseases",
  "dataType": ["biolink:GeneToDiseaseAssociation"],
  "field": [
    {
      "@id": "curated_genes_diseases/gene",
      "dataType": ["biolink:Gene"],
      "equivalentProperty": "biolink:subject",
      "subField": [
        { "@id": "GeneID", "dataType": ["sc:Integer", "biolink:Gene"],
          "equivalentProperty": "biolink:id", "source": "src(GeneID)",
          "annotation": [{ "@type": "cr:Field", "@id": "GeneID/id_prefix", "name": "id_prefix",
                           "dataType": "sc:Text", "equivalentProperty": "sh:prefix", "value": "NCBIGene" }] },
        { "@id": "GeneSymbol", "dataType": "sc:Text",
          "equivalentProperty": "biolink:symbol", "source": "src(GeneSymbol)" }
      ]
    },
    {
      "@id": "curated_genes_diseases/disease",
      "dataType": ["biolink:Disease"],
      "equivalentProperty": "biolink:object",
      "subField": [
        { "@id": "DiseaseID", "dataType": ["sc:Text", "biolink:Disease"],
          "equivalentProperty": "biolink:id", "source": "src(DiseaseID)" },
        { "@id": "DiseaseName", "dataType": "sc:Text",
          "equivalentProperty": "biolink:name", "source": "src(DiseaseName)" }
      ]
    },
    { "@id": "DirectEvidence", "dataType": "sc:Text", "source": "src(DirectEvidence)",
      "equivalentProperty": "biolink:predicate",
      "references": { "field": { "@id": "codes_dbebbf69/name" } } },
    { "@id": "PubMedIDs", "dataType": "sc:Text", "source": "src(PubMedIDs)",
      "equivalentProperty": "biolink:publications",
      "annotation": [ { "...": "sh:prefix PMID" }, { "...": "rcv:listSeparator |" } ] }
  ]
}
```

`codes_dbebbf69` is an `sc:Enumeration` record set mapping `marker/mechanism` and `therapeutic` to predicates. `DiseaseID` already holds CURIEs (`MESH:D003872`), so it needs no prefix annotation; `MESH` is declared in `@context`.

The file itself gets its dialect:

```json
{
  "@id": "file_curated_genes_diseases_tsv",
  "contentUrl": "https://ctdbase.org/reports/CTD_curated_genes_diseases.tsv.gz",
  "encodingFormat": ["text/tab-separated-values", "application/gzip", "https://ctdbase.org/downloads/"],
  "csvw:dialect": { "@type": "csvw:Dialect", "csvw:delimiter": "\t",
                    "csvw:commentPrefix": "#", "csvw:header": false },
  "rcv:columnOrder": "field_order",
  "rcv:columnNames": ["GeneSymbol", "GeneID", "DiseaseName", "DiseaseID", "DirectEvidence", "OmimIDs", "PubMedIDs"],
  "rcv:dialect": "ctd-tsv"
}
```

Reading records with only what this croissant says about the file gives
`{"GeneSymbol": "A", "GeneID": "50518", "DiseaseName": "Dermatitis", "DiseaseID": "MESH:D003872", "DirectEvidence": "marker/mechanism", ...}`.

## Context additions

Files with semantics need, in `@context`:

- `annotation` → `cr:annotation`, and `value` → `sc:value`. Our older files lack these; with `cr:value`, mlcroissant reads constants back empty.
- Prefixes for each vocabulary used: `biolink`, `skos`, `sh`, `qudt`, `unit`, `csvw`, `rcv`, and any OBO prefixes.
- Every identifier prefix that appears in the data (`MESH`, `NCBIGene`, `ECO`, ...), so CURIEs expand to full IRIs. Expansions come from Biolink where it has them, else Bioregistry, and must end in a delimiter (`/`, `#`, `_`, `:`, `=`).

## What mlcroissant 1.1.1 does with it

- The output croissants pass `mlcroissant validate`. Reading records back gives nested entities: `{gene: {GeneID, GeneSymbol}, disease: {DiseaseID, DiseaseName}, DirectEvidence, ...}`.
- `references` must use the 1.0 form `{"field": {"@id": ...}}`; the bare 1.1 form crashes mlcroissant.
- Entities can't be grouped over JSON-path columns (mlcroissant fails to read them), so those columns are typed one by one.
- Two fields reading the same column (derived fields) load fine.
- mlcroissant ignores `csvw:dialect`. A gzipped table with a header row loads once its content type comes first in `encodingFormat`. Files with comment lines or no header row (VCF, GAF, CTD) still don't load in mlcroissant, although the croissant now describes them fully. Reading them is the gap tracked in robo-croissant-pipeline #56. The skill's own data checks read them using the dialect.

## Tests so far

The skill was run by fresh agents, with no context from its development, on ten knowledge bases from the sandbox, in three rounds:

- **Round 1:** CTD, Gene Ontology, CIViC and BioPortal.
- **Round 2:** HGNC, Alliance of Genome Resources, BindingDB and PGS Catalog.
- **Round 3:** MSigDB, ClinPGx, FlyBase and BRCA Exchange.

Each round's feedback, and a separate review of the round-2 plans, changed the skill. Most of the checks listed above exist because a run made that mistake and passed the earlier checks. Coverage varies a lot by knowledge base: 42 of 42 record sets with semantics for CTD (61% of fields Biolink), 17,091 of 17,091 for PGS Catalog (mostly local terms), 342 of 474 for MSigDB, 916 of 3,366 for BRCA Exchange (plus dialects on 3,032 files, mostly VCFs).

Round 3 also showed that the scripts can hit a knowledge base's servers too hard: 55 parallel sampling calls got FlyBase's firewall to answer with bot challenges. The scripts now pace their requests per host, cache downloads, stop on a bot challenge, and the skill says to sample one knowledge base at a time.

The runs also turned up pipeline problems. Among them:

- Data URLs that point at a release that's gone.
- Atomic types inferred file by file, so one column is Integer in some files and Text in others.
- Columns declared that the files don't have.
- Stale checksums from monthly rebuilds.
- Files described only as gzip.

These are filed on robo-croissant-pipeline (#51–#58).

## Gaps and open questions

- **Provenance in the croissant.** Basis, source and confidence stay in the plan. Putting them in the croissant, so maintainers can confirm them through RoboBoulangerie, needs a design.
- **Formats mlcroissant can't read.** GMT (rows end in a variable-length list), Excel sheets and SQLite tables can now be described and sampled by the skill, but mlcroissant still can't load them, so their records aren't tested through it.
- **Value rules and required fields.** Croissant has no native way to express these.
- **Conventions only our tools know.** The annotations and dialect properties above need a published vocabulary at the `rcv:` namespace for consumers.
- **Biolink's own inconsistencies.** Some slot ranges disagree with their definitions (`authors`, `genome_build`). The skill keeps a short list and follows the definitions.
- **Lists of entities.** A list column of ids (orthologs, interactors) is typed with a list separator, but not as a list of grouped entities (`isArray`). Not yet tested.
- **Where this runs.** It's a separate step that annotates an existing croissant. Folding it into the RoboCroissant agent is the next decision.
