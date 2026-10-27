# What a plan becomes in Croissant

Everything here uses Croissant 1.1 constructs. Croissant accepts terms from any vocabulary
whose prefix is in `@context`; validators check structure, not meaning.

| Plan | Croissant |
|---|---|
| `row.type` | Record set `dataType` |
| `row.predicate`, layout `constants` | A field with a constant `value` and an `equivalentProperty` |
| entity | A field typed with the entity's class, `equivalentProperty` = its role, whose `subField`s are the member columns (moved, not copied; their `@id`s are unchanged) plus any constants |
| column `type` | Added to the field's `dataType`, after its atomic type (`sc:Text`, `sc:Integer`, ...) |
| column `property` | Field `equivalentProperty` |
| `id_prefix` with `id_form: bare` | An `annotation` on the field: `equivalentProperty sh:prefix`, `value` the prefix |
| `unit` | An `annotation`: `equivalentProperty qudt:hasUnit`, `value` the unit |
| `list_separator` | An `annotation`: `equivalentProperty rcv:listSeparator`, `value` the separator |
| `prefix_column` | An `annotation` `sh:prefix` whose value is read per row from the other column (its `source`) |
| `true_values`, `null_values` | `annotation`s `rcv:trueValue` / `rcv:nullValue`, one per value |
| `atomic` | Replaces the field's atomic `dataType` |
| `id_form` `url`, `mixed` or `native` | An `annotation`: `equivalentProperty rcv:idForm`, `value` the form |
| `codes` | A record set `codes_<hash>` typed `sc:Enumeration` with inline `data` (`name` → `url`), shared by every column (in any layout) with the same mapping, and `references` from the column to its `name` field |
| `local_terms` | A record set `rc_local_terms` typed `sc:DefinedTerm`, with inline rows: id, label, definition, SKOS matches, see-also |
| `prefixes` | Added to `@context` |
| layout `id` | `rcv:planLayout` on each record set the layout matched, so the croissant names its plan entry |

Example: an entity, as written into the croissant.

```json
{
  "@type": "cr:Field", "@id": "record_befaf09a/gene", "name": "gene",
  "dataType": ["biolink:Gene"], "equivalentProperty": "biolink:subject",
  "subField": [
    { "@id": "field_476dd6af", "name": "GeneID", "dataType": ["sc:Integer", "biolink:Gene"],
      "equivalentProperty": "biolink:id", "source": { "...": "unchanged" },
      "annotation": [{ "@type": "cr:Field", "@id": "field_476dd6af/id_prefix", "name": "id_prefix",
                       "dataType": "sc:Text", "equivalentProperty": "sh:prefix", "value": "NCBIGene" }] },
    { "@id": "field_dc10d52a", "name": "GeneSymbol", "equivalentProperty": "biolink:symbol", "source": { "...": "unchanged" } },
    { "@id": "record_befaf09a/gene/taxon", "name": "taxon", "equivalentProperty": "biolink:in_taxon", "value": "NCBITaxon:9606" }
  ]
}
```

Reading it back with mlcroissant gives nested records:
`{gene: {GeneID: 4524, GeneSymbol: MTHFR, taxon: NCBITaxon:9606}, disease: {...}, ...}`.

## Conventions that are ours, not Croissant's

Croissant has no native way to say these, so they're conventions built from standard parts.
Tools that don't know them still read the file; they just don't act on them.

- **`sh:prefix` annotation**: the column holds bare local ids; prefix them with this CURIE
  prefix and a colon.
- **`qudt:hasUnit` annotation**: the unit of a numeric column.
- **`rcv:listSeparator` annotation**: cells hold several values joined by this character.
- **`rcv:nullValue` / `rcv:trueValue` annotations**: values meaning "no value" / "true".
- **`rcv:idForm` annotation**: the ids are full IRIs (`url`) or a mix of CURIEs and bare ids
  (`mixed`), so no single prefix applies.
- **File dialects**: `csvw:dialect` (W3C CSVW properties) on a FileObject or FileSet, the
  format's spec URL in `encodingFormat`, and `rcv:columnOrder`, `rcv:columnNames`,
  `rcv:dialect` for what CSVW can't say. See `dialects.md`.
- **`rcv:headerInComments`** on a file: the last comment line before the data names the columns.
- **`rcv:restColumn`** on a file: the named column takes the rest of each row as a list (GMT).
- **`rcv:planLayout`** on a record set: the `id` of the plan layout that described it. A
  layout's `match.layout` fingerprint is computed from the original croissant's columns and
  can't be recomputed from the semantic one (columns move into entities), so this is the link
  from a record set back to its plan entry. Record sets no layout matched have none.
- **`rcv:` namespace** (`https://roboboulangerie.github.io/vocab/`): terms defined for one
  knowledge base, in `rc_local_terms`. Provisional; the namespace may move.

## mlcroissant 1.1.1 details the scripts already handle

- `references` must use the Croissant 1.0 form `{"field": {"@id": ...}}`. The 1.1 form
  `{"@id": ...}` crashes mlcroissant when it builds its graph.
- `value` must map to `sc:value` in `@context`; with `cr:value` the constant reads back empty.
- `annotation` needs `"annotation": "cr:annotation"` in `@context`; older RoboCroissant files lack it.
- Inline `data` rows must have every field of the record set, even when empty.
- Sub-fields can keep their original `@id`s; they don't need the parent's id as a prefix.
- Sub-fields with JSON-path sources fail to load (`Column "$.id" does not exist`), so entities
  over JSON-path columns are written as individually typed columns.
- `encodingFormat` must lead with the content type (`text/tab-separated-values`, then
  `application/gzip`); `application/gzip` alone doesn't load. `csvw:dialect`, `rcv:` properties
  and spec URLs in `encodingFormat` are tolerated but not used: files with comment lines or no
  header row still don't load.
- `@context` must define `data` as `{"@id": "cr:data", "@type": "@json"}`, or mlcroissant reads
  the rows of code tables and `rc_local_terms` as graph nodes and the validator rejects them.
  `apply_plan.py` sets it.
- Every `@id` must be unique. An entity, constant or derived field named like an existing column
  would clash and break mlcroissant with an unrelated-looking error, so `apply_plan.py` refuses
  it.
- `prefix_codes` becomes a `references` from the prefix annotation to a code table that maps each
  key to its namespace.
- Fields need an `@id`. Where the input croissant has none (BRCA Exchange), `apply_plan.py`
  generates `<record set>/<cleaned name>` ids and warns; it's a pipeline problem worth noting.
- `check.py --load-records` extracts archive members itself and points a small test croissant at
  the extracted file. mlcroissant can't open the pipeline's `archive.zip!/member` paths, and an
  archive's stale checksum would otherwise fail every member; neither says anything about the
  plan.
- An annotation may have a `source` instead of a `value` (used for `prefix_column`); it
  validates and loads.
