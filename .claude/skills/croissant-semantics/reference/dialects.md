# File dialects

Semantics say what values mean. A dialect says how to read the file: the delimiter, comment
lines, whether there's a header row, quoting, what "no value" looks like. Many knowledge-base
files aren't plain CSV (VCF's `##` lines, GAF's `!` lines and missing header, CTD's commented
`# Fields:` block). If the croissant doesn't describe this, no tool can read the file without
guessing.

A dialect is written in established vocabulary wherever possible:

- **W3C CSV on the Web** dialect properties (`csvw:delimiter`, `csvw:commentPrefix`,
  `csvw:header`, `csvw:headerRowCount`, `csvw:skipRows`, `csvw:quoteChar`,
  `csvw:doubleQuote`, `csvw:null`).
- **The format's published spec** (VCF, GAF, GPAD/GPI, PSI-MI TAB), added to `encodingFormat`.
  For well-known formats this is the real definition. The CSVW properties are just enough of it
  for a generic reader to get the rows out.
- **Our conventions** for what CSVW can't say: `rcv:columnOrder` (`header`, or `field_order`
  when there's no header row) and `rcv:columnNames` (the names in file order, for
  `field_order` files).

## Named dialects

`scripts/dialects.json` lists known formats. Each entry has the CSVW properties, the media type,
the spec URL and the EDAM format where there is one.

| Name | Format |
|---|---|
| `csv`, `tsv` | Plain tables with a header row |
| `vcf-4` | VCF 4.x: `##` meta lines, `#CHROM …` header row |
| `mitab-2.7` | PSI-MI TAB 2.7: `#`-prefixed header row, `-` for no value |
| `mitab-2.7-comment-header` | PSI-MI TAB 2.7 after a `#` comment block whose last line names the columns (FlyBase, Alliance) |
| `gaf-2.2`, `gpad-2.0`, `gpi-2.0` | GO annotation files: `!` header lines, no column-name row |
| `ctd-csv`, `ctd-tsv` | CTD reports: `#` comment block, names on a commented `# Fields:` line, no column-name row |
| `flybase-tsv` | FlyBase precomputed files: `##` comment lines, a header row starting with `#` |
| `csv-noheader`, `tsv-noheader` | Plain tables with no header row (UniProt idmapping files) |
| `tsv-commented` | A `#` comment block (release, date), then an ordinary header row (Alliance downloads) |
| `tsv-comment-header` | A `#` comment block whose last line names the columns (Alliance MITAB) |
| `tsv-commented-noheader` | A `#` comment block, then rows with no header row: columns in the croissant's field order |
| `sssom-tsv` | SSSOM mapping sets: a `#`-prefixed YAML block, then a header row |
| `robot-template-tsv`, `robot-template-csv` | ROBOT templates: a header row, then a template row (`ID`, `LABEL`, `A rdfs:comment`, …), then data |
| `gmt` | Gene Matrix Transposed (GSEA, MSigDB, Enrichr): name, description, then any number of genes; no header row |

`dialects.py list` and `dialects.py show NAME` print them.

When a format you meet is well known and has a spec, add it to `dialects.json` rather than
describing it inline. Use the spec's own URL, check that the URL resolves, and add a test
reading a few real lines. Format families that are structurally identical (GAF, GPAD and GPI
all look the same to a reader) still get separate entries, because the spec says what the
columns *are*.

## Finding the dialect

```
scripts/dialects.py detect CROISSANT --layout ID
```

This reads the first lines of a file in the layout, parses them with every named dialect, and
scores how well the rows match the croissant's columns. A score of 1.00 means every row has
the right number of cells and, for header files, the right column names. Sparse rows don't
lower it.

Several dialects can score 1.00: GAF, GPAD and GPI are indistinguishable by structure, and a
FlyBase file parses the same with `vcf-4`, `ctd-tsv` and `flybase-tsv`. Pick the one whose spec
describes the file, not just one that parses it. `detect` marks with `*` the dialects named for
the knowledge base or file type, and lists them first among equal fits. It reads the head of a
plain file, or the archive a member is in, up to `--max-mb` (default 40).

`detect` also rejects two misreadings that otherwise score well: a no-header reading whose
first row is the croissant's column names (the file has a header row), and a header reading
whose first data row is a ROBOT template row. Among equal fits it prefers a dialect named for
the knowledge base, then the generic ones (`tsv`, `tsv-noheader`, ...) over other databases'
formats that happen to parse the file.

The readers strip a UTF-8 byte-order mark (Excel exports) and keep quoted fields that span
lines whole.

Two things the CSVW properties can't say, which named dialects handle:

- **A header row that starts with a comment mark** (`#CHROM`, FlyBase's `#gene_symbol`). It's
  read as the header; the mark is kept when the croissant's column name has it and dropped when
  it doesn't.
- **A header in the comment block**: `"header_in_comments": true` (written as
  `rcv:headerInComments`) reads the last comment line before the data as the header row. Blank
  comment lines and rules (`#`, `#-----`) after it don't count.
- **Backslash escapes**: `"doubleQuote": false` means a backslash escapes the quote character
  (`\"`), as CSVW defines it (PhenX exports).
- **A last column that takes the rest of the row** (GMT's genes): `"rest_column": true` in the
  dialect, written as `rcv:restColumn` (the column's name) on the file. The column should be
  `isArray` in the croissant.

`sample.py --dialect` takes a name, a `by_suffix` mapping, or an inline dialect as JSON
(`--dialect '{"based_on": "tsv", "csvw": {"commentPrefix": "%"}}'`).

## In the plan

A layout's `dialect` takes one of three forms:

```json
"dialect": "vcf-4"
"dialect": { "by_suffix": { ".csv": "ctd-csv", ".tsv": "ctd-tsv" } }
"dialect": { "based_on": "tsv", "csvw": { "commentPrefix": "%" }, "columns": "header" }
```

- **A name** from `dialects.json`.
- **`by_suffix`**, when a layout's record sets read files in several formats (CTD publishes each
  report as `.csv.gz` and `.tsv.gz`; a GO layout can mix `.gaf` and `.gpad`). Compression
  suffixes are ignored and the longest matching suffix wins. A file with no matching suffix is
  an error.
- **An inline dialect** for a one-off format. `based_on` starts from a named one; `csvw` keys
  override its properties; `columns` is required without `based_on`. If you write the same
  inline dialect twice, add it to `dialects.json` instead.

Set a dialect on every table layout whose files aren't plain CSV/TSV with a header row. That
means any file with comment lines, no header row, a commented header, a non-standard null, or a
format with its own spec. Also set one for any table file whose `encodingFormat` is only `application/gzip` (the check
warns about these): the dialect states the inner format, which is what lets mlcroissant read
it. For other plain CSV/TSV it's optional but harmless.

## What apply_plan writes

On every file (FileObject or FileSet) the layout's record sets read:

```json
{
  "encodingFormat": ["text/tab-separated-values", "application/gzip",
                     "https://geneontology.org/docs/go-annotation-file-gaf-format-2.2/"],
  "csvw:dialect": { "@type": "csvw:Dialect", "csvw:delimiter": "\t", "csvw:commentPrefix": "!",
                    "csvw:header": false },
  "rcv:columnOrder": "field_order",
  "rcv:columnNames": ["DB", "DB Object ID", "DB Object Symbol", "..."],
  "rcv:dialect": "gaf-2.2"
}
```

- `encodingFormat` leads with the content type. mlcroissant picks its reader from the first
  entry, and a gzipped table described only as `application/gzip` doesn't load.
- `rcv:columnNames` is taken from the input croissant before columns are grouped into entities,
  because grouping changes the field order.
- Two layouts giving one file different dialects is an error.

## What the checks do

- `check.py --plan-only` checks that every dialect named exists.
- The data check reads sampled files with the plan's dialect (not by guessing) and reports the
  fit. A fit under 0.9 is an error, with the reason (rows too wide, too narrow, names that don't
  match).
- `dialects.py read OUT --record-set ID` reads records using nothing but what the output
  croissant says about the file. If that works, the croissant describes the file well enough
  for any reader that follows CSVW.
- Rows ending in an empty extra cell (a trailing delimiter, as in CTD's TSVs) are not counted
  as too wide.

## What mlcroissant does with it

mlcroissant 1.1.1 ignores `csvw:dialect`. It reads files with a header row through pandas, so:

- A gzipped CSV/TSV with a header row loads once `encodingFormat` leads with the content type.
- A file with comment lines or no header row (VCF, GAF, CTD) still fails to load in mlcroissant,
  even though the croissant now describes it fully. The check reports these as pre-existing load
  failures. Supporting dialects in the reader is tracked in robo-croissant-pipeline#56; the
  sampled-data checks, which do use the dialect, are the evidence for those layouts.
