# Semantics plan format

A plan is a JSON file that says what the data in a croissant means. It's written per *layout*
(a set of column names shared by many record sets), refers to columns by name, and records for
every assertion why it was made and how sure we are. `apply_plan.py` turns it into Croissant;
the plan itself is kept next to the result as the record of what was asserted and why.

## Top level

```json
{
  "kb": "ctd",
  "croissant": "kbs/ctd/croissant.json",
  "biolink_version": "v4.4.6",
  "prefixes": { "NCBIGene": "http://identifiers.org/ncbigene/", "MESH": "http://id.nlm.nih.gov/mesh/" },
  "local_terms": [ ... ],
  "column_library": { "pubmed_ids": { "property": "biolink:publications", "id_prefix": "PMID", "list_separator": "|" } },
  "layouts": [ ... ],
  "skipped": [
    { "what": "layout L1234abcd (release notes)", "reason": "free text; nothing to type" },
    { "what": "ontology-specific columns outside the has_columns match", "reason": "vary per file; not sampled" }
  ]
}
```

- `prefixes`: every CURIE prefix used anywhere in the plan (types, properties, id prefixes,
  code mappings, constant values) **and** every prefix that appears in the data's CURIE values.
  `biolink`, `skos`, `sh`, `qudt`, `unit`, `rcv`, `infores`, `csvw`, `rdfs`, `sc`, `cr` are built in.
  Use the expansion Biolink uses (`vocab.py prefix X`); if Biolink doesn't have the prefix, use
  Bioregistry's URI format up to the `$1`. The key is the prefix as the data writes it (`MGI`, not
  `mgi`), which is what checks compare values against; the expansion is what makes it
  resolvable. When the data's spelling differs from Biolink's, declare the data's.
- `column_library`: column specs defined once by name, for knowledge bases whose many layouts
  share columns (snapshots that add columns over time). Use one in a layout with
  `"PubMedIDs": { "use": "pubmed_ids" }`; other keys next to `use` override the library's, and
  an override of `null` removes a key (`{"use": "pubmed_ids", "list_separator": null}` for a
  JSON layout where the values are already lists).
- Many layouts that are subsets of one column set (the same records as TSV and as JSON with
  different keys): mark library columns and entities `"optional": true` and use one layout entry
  per group of layouts; optional columns are typed where a record set has them and skipped where
  it doesn't. If the layouts still differ too much, generate the entries with a small script in
  `OUT/work/` from `inventory.py --json` and the library, and keep the script.
- Prefix expansions: use Biolink's, else Bioregistry's URI format up to `$1`. When neither
  resolves the knowledge base's ids (a format pointing at the wrong record type or site), use
  the knowledge base's own URL pattern and say why in `basis` of a column that uses the prefix.
  When a URI format has fixed text after `$1` and so can't be an expansion, use
  `https://bioregistry.io/<prefix>:` (Bioregistry resolves it).
- Prefix expansions must end in `/`, `#`, `_`, `:` or `=` so CURIEs expand to valid IRIs.
  Some Biolink expansions lack the delimiter (`CLINVAR`); add it. Some Bioregistry URI formats
  have no delimiter before `$1` either (PDB's ends in `pdb_0000`, which only works for padded
  ids). Then use a URL that takes the id after a delimiter: the knowledge base's own link or the
  database's (`https://www.rcsb.org/structure/`), else `https://bioregistry.io/<prefix>:`.
- `skipped`: what you deliberately left alone — layouts, record sets, groups of columns —
  each with a reason. Not used by `apply_plan.py`; it documents the gaps.
- `explained`: warnings you checked and that stand, so the report doesn't keep raising them:
  `[{"where": "disease_annotations", "message": "biomarker_for", "reason": "a cross-tab of the whole file shows these codes only on gene rows"}]`.
  - `where` and `message` are substrings of the warning's location and text; a matching warning
    becomes a note carrying the reason. Copy them from `check_report.findings.json`, which has
    each finding's own location. The report merges identical warnings into one line
    ("A, B"); a `where` copied from such a line matches each of its places.
  - When several entries match, the most specific one (longest `where` and `message`) wins, so
    an entry for `X column_6 -> knowledge_level` beats one for `X column_6`.
  - One entry per place you checked: an entry that matches more than one warning is reported,
    because a loose entry can hide a new warning somewhere else.
  - Only for warnings you verified, with the evidence in the reason; never to hide one you didn't
    look into. Entries that match nothing are reported.
  - An error can be explained only by an entry with `"level": "error"`, and only when the error
    is false because of a limit of the scripts (they read the wrong sheet, can't read the format)
    and you checked the data another way; say how in the reason.

## Layouts

```json
{
  "id": "gene_disease_curated",
  "match": { "layout": "L02d92483" },
  "summary": "One row per gene–disease relationship curated by CTD.",
  "row": {
    "type": "biolink:GeneToDiseaseAssociation",
    "predicate": "biolink:associated_with",
    "basis": "Each row pairs one gene with one disease plus evidence and PubMed ids.",
    "source": "documentation", "confidence": "high"
  },
  "entities": [ ... ],
  "columns": { ... },
  "constants": [ { "name": "knowledge_source", "property": "biolink:primary_knowledge_source", "value": "infores:ctd" } ]
}
```

- `id`: short, unique in the plan, snake_case.
- `match` (required): `layout` is the id from `inventory.py`, or `layouts` a list of them (one entry for
  the same kind of file whose columns changed across releases); or `has_columns`, a list of column
  names, to match every record set that has all of them (for knowledge bases with hundreds of
  layouts that share core columns, see `inventory.py --common-columns`). Columns outside the
  `has_columns` list may still be used if they're marked `optional`. Add `record_set_name` (a regex on
  record set names) or `record_sets` (ids or names) to apply the layout to only some of those
  record sets — needed when record sets share columns but differ in meaning (for example
  separate files for biological process, cellular component and molecular function). The most
  specific match wins (`record_sets`, then `record_set_name`, then `layout`, then `has_columns`;
  among otherwise equal `has_columns` matches, the one listing more columns); two equally specific matches for one
  record set are an error. That gives a **fallback pattern**: a broad entry (`layout`, or
  `has_columns` with the core columns) types what every record set shares, and more specific
  entries (`record_set_name`, `record_sets`) override it for the record sets that need more. A
  record set gets exactly one entry, the most specific, so the specific entry must repeat
  anything from the broad one it still wants (a `column_library` makes that cheap).
- `row` (optional): what one row is. Two patterns:
  - **Rows are relationships** (gene–disease pairs): `type` is an association class, entities
    have roles such as `biolink:subject` and `biolink:object`. `predicate` is only for a
    predicate that is the same for every row; when it varies, map the column that holds it
    (see `codes`).
  - **Rows are things** (a variant table, a gene list): `type` is the thing's class
    (`biolink:SequenceVariant`). Map the thing's own columns in `columns` with its properties
    (`biolink:id`, `biolink:name`, ...), and group the *other* things a row mentions into
    entities whose `role` is the property linking them to the row's thing
    (`biolink:has_gene` on a gene entity).
  A local class (`rcv:<kb>/EvidenceItem`) is allowed as a row type when no established class
  fits, for example a knowledge base's core curated record. Define it in `local_terms`, linked
  to the nearest class (`broad_match: biolink:Association`).
- `encoding_format`: replaces the encodingFormat of the layout's files, content type first
  (`["text/tab-separated-values", "application/gzip"]`, `["application/json", "application/gzip"]`,
  `"application/x-parquet"`). For files the generator labelled wrongly (`text/plain` for a TSV,
  gzip alone for JSON, `application/parquet`), which mlcroissant then can't read. Say why in the
  layout's `summary`, and report it. A dialect also sets the content type for tables; use this for
  the rest, or to override it.
- `dialect`: how to read the layout's files. A layout with only a dialect (and `atomic` fixes)
  is worth having: it makes files readable even where nothing else is typed. The report counts
  files with a dialect separately from record sets with semantics. The value is a name from
  `scripts/dialects.json` (`vcf-4`, `gaf-2.2`, `ctd-csv`, ...), `{"by_suffix": {".csv": "ctd-csv", ".tsv": "ctd-tsv"}}` when the
  layout's files come in several formats, or an inline dialect. Set it for every table layout
  whose files have comment lines, no header row, or a format with its own spec. Find it with
  `dialects.py detect`. See `dialects.md`.
- `entities`: groups of columns that together describe one thing. See below.
- `columns`: columns that aren't part of an entity. See below.
- `constants`: fixed values for every row of the layout, added as fields with a constant value
  (a CURIE, a literal, or an enum value with the `biolink:` prefix, e.g.
  `{"name": "knowledge_level", "property": "biolink:knowledge_level", "value": "biolink:knowledge_assertion"}`).
  When the value differs by file and is in the record set's name (a scoring file per score),
  add `name_pattern`, a regex on the record set name, and write the value as a template:
  `{"name": "score", "property": "rcv:pgs/score", "name_pattern": "(PGS\\d{6})", "value": "pgs:{1}"}`
  (`{1}` is the first group, `{0}` the whole match). A record set the pattern doesn't match is an
  error. When the name holds a key rather than the value (a species name for a taxon), add
  `value_map` from the first group to values instead of a template:
  `{"name": "taxon", "property": "biolink:in_taxon", "name_pattern": "^(\\w+?)_", "value_map": {"human": "NCBITaxon:9606", "mouse": "NCBITaxon:10090"}}`.
  One entry with a `value_map` replaces one entry per species.

Every column you don't mention is left as it is. Mention only what you can support.

## Entities

```json
{
  "name": "gene",
  "type": "biolink:Gene",
  "role": "biolink:subject",
  "description": "The gene, by NCBI Gene id and symbol.",
  "columns": {
    "GeneID":     { "property": "biolink:id", "id_prefix": "NCBIGene", "id_form": "bare" },
    "GeneSymbol": { "property": "biolink:symbol" }
  },
  "constants": [ { "name": "taxon", "property": "biolink:in_taxon", "value": "NCBITaxon:9606" } ],
  "basis": "GeneID is a numeric NCBI Gene id (values like 4524); CTD documents it as NCBI Gene.",
  "source": "documentation", "confidence": "high"
}
```

- `name`: the group's field name (`gene`, `disease`, `chemical`, `subject`, ...).
- `type`: the entity's class. Use the most specific class the data supports.
- `role`: the property of the row the entity fills: an association slot (`biolink:subject`,
  `biolink:object`) when rows are relationships, or a property of the row's thing
  (`biolink:has_gene`) when rows are things. Omit only when the row has no type.
- `columns`: column name → column spec (below). Mark exactly one column `biolink:id`, or, for an
  entity known only by name or symbol (no id column at all), give `"no_id": "why"` instead.
- `constants`: as for layouts, including `name_pattern` and `value_map`.
  It automatically gets the entity's type as a second data type.
- `constants`: values that hold for every row but aren't in the data, such as the taxon of a
  single-species file.

A column can be in at most one entity, and not also in `columns`.

## Column specs

Used both in `columns` and in an entity's `columns`.

| Key | Meaning |
|---|---|
| `type` | Semantic type(s) of the values (CURIE or list). An atomic type is kept automatically. |
| `property` | The property this column fills: of the entity (`biolink:id`, `biolink:name`, `biolink:symbol`, `biolink:in_taxon`) or of the row (`biolink:publications`, `biolink:has_evidence`, `biolink:p_value`, ...). |
| `id_prefix` | The CURIE prefix of the identifiers in the column. |
| `id_form` | `bare` (values like `4524`, prefix must be added), `curie` (values like `MESH:D003920`), `obo` (OBO short forms like `MONDO_0004989`: prefix, underscore, local id; every prefix must be in `prefixes`), `mixed` (some of each), `url` (full IRIs, possibly from many namespaces), or `native` (the source's own tagged strings that look like CURIEs but aren't: MITAB `entrez gene/locuslink:613`, `psi-mi:"MI:0018"(two hybrid)`, HGVS `NC_000017.11:g.43044295del`). Default `bare` when `id_prefix` is set. `url`, `mixed`, `native` and `obo` are recorded in an annotation; `native` columns aren't checked for undeclared prefixes, and their tags shouldn't be declared as prefixes. |
| `list_separator` | The text joining several values in one cell (`"\|"`, `";"`, `", "`: several characters are fine; items are trimmed). |
| `item_pattern` | A regular expression for compound list items (`HP:0001250\|Seizure`, `TP53\|7157\|SRC`) whose first group is the id: the checks check that group (prefixes, patterns, codes). Use with `list_separator` or `list_format`; an item the pattern doesn't fit is checked whole, so it shows as a misfit. |
| `list_format` | `json` or `python` for cells written as list literals (`["a", "b"]`, Monarch's `['PMID:1', 'PMID:2']`); the checks parse each cell and check its items. Use instead of `list_separator`. |
| `unit` | Unit of measure for the whole column: a QUDT unit CURIE (`unit:NanoMOL-PER-L`), (`unit:BasePair` for genomic distances), or a UO term where QUDT has none. Find QUDT units with `vocab.py unit "nanomolar"`. The check confirms it exists. When the unit differs by row (a column of unit labels or UCUM codes), map that column with `codes` to `unit:` CURIEs instead; those are checked too. |
| `codes` | `{ "value in the data": "CURIE or URL" }` for columns that hold a small set of codes. With `list_separator`, the codes apply to each item of the list. An empty cell can't be mapped: it means no value. |
| `atomic` | Replace the column's basic type when the input croissant has it wrong, e.g. `sc:Text` for ids typed `sc:Integer` that have leading zeros or letters. Say why in `basis`; also list it in `notes.md` as a problem for the pipeline. |
| `prefix_column` | The column that holds each row's id prefix (GAF's `DB` column for `DB Object ID`). Use with `id_form: bare` or `mixed`; the check validates each id against its own row's prefix. Every prefix that column holds must be in `prefixes`. |
| `prefix_codes` | With `prefix_column`, when that column holds namespace keys rather than prefixes (GlyGen's `protein_xref_omim`, UniProt idmapping's `ID_type` value `MIM`): `{ "key": "PREFIX" }`. Each prefix must be in `prefixes`. The croissant gets a code table mapping each key to the prefix's namespace, and the check validates each id against its own row's prefix. Keys missing from the map are reported; map a key to `null` when its values aren't ids in any namespace (Ensembl's `EntrezGene_trans_name`), and they're skipped. |
| `pattern_note` | Why values may legitimately not fit the registry's id pattern (e.g. "CGD ids have 10 digits; Bioregistry's pattern expects 7"). Turns pattern mismatches into warnings. Only after checking the data and the source. |
| `true_values` | For yes/no columns: values that mean true (`["NOT"]` in a negation column, `["Y"]`). Empty or other values mean false. Use with a boolean property such as `biolink:negated`. |
| `optional` | The column may be missing from some matched record sets; skip it there instead of failing. Also allowed on an entity (skipped when none of its columns are present). |
| `null_values` | Values that mean "no value" (`["N/A", "-"]`). Recorded in the croissant and ignored by the checks. Set these instead of dropping an `id_prefix` because of placeholders. |
| `derived` | Extra fields read from this same column, each with its own `name` (new, unique), `property` and the usual keys (`codes`, `true_values`, `null_values`, ...). For a column that says two things, like a predicate column whose `is_not_implicated_in` also means the statement is negated. A derived field inherits the column's `list_separator`, `list_format` and `null_values` unless it sets its own. |
| `description` | A note on the column (how a list is joined, what a placeholder means); added to the field's description. |
| `basis`, `source`, `confidence` | Why, from where, how sure (required on every top-level column spec; on entity members they're optional, the entity's apply). |

A column spec needs no `property`. One with only `id_prefix`, `id_form` or `list_separator` (an id
column whose role in the row you can't establish, a list column) is fine, and still useful; so is
one with only `codes` (a gene-type column mapped to Sequence Ontology classes, when Biolink has no
slot for it).

An id column that mixes namespaces with no column saying which (a registered prefix and the
source's own keys, `IMPC-CURATE-…`; KEGG's `path:map00010`) is `id_form: native` with no
`id_prefix`, and `description` says what the values are. `mixed` is for a single prefix whose
values are written sometimes as CURIEs and sometimes bare.

**Lists within lists.** When a cell holds groups joined by one separator and items within a
group joined by another (UniProt peptide accessions: `,` within a gene group, `;` between
groups), set `list_separator` to the outer one and say in `description` how the inner items
are joined. There's no two-level separator. Lists wrapped in brackets or braces
(`{11301, 11305}`, `[a, b]`) get the separator inside them, with the wrapper described in
`description`.

For `curie` columns, set `id_prefix` only when every value uses that one prefix; the check
then confirms it. Put every prefix the values use in the plan's `prefixes`.

When the prefix depends on another column and that column holds the prefix itself (GAF `DB`:
`FB`, `MGI`, `UniProtKB`), use `prefix_column`. When the other column only implies it (a
`source_type` of `PubMed` or `ASCO` decides whether a citation id is a PubMed id or a DOI), set
`id_form: mixed` with no `id_prefix`, say which column decides in `basis`, and list it in
`notes.md`.

### A column of source names

Many knowledge bases have a column naming where each row came from (`ChEMBL`, `PubChem`,
`CSAR`). Map it with `codes` to infores ids and `property: biolink:primary_knowledge_source`
(or `biolink:aggregator_knowledge_source` when the knowledge base gathered rather than curated
it). Look up each source with `vocab.py infores NAME` (it searches names, synonyms and URLs;
try the full name too: CSAR is `infores:community-sar`). The check rejects ids that aren't in
the catalog. For a source with no infores id, leave it unmapped. The check warns about it, and `notes.md` lists it as a question for the maintainers
(a new infores entry is the fix). Don't invent infores ids, and don't make local terms for
sources unless the knowledge base documents them as its own.

### Repeated groups of columns

Some files repeat a block of columns per item (BindingDB has chain 1..50, each with a UniProt
id, a PDB id and a name). Type the first block properly. If the others matter, write the plan's
layout entry with a small script in `OUT/work/` that repeats the block's specs for each number,
keep the script, and say in `notes.md` which block is the entity (usually the first) and why.

### A code column as the predicate

When a column says which relationship each row asserts, map it to predicates with `codes` and
`property: biolink:predicate`:

```json
"DirectEvidence": {
  "property": "biolink:predicate",
  "codes": { "marker/mechanism": "biolink:gene_associated_with_condition",
             "therapeutic": "biolink:target_for" },
  "null_values": [""],
  "basis": "...", "source": "agent", "confidence": "medium"
}
```

When some codes are negations (`is_not_implicated_in`), map each negative code to the positive
predicate it negates, and say that the statement is negated in a separate field with
`derived`:

```json
"AssociationType": {
  "property": "biolink:predicate",
  "codes": { "is_implicated_in": "biolink:contributes_to", "is_not_implicated_in": "biolink:contributes_to" },
  "derived": [ { "name": "negated", "property": "biolink:negated",
                 "true_values": ["is_not_implicated_in"],
                 "basis": "...", "source": "documentation", "confidence": "high" } ],
  ...
}
```

Biolink enum values work as codes for any slot whose range is an enum (`knowledge_level`,
`agent_type`, `causal_mechanism_qualifier`, `druggable_gene_category`, qualifier slots): write
them with the `biolink:` prefix. `vocab.py enum SLOT_OR_ENUM` lists them, and the check reports
a code that isn't one of them.

For rows that are predictions rather than direct assertions (`…_via_orthology`, IEA), mark them
with a derived field: `biolink:knowledge_level` takes enum values, given as codes with the
`biolink:` prefix, e.g. `{"name": "knowledge_level", "property": "biolink:knowledge_level",
"codes": {"implicated_via_orthology": "biolink:prediction", "is_implicated_in": "biolink:knowledge_assertion"}}`
(KGX writes these as bare values; in a code table they need the `biolink:` prefix to be CURIEs).
`knowledge_level` values: `knowledge_assertion`, `logical_entailment`, `prediction`,
`statistical_association`, `text_co_occurrence`, `observation`, `not_provided`. `agent_type`
values: `manual_agent`, `automated_agent`, `data_analysis_pipeline`, `computational_model`,
`text_mining_agent`, `image_processing_agent`, `manual_validation_of_automated_agent`,
`not_provided`.

Rows where the column is empty (CTD's inferred rows) have no predicate from this column; say
in `basis` and `notes.md` what they mean. Don't also set `row.predicate`.

## Local terms

For meanings with no term in Biolink or an established vocabulary.

```json
{
  "id": "rcv:ctd/DirectEvidence",
  "label": "CTD direct evidence",
  "definition": "Whether CTD curators found the gene to be a marker/mechanism or a therapeutic target for the disease.",
  "see": "https://ctdbase.org/help/glossary.jsp",
  "broad_match": "biolink:has_evidence",
  "close_match": ["NCIT:C25424"],
  "exact_match": [],
  "narrow_match": []
}
```

- `id`: `rcv:<kb>/<Name>`. Use UpperCamel for classes, lower_snake for properties and codes.
- `definition`: from the knowledge base's own documentation where possible; cite it in `see`.
- Match keys take one CURIE or a list of them.
- Link to the nearest established term with the SKOS match that's actually true. Omit match
  keys that don't apply (don't write empty strings). A local term with no true match is allowed;
  say why in `no_match_reason` (`check.py` warns about unlinked terms without one).

## Assertion fields

Every `row`, entity and top-level column spec carries:

- `basis`: one or two sentences: what in the data or documentation supports this. Quote sample
  values or the documentation.
- `source`: `documentation` (the knowledge base's own docs or data dictionary — cite the URL in
  `basis`), `data_dictionary` (a schema shipped with the files), `agent` (inferred from names
  and values), or `maintainer`. Nothing else: what you saw in sampled data is `agent`, with the
  values quoted in `basis`.
- `confidence`: `high` (the values and documentation agree, or the meaning is unmistakable from
  the values), `medium` (consistent with the values but the meaning is inferred), `low` (a
  plausible reading that a maintainer should confirm).
