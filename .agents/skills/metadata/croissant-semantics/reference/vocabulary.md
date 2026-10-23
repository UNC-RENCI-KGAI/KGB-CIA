# Choosing terms

We can't count on everything mapping cleanly to Biolink. For each entity, row and column, use
the first option that truly fits:

1. **Biolink** (`vocab.py class|slot|search`). The common case for genes, proteins, chemicals,
   diseases, phenotypes, anatomy, pathways, variants, publications, and their associations.
2. **Another established vocabulary** when Biolink has no term:
   - OBO ontologies via `vocab.py ols` — SO (sequence feature types), RO (relations), ECO
     (evidence types), UBERON (anatomy), OBI (assays), STATO (statistics), NCIT.
   - QUDT for units (`unit:MilliGM-PER-L`), or UO.
   - EDAM for data types and formats.
   - schema.org or Wikidata for general things.
3. **A local term** (`rcv:<kb>/...`) defined in `local_terms`, from the knowledge base's own
   documentation, linked to the nearest established term with the SKOS match that is true
   (`broad_match` when the established term is more general, `close_match` when it's nearly
   the same).
4. **Nothing.** Leave the column untyped. A good description beats a wrong type. Don't fall
   back to `biolink:NamedThing`, `biolink:related_to` or `biolink:Association` just to type
   something: that loses information and looks more certain than it is.

Grouping columns into entities and marking ids, prefixes and units is worth doing even when
the type only reaches option 3 or 4.

## Biolink pointers

- Version is pinned in `vocab.py` (v4.4.6). Class CURIEs are `biolink:UpperCamel`; slots are
  `biolink:lower_snake`.
- `vocab.py class X` lists the class's `id_prefixes`, preferred first. Use those prefixes for
  its ids where the data allows (`NCBIGene`, `HGNC`, `FB`, `MGI` for genes; `MONDO`, `DOID`,
  `MESH`, `OMIM` for diseases; `CHEBI`, `PUBCHEM.COMPOUND`, `MESH` for chemicals). If the data
  uses another prefix, keep the data's prefix; don't rewrite identifiers.
- Row types: pick the most specific association whose subject and object classes match your
  entities: `vocab.py associations --subject Gene --object Disease` lists them, closest first
  (`vocab.py class` shows `slot usage` for one class). Class names are exact
  (`ChemicalEntityToDiseaseOrPhenotypicFeatureAssociation`); `vocab.py class` suggests near
  misses. If none fits, use a
  less specific one that does (`biolink:Association` only as a last resort, at `low`).
- Common entity properties: `biolink:id`, `biolink:name`, `biolink:symbol`,
  `biolink:synonym`, `biolink:description`, `biolink:in_taxon`, `biolink:xref`.
- Common row properties: `biolink:publications`, `biolink:has_evidence`,
  `biolink:has_evidence_of_type`, `biolink:knowledge_source`,
  `biolink:primary_knowledge_source`, `biolink:p_value`, `biolink:adjusted_p_value`,
  `biolink:has_confidence_score`, `biolink:update_date`, `biolink:qualifiers`.
- Predicates are slots that descend from `biolink:related_to` (`vocab.py slot X` shows the
  `is_a` chain). Choose the most specific one the knowledge base's own wording supports.

## Identifiers

- Look at the values first (`sample.py`). CURIE-shaped values (`MESH:D003920`) are `curie`;
  bare values (`D003920`, `4524`) are `bare` and need `id_prefix`.
- For bare values with letters, `vocab.py identify VALUE ...` lists registered prefixes whose
  patterns fit, most specific first; give it values from one id family at a time (it splits
  differently shaped values for you). For ontology terms, `vocab.py term ECO:0000315` looks up
  an id exactly; `vocab.py ols` free-text search often misses exact labels, and searching one
  ontology (`--ontology obi`) works much better than searching all. For plain integers the
  prefix can only come from the column name and the
  knowledge base's documentation — say which in `basis`.
- `vocab.py prefix X` gives Biolink's expansion and Bioregistry's pattern and URI format.
  Put the expansion in the plan's `prefixes`.
- Pipe- or semicolon-joined lists of ids are common (PubMed ids). Set `list_separator`.
- **A generic prefix for mixed-type ids.** When one id column holds a knowledge base's
  accessions for several kinds of things (PharmGKB `PA…` ids for genes, chemicals and
  variants alike), the registry's generic prefix for the knowledge base (`pharmgkb`) is the
  honest one, even though Biolink lists type-specific prefixes. Use the type-specific prefix
  only where the column holds one type.
- **No registered id.** A table of things whose only id is the knowledge base's own string key
  (a BRCA Exchange variant key) can still mark that column `biolink:id`, with
  `id_form: native` and the meaning in `basis`. Don't invent a prefix.
- A column spec may carry only `id_prefix` (and `list_separator`) with no `property`, for an id
  column whose role in the row you can't establish; the prefix is still useful.

## Things that are easy to get wrong

- **Same columns, different meaning.** Files that share a layout can still differ: GO process
  vs component vs function files, per-species files (different taxon), forward vs reverse
  tables. Split them with `record_set_name`.
- **Names that look like ids.** A column called `ID` may be a row number. Check the values.
- **Symbols vs names.** Gene symbols (`MTHFR`) are `biolink:symbol`. A full name
  ("methylenetetrahydrofolate reductase") is `biolink:full_name` when the table also has a
  symbol; use `biolink:name` for the main label of things that have no symbol (diseases,
  chemicals).
- **Counts and scores.** `InferenceGeneQty` is a count, not an id. Only type numeric columns
  when there's a fitting property (`p_value`, a score) or a unit; otherwise leave them.
- **Codes.** Map only codes you've seen in the data or the documentation. The check warns about
  sampled values your mapping misses.
- **Class that varies by row.** When a code column decides the entity's class (GO terms that
  are processes, functions or components depending on an `Aspect` column), use the most
  specific class covering all of them — for mixed GO branches that is `biolink:OntologyClass`,
  which is also the object range of `biolink:FunctionalAssociation`. If the files split by the
  code, use separate layouts with `record_set_name` and the specific classes. Map the code
  column itself with `codes` to the branch classes.
  When the possible classes share only a mixin (a subject that is a gene, an allele or a
  genotype), the mixin is the type (`biolink:GenomicEntity`); map the column that says which
  with `codes`.
- **One thing spread over alternative columns.** When a row's object is in one of two columns
  (an expression site that is an anatomy term in one column or a GO cellular component in
  another, with the other empty), make two entities, one per column, each with the same role,
  and say in `basis` that each row fills one of them. The same goes for an entity's **id**:
  when the id is in one of two columns (a UniProt accession under SwissProt for some rows and
  TrEMBL for others), make two entities with the same role, one per id column, rather than
  filing the second as an xref and leaving rows without an id. The check warns when more than
  5% of sampled rows have no id.
- **Association classes constrain their subject and object.** `vocab.py class X` shows the
  slot usage: `PairwiseMolecularInteraction` wants molecular entities, so gene-typed
  interactors need `PairwiseGeneToGeneInteraction` (or protein-typed entities). Predicates
  have domains and ranges too (`biomarker_for` is for chemicals and gene products, not
  alleles). Check both before picking; the check warns when an entity's class doesn't fit.
- **Negated and inferred rows aren't direct ones.** Codes like `is_not_implicated_in`,
  `implicated_via_orthology`, `predicted`, or GO's `IEA` evidence mark rows that don't assert the
  same thing as their direct counterparts. Don't map them to the same predicate with nothing
  else to tell them apart: add a `derived` field (`biolink:negated` with `true_values`, or
  `biolink:knowledge_level`/`agent_type` codes for predictions), split them into their own
  layout, or leave them unmapped and say so. The check warns about merged codes.
- **Measurements that report no effect.** Censored values (`>100000` nM, `<0.01`) and "not
  active" rows don't support a predicate that asserts an effect (`directly_physically_interacts_with`).
  Use a weaker predicate for the whole layout (`interacts_with`), or say in `basis` and
  `notes.md` which rows the predicate overstates.
- **Is the row really the thing?** Before typing rows as a thing with an id column, check that
  the id is unique per row. Rows that repeat it (an allele listed once per variant) are
  relationships: type them as an association, or as the thing with the repeated parts as
  entities, and say which. The check warns when sampled row ids repeat, but a sample of the
  first rows can miss repeats spread through a big file: confirm with
  `sample.py --record-set ID --profile ID_COLUMN --top 3`, where any count above 1 is a repeat.
- **Code values are concepts.** A local term for a code value (`hm_source_liftover`) links with
  SKOS to a class or concept, or to nothing with a `no_match_reason`; never to a property
  (`broad_match: rcv:.../hm_source` or a Biolink slot). Give the property itself, on the column,
  instead.
- **Tagged strings aren't CURIEs.** MITAB ids (`entrez gene/locuslink:613`, `taxid:9606(human)`,
  `psi-mi:"MI:0018"(two hybrid)`) and HGVS expressions look like CURIEs but aren't. Mark the
  columns `id_form: native` and don't declare their tags as prefixes; `in_taxon` needs real
  taxon CURIEs, so leave a `taxid:...(name)` column untyped or say why. The check warns about
  declared prefixes that Biolink spells differently (`pubmed` vs `PMID`).
- **OBO short forms** (`MONDO_0004989`, `EFO_0000270`) are `id_form: obo`, not `native`;
  declare each ontology's prefix.
- **Sources of a thing vs of a statement.** `primary_knowledge_source` is a single-valued slot
  of associations. For rows that are things, a column listing the databases a record came
  from is `biolink:provided_by` (multi-valued), mapped to infores ids with `codes`.
- **Roles with no Biolink slot.** When a row links to a thing in a way no Biolink slot covers
  (an allele used in a disease model, the evidence an ortholog call rests on), use a local
  property (`rcv:<kb>/model_allele`) defined in `local_terms` with a `broad_match` to the
  nearest Biolink slot. Linking a property to a property is right here (it's code values that
  shouldn't link to properties).
- **Evidence columns that mix codes and text.** When a column holds evidence codes for some
  rows (`IEA`) and sentences for others ("modeled by …"), map the codes with `codes`, leave
  the sentences unmapped, and say in `basis` which rows are predictions (IEA) so the
  negated-or-inferred rule above applies.
- **Interactions between proteins.** In Biolink 4, `Protein` isn't a `MolecularEntity`, so
  `PairwiseMolecularInteraction` (molecular entities on both sides) doesn't fit protein–protein
  or gene–gene interactions; use `PairwiseGeneToGeneInteraction` (gene or gene product).
- **Taxonomy tables.** A row that is a taxon (a species, a virus isolate or strain) is
  `biolink:OrganismTaxon`; Biolink uses the class for strains and isolates too. Lineage
  columns that hold rank names without ids (Realm, Family, Genus, ...) can't be linked to taxon
  ids; type them with local properties per rank (or leave them untyped), and the rank itself
  with TAXRANK terms (`biolink:has_taxonomic_rank`) where a column names it. Don't invent
  lineage ids.
- **Ontology repositories.** Some knowledge bases are an ontology's source repository (OBO
  repos): ROBOT templates, SSSOM mapping sets, term lists, SPARQL query results.
  - A ROBOT template's second row says what each column becomes (`A rdfs:label`,
    `SC %` for subclass of, `AI IAO:0000115`). It is a data dictionary: read it, cite it as
    `source: data_dictionary`, and use the `robot-template-*` dialect so it isn't read as data.
  - SSSOM rows are mappings: row type `sssom:Mapping` is fine (declare `sssom`), with
    `subject_id` and `object_id` as entities whose role is `sssom:subject_id` /
    `sssom:object_id`. Non-Biolink row types (`owl:Class`, `sssom:Mapping`) are allowed;
    roles then come from the same vocabulary.
  - Restriction columns of templates (fillers given as labels) can stay untyped.
- **Data dictionaries.** Some knowledge bases publish only variable definitions (PhenX, CADRE):
  each row is a variable. Type rows `biolink:StudyVariable`, mark the variable name `biolink:id`
  (`id_form: native` when it has no registered prefix; check that it's unique), the text
  `biolink:description`, and the other dictionary columns (type, units, permissible values,
  cohort) with local properties. NCIT's ISO 11179 terms (Data Element, Permissible Value) are
  classes, not properties, so a local property can't be linked to them with SKOS: give the class
  in the definition and a `no_match_reason`.
- **Association-style JSON** (KGX edges, `{"subject": ..., "predicate": ..., "object": ...}`):
  entities can't be grouped over JSON paths, so the subject's and object's categories go on the
  row as `biolink:subject_category` / `biolink:object_category` columns.
- **Binding affinities** (Ki, IC50, Kd, EC50 in nM). Biolink's `pKi`, `pIC50` and similar slots
  hold −log10 values, so raw concentration columns don't fit them. Type the column with the
  BAO term for the measure (`BAO:0000192` Ki, `BAO:0000190` IC50, `BAO:0000034` Kd,
  `BAO:0000188` EC50) and the unit
  (`unit:NanoMOL-PER-L`), with no property or a local one. Censored values (`>10000`) mean no
  measurable activity: see "Measurements that report no effect".
- **Evidence codes and knowledge level.** GO evidence codes (and the ECO terms GPAD uses for
  them) say how a statement was made, which maps onto `knowledge_level` and `agent_type`. As a
  starting point (check GO's evidence code guide for the file at hand):
  - experimental (EXP, IDA, IPI, IMP, IGI, IEP, HTP, HDA, HMP, HGI, HEP) and author statements
    (TAS, NAS): `knowledge_assertion`, `manual_agent`
  - curator inference (IC): `logical_entailment`, `manual_agent`
  - curated computational analysis (ISS, ISO, ISA, ISM, IGC, IBA, IBD, IKR, IRD, RCA):
    `prediction`, `manual_validation_of_automated_agent`
  - electronic annotation (IEA): `prediction`, `automated_agent`
  - no data (ND): `not_provided`

  Map them with `derived` fields on the evidence column. GPAD files give ECO ids instead; GO's mapping between the two is
  http://purl.obolibrary.org/obo/eco/gaf-eco-mapping.txt.
- **RO relations.** GPAD and some other files give relations as RO ids. `vocab.py search
  RO:0002331` finds the Biolink predicate mapped to one (`actively_involved_in`); when none is,
  use the nearest broader predicate and say so in `basis`.
- **Gene sets.** A set of genes (MSigDB, GMT files) relates to its members with
  `biolink:has_gene_or_gene_product`, not `has_participant` (which needs a process).
- **Dates.** Biolink's date slots (`update_date`, `creation_date`) take dates. A column of
  timestamps (`2024-01-31 12:00:00 UTC`) still fits the property; give it `atomic: sc:DateTime`,
  and say in `description` that values carry a time.
- **Things known only by name.** Many tables name genes, drugs or variants without ids
  (ClinPGx annotations, MSigDB symbol lists). Don't invent ids or map names to ids yourself.
  Either keep them as an entity with `"no_id": "..."` and their name or symbol columns, or, when
  a row is a record about named things (an annotation, a report), type the row with a local
  class and leave the names as properties. Symbols aren't ids: don't give a symbol column
  `biolink:id` or `has_gene` (which wants ids).
- **Gene sets** (MSigDB, GMT files, Enrichr): Biolink has no gene-set class. A local class
  (`rcv:<kb>/GeneSet`, `close_match` `biolink:GeneGroupingMixin`) for the row, with the members
  as a Gene entity when they're ids (NCBIGene) and a local property when they're symbols.
- **RDF triple dumps** (a SPARQL export with subject, predicate, object columns): rows are
  `rdf:Statement`, with the columns as `rdf:subject`, `rdf:predicate`, `rdf:object`. Subjects and
  predicates are `id_form: url`; objects mix IRIs, literals and blank nodes (`_:b1`), so leave
  their form unset. Biolink types don't apply row by row (a triple isn't a disease), so the
  plan stays thin; say in `notes.md` that the ontology's own release files would describe it
  better.
- **Pathway relations** (BioPAX-like: reaction, catalysis, control): Bioregistry and Biolink
  don't cover BioPAX, so `vocab.py prefix biopax` finds nothing. Use the BioPAX namespace
  (`bp: http://www.biopax.org/release/biopax-level3.owl#`) for classes and relations, or SBO
  terms for reaction and participant roles.
- **Non-Biolink row types and roles** (`owl:Class`, `sssom:Mapping`, `csvw:Column`, `rdf:Statement`,
  `IAO:0000136` "is about") are fine. Declare their prefixes; the checks only look up Biolink
  terms in Biolink.
- **Properties that take identifiers or URLs** (`biolink:license`, `xref`, `has_evidence`)
  don't take free text. Leave a column of sentences untyped or give it a local property.
- **JSON sources.** mlcroissant 1.1.1 can't read entities grouped over JSON-path fields, so
  `apply_plan.py` types those columns individually instead and says so. Plan entities normally;
  the fallback is automatic.
- **Ontology dumps.** A file that lists the classes of an arbitrary ontology (BioPortal CSV
  exports, OBO files) has rows of whatever kind that ontology covers. `biolink:OntologyClass`
  is the honest type for the shared columns, even though Biolink prefers more specific
  classes. Where a file's ontology is known (a disease ontology, an anatomy ontology), add a
  layout for it with `record_set_name` and a more specific class.
- **Placeholders.** `N/A`, `-`, `none` in id or code columns are "no value", not data. Declare
  them in `null_values`; don't drop the column's `id_prefix` because of them.
- **JSON lists.** Columns whose values are JSON arrays are already lists; don't set
  `list_separator`. `sample.py` reports them as "JSON lists".
- **The data's URLs win.** Bioregistry's URI format for a prefix can differ from the URLs the
  knowledge base actually uses (`/links/variant/` vs `/links/variants/`). When the data has
  its own URL column, trust that, and note any mismatch.
- **Species.** Only add an `in_taxon` constant when the file is single-species — from the file
  name, documentation, or a taxon column with one value.

## Known Biolink quirks (v4.4.6)

Places where Biolink's machine-readable model disagrees with its own text. Follow the text, and
say so in `basis`.

- **`biolink:authors`**: the range is `agent`, but the definition asks for a comma-delimited list
  of author names. A column of names fits; the check doesn't ask for identifiers here.
- **`biolink:genome_build`**: the range is `StrandEnum` (a copying error); the values are build
  names like `GRCh38`. Use it with text values as a constant or column.
- **`biolink:participates_in`**: the domain (the subject's class) is `occurrent`, but the
  definition says the subject is a continuant (a gene) taking part in a process; the range,
  `biological process or activity`, is right. Its inverse `has_participant` has the mirror
  error (range `occurrent` where the object is the continuant). Gene → pathway with
  `participates_in` is standard usage; the check doesn't test this predicate's domain.
- **GO terms as `biolink:OntologyClass`**: Biolink doesn't list `GO` among OntologyClass's id
  prefixes, so the check warns about an entity's or a row's id column (not a column `type`). When
  the column holds GO terms of one aspect, use
  `biolink:BiologicalProcess`, `MolecularActivity` or `CellularComponent` instead; when it
  mixes aspects, keep OntologyClass and leave the warning, explained in `notes.md`.

- **`biolink:NamedThing` lists slots Biolink never defines**: `equivalent_identifiers` and
  `information_content` appear among NamedThing's slots, but `vocab.py slot` finds neither
  (`vocab.py class` flags them). Don't map columns to them.

Reported to Biolink: `participates_in` in biolink/biolink-model#1606, `authors` in #1836,
`genome_build` in #1837, OntologyClass id prefixes in #1838, NamedThing's undefined slots in
#1839.

Add to this list when you confirm another one with `vocab.py`.
