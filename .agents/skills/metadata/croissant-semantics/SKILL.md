---
name: croissant-semantics
description: Add semantics (what the data means) to an existing Croissant metadata file for a biomedical knowledge base — group columns into typed entities (genes, diseases, ...), type rows as Biolink associations, mark identifier prefixes, units and code values — using only Croissant 1.1 constructs, and check the result against the real data. Use when asked to add semantics, Biolink types, identifier prefixes or entity/association structure to a croissant.json.
---

# Adding semantics to a croissant

A RoboCroissant file says which files a knowledge base has and what columns they contain. This
skill adds what the data **means**: which columns together describe a gene or a disease, what a
row asserts, which identifier prefix a column's ids use, what codes stand for. It writes that
into a new croissant using Croissant 1.1 constructs, and records why each statement was made.

You produce a **plan** (JSON, by layout), the scripts apply and check it. You don't edit the
croissant by hand.

## Inputs and outputs

- Input: a croissant (`croissant.json` or `croissant.json.gz`) and an output directory, plus
  the maintainers' `semantics_answers.json` next to the croissant, if there is one.
- Output, in the output directory:
  - `semantics_plan.json` — the plan (format: `reference/plan-format.md`)
  - `croissant.semantic.json` (or `.json.gz` if the input was gzipped) — the result
  - `check_report.md` — output of `check.py` (and `check_report.load_errors.txt`, full
    mlcroissant errors, when records failed to load)
  - `notes.md` — decisions, layouts left out and why, and questions for the knowledge base's
    maintainers

The input croissant is never modified. Put downloads, helper scripts and other scratch work
in `OUT/work/`, not elsewhere.

## Scripts

All in `scripts/`, run directly (they're `uv run --script` files and install their own
dependencies). Use `--help` on any of them.

| Script | Use |
|---|---|
| `inventory.py CROISSANT [--layout ID] [--json F] [--common-columns]` | List layouts (distinct column sets), largest first; describe one layout's columns, record sets and files; list the columns most record sets share |
| `sample.py CROISSANT --layout ID` | Read real rows from three of the layout's files; shows each column's example values and their shape (CURIEs and their prefixes, bare ids, lists, codes) |
| `dialects.py list/show/detect/read` | Known file formats (VCF, GAF, MITAB, CTD, ...) as computable dialects; which one reads a layout's files; read records using only what a croissant says about the file |
| `vocab.py class/slot/search/associations/prefix/identify/term/ols/infores` | Look up Biolink classes and slots (with near-miss suggestions), association classes for a subject/object pair, identifier prefixes (Biolink + Bioregistry), which prefixes fit bare ids, ontology terms by id or text, and infores ids for source names |
| `apply_plan.py CROISSANT PLAN OUT` | Write the plan into a new croissant |
| `check.py CROISSANT PLAN OUT --report R --load-records` | `--plan-only` first: terms, prefixes, dialects and units in seconds. Then the full check: checks terms, prefixes, codes and dialects against Biolink and sampled data, runs the MLCommons validator, reads a few records through mlcroissant (from the first lines of each file, not whole downloads), and reports coverage |

## Workflow

1. **Survey.** If there's a `semantics_answers.json` next to the croissant, read it first: it
   holds the knowledge base's maintainers' answers to earlier questions (see "Maintainer
   answers" below). Don't ask those questions again; `apply_plan.py` and `check.py` apply the
   answers to your plan automatically. Then `inventory.py CROISSANT`. Note the knowledge base (name, description, url in the
   croissant) and how record sets spread over layouts. Large knowledge bases have hundreds of
   layouts; work from the top of the list.

2. **Pick the layouts to do.** Aim for the layouts that cover most record sets and the core
   content (associations and the main entity tables) — typically the top 5–15, or until the
   remaining layouts are minor (readme-like files, logs, per-release counts). If there are many
   small layouts (`inventory.py` will say so), the record sets probably share core columns: use
   `inventory.py --common-columns` and plan those columns once with `match.has_columns`.
   Everything you leave out goes in the plan's `skipped` list with a reason. Doing fewer layouts
   well is better than many badly.

3. **For each layout, look at the data before deciding anything.**
   - `inventory.py CROISSANT --layout ID` for columns, descriptions and file names. A column
     marked `MIXED` has different atomic types in different record sets (the pipeline infers
     them per file); decide its type from the data and set `atomic`.
   - For tables, `dialects.py detect CROISSANT --layout ID` finds how the files are written
     (comment lines, header or none, delimiter) among known formats. Well-known formats (VCF,
     GAF, MITAB) have published specs; rely on them rather than rediscovering the format. If
     nothing fits, describe the dialect inline (`reference/dialects.md`). Then sample with
     `--dialect NAME` so rows are read the way the format says, not guessed.
   - `sample.py CROISSANT --layout ID` for real values. It reads three files of the layout (the
     newest-looking by name, the largest, the smallest), because one file — often an old
     snapshot or a filtered subset — can hide values (`N/A` placeholders, extra code values,
     other id types). `--files N` samples more of them; `--record-set ID` samples one
     particular file. If sampling fails (a huge file, an archive, a file set), say so in the
     plan's `basis` and lower confidence.
   - For code columns (types, evidence, relations, sources), a sample misses rare values.
     `sample.py CROISSANT --record-set ID --profile COL1,COL2` reads the whole file and counts
     every distinct value; repeat `--record-set` to add several files up. Map from the profile,
     not the sample.
   - **Be gentle with the knowledge base's servers.** Run one sampling or check command at a time
     per knowledge base, never many in parallel: hosts block bursts with a bot challenge, and then
     every later request fails. The scripts pace their own requests and cache downloads (in
     `~/.cache/croissant-semantics/downloads`; `--refresh` downloads again), so re-running is
     cheap (a copy cached under a larger `--max-mb` serves a smaller one). If a script
     reports a bot challenge or an HTTP 403, stop sampling that host, don't try to get past it,
     and say in `notes.md` which checks couldn't run. The scripts then refuse that host for an
     hour, across runs, and work from cached files.
   - Spreadsheets (xlsx), SQLite databases and parquet files are sampled and profiled too: the
     sheet or table is the one the record set's name ends with ("file.xlsx — Sheet1",
     "file.xlsx: Sheet1"), and sampling fails rather than guess when it can't tell. They're read
     whole, so raise `--max-mb` if needed (on `check.py` as well). mlcroissant can't read
     spreadsheets or databases, so their records aren't loaded.
   - XML, RDF (Turtle, RDF/XML, OWL), FASTA and other files that aren't tables can't be sampled:
     the scripts say so. If a layout matters, check its ids with a small script in `OUT/work/`
     (`uv run --with rdflib` for RDF; stream XML with `xml.etree.ElementTree.iterparse`), and say
     how in `notes.md`. Their fields usually have no `extract`, so no tool can read them: a
     generator problem to report.
   - `--profile` with `--layout` profiles `--files` record sets of the layout (default 3, or
     `all`) instead of naming each with `--record-set`. `--prefixes` counts each value's prefix
     or tag, for all profiled columns or only those listed (`--prefixes COL1,COL2`); `--by COL`
     cross-tabulates against another column (which object types each code occurs with);
     `--split SEP` counts the items of list cells; `--stop-mb N` stops each file after N MB, for
     files of many GB (the counts then cover only the start; in a file sorted by species or id
     that may be one group only, so say so).
   - **Profile id columns** of the main layouts (`--profile COL --shapes`) before trusting them:
     the data check reads the first rows of a few files, so a column whose later rows lack ids, or
     hold another id family, passes it. Several knowledge bases had 20–30% of rows without an id
     after hundreds of rows that all had one.
   - A `match.has_columns` entry that covers many layouts is sampled by `check.py` on only a few
     of its files. Sample the layouts it spans that differ (`--record-set-name`, `--files`)
     before trusting it.
   - The scripts use the operating system's certificate store. If downloads still fail with a
     certificate error, report it with the output; don't turn verification off.
   - If the documentation is behind a bot check ("verify you are human", a Cloudflare or ALTCHA
     challenge), don't try to get past it; plan from data and names, and cap confidence at
     `medium` for meanings only the documentation could confirm. The croissant's provenance may
     cite pages that have since been put behind a check: cite only what you could read.
   - In a huge file the first rows can be unrepresentative (CTD's 3 GB `genes_diseases` starts
     with thousands of inferred rows). Profile a smaller file of the same layout (the curated
     subset) rather than streaming the whole thing.
   - To pick record sets by name, use `--record-set-name REGEX` (instead of `--layout`) rather
     than building a list of `--record-set` arguments in the shell (zsh doesn't split variables
     into words). `--per-file` shows each sampled file separately, for layouts whose files differ
     (one id kind per file).
   - The data check reads the first rows of a few files. A column's later rows can differ (blank
     nodes after 200 IRIs, a rare code): confirm id forms and codes with `--profile`, which
     reads whole files.
   - `inventory.py` lists documentation files shipped with the data (README.pdf inside a zip, a
     data dictionary) after the layouts. Read the relevant ones: `sample.py CROISSANT --dump
     FILE` prints a file as text, `--member NAME` a member of an archive (it lists the members
     of a zip when the name is wrong), `--sheet NAME` a sheet of a spreadsheet.
   - `sample.py` flags columns whose declared type doesn't fit the values (an `sc:Integer`
     column holding `27152464|29518214`) and files whose rows are wider or narrower than the
     header. Both are usually pipeline problems: fix types with `atomic`, and report them.
   - **When the scripts can't read a source**, fetch the files yourself (one at a time, into
     `OUT/work/`) and point the scripts at them with `--url-map OLD=file:///abs/path/` so their
     checks still run, rather than writing your own profilers. Downloads are cached, and
     `~/.cache/croissant-semantics/downloads/index.tsv` maps each cached file to its URL.
   - If a layout's "column names" are really data (no header row, so the pipeline took the
     first data row as names, and every file became its own layout), skip it and report it in
     `notes.md` as a pipeline problem.
   - If the data URLs are gone (HTTP 404), the knowledge base has probably moved on to a newer
     release. Find the current URLs from its download page and pass `--url-map OLD=NEW` to
     `sample.py` and `check.py` (e.g. `--url-map 202607=202610`). When more than the release
     changed (another path or file name), map whole URLs, one `--url-map OLD_URL=NEW_URL` per
     file. When the URLs themselves contain `=` (query strings), write `OLD=>NEW`. Say so in
     `notes.md`; it's a pipeline problem too. A SPARQL endpoint URL that returns JSON where the
     croissant describes a table usually needs `&format=csv` (or `text/csv`) mapped on.
   - Archives: members of zip and tar archives are sampled. A zip is downloaded whole (up to
     `--max-mb`); a tar is streamed until the member is found, so a member deep inside a huge
     tar can exceed `--max-mb`. An archive that fails once isn't tried again, and sampling stops
     after repeated failures. If you must, extract a member into `OUT/work/` yourself, and record
     what you checked and how in `notes.md`.
   - If the data needs a login or API key (HTTP 401/403, or the croissant's
     `conditionsOfAccess` says so), don't look for credentials. Plan from the documentation
     and column names only: `source` is `documentation` or `agent`, `confidence` at most
     `medium`, and `notes.md` says that nothing was checked against data.
   - **Look for a shipped data dictionary first**: a "column definitions" or README sheet in a
     spreadsheet, a README next to the files, the template row of a ROBOT template, a data
     dictionary on the download page. It's the best source; cite it with
     `source: data_dictionary`.
   - Read the knowledge base's documentation when column meanings aren't clear from names and
     values (the croissant's `url`, the file descriptions, a README or data dictionary). Follow
     links from the downloads page rather than guessing URLs (a site whose pages are `#` routes,
     like `docs.example.org/#Section`, usually also has the real page paths; use those). Cite
     what you used. Help pages that render in the browser (a fetch returns an empty shell) often
     come from a public repository: look for it (`gh search repos`), and read the docs there.
   - **Check that the croissant describes the source's main data.** `inventory.py` lists the files
     no record set reads. When the source's own download page names main files (STRING's network
     files, UniProt's Swiss-Prot) and they're among those, or missing altogether, the plan can't
     add them: say in `notes.md`, near the top, which main files the croissant leaves out (a
     generator problem), and plan what's there.
   - Check whether record sets in the layout differ in meaning (species, ontology aspect,
     direction). If so, split with `match.record_set_name`.

4. **Decide the semantics**, in this order (`reference/vocabulary.md` has the details):
   - *Entities*: which columns describe one thing (an id with its name, symbol, taxon)? Group
     them; give the group a class; mark its id column `biolink:id` with its prefix and form.
   - *Row*: if a row relates two entities, type it as the most specific fitting Biolink
     association, give the entities their roles (`biolink:subject`, `biolink:object`), and add
     the predicate (constant, or a code column mapped with `codes`). Check the association's
     slot usage (`vocab.py class X`): its subject and object ranges must fit your entities'
     classes. Codes that negate or infer (`is_not_…`, `…_via_orthology`) need marking, not
     the direct predicate alone. If a row *is* a thing (a
     variant, a gene, with a unique id per row), type the row with that thing's class, map its own columns directly, and
     link the other things it mentions as entities with roles like `biolink:has_gene`. Many
     layouts sharing columns? Define specs once in `column_library`.
   - *Dialect*: for tables that aren't plain CSV/TSV with a header row, the layout's `dialect`.
   - *Other columns*: publications, evidence, scores, p-values, units, list separators, code
     tables. Leave columns you can't support untyped.
   - *Vocabulary*: Biolink if it truly fits → another established vocabulary → a local
     `rcv:` term linked with SKOS → nothing. Never force a vague type.
   - Every row, entity and column you type gets `basis`, `source` and `confidence`.

5. **Write the plan** to `OUT/semantics_plan.json`.

6. **Apply and check.**
   ```
   scripts/apply_plan.py CROISSANT OUT/semantics_plan.json OUT/croissant.semantic.json
   scripts/check.py CROISSANT OUT/semantics_plan.json OUT/croissant.semantic.json --report OUT/check_report.md --plan-only
   scripts/check.py CROISSANT OUT/semantics_plan.json OUT/croissant.semantic.json --report OUT/check_report.md --load-records
   ```
   While iterating on a few layouts, `--layouts id1,id2 --no-validate` re-checks just those
   (`--skip-layouts id3` leaves some out);
   run the full check at the end. It prints progress to stderr, and if interrupted still writes
   the report so far. Validating a large croissant (hundreds of thousands of fields) takes
   several minutes, sometimes over an hour (`--validate-timeout` is in seconds, default 3600;
   a timeout is reported as unverified, not as a failure; a croissant identical to one already
   validated reuses the result). Run the full check as a background task and wait for its
   completion notification (run it with your harness's background option, redirecting stderr to
   `OUT/work/check.log`). Don't poll: `sleep` is blocked, `timeout` isn't on macOS, a
   `pgrep -f check.py` loop matches its own command line, and the report is only written at the
   end. The last line on stderr is a summary, `[check] done: N errors, ...` (with the prefix).
   `check_report.md` from an earlier `--plan-only` run stays on disk until the full check
   finishes; don't read it as the result.

   The validator and each record load run with a memory cap (`--memory-gb`, default 4). Record
   loading tests one record set per plan entry and set of columns, at most `--load-per-entry`
   (default 3) per entry; a load that times out is retried once, then reported as unverified.
   Croissants whose columns are named `column_1`, `column_2`, … (headerless files, or headers the
   generator ignored) always fail to load in mlcroissant, from the input croissant too, because
   it ignores dialects: the report says so, and the sampled-data checks are the evidence.

   Fix every **error** (wrong prefix, values that don't fit the declared form, unknown Biolink
   term, validator failure) by changing the plan, then re-run both. Read every **warning** and
   either fix it or explain why it stands, in the plan's `explained` (copy the `where` and `message`
   from `check_report.findings.json`) and in `notes.md`. `apply_plan.py` also warns about plan
   keys it doesn't know.

   The validator and each record load run with a memory cap (`--memory-gb`, default 4), since
   agents run in parallel. A load stopped by the cap is a warning; the usual cause is a croissant
   that reads a table as several `extract: {fileProperty: "lines"}` fields with regex transforms:
   mlcroissant combines those per line pair, so its memory grows with the square of the lines
   (2,000 lines take 1 GB). That's a generator problem to report; the sampled-data checks are the
   evidence for those layouts.

   For a croissant too large for the validator to finish (thousands of record sets; it can
   take many hours), `--validate-reduced` validates one record set per layout and row type,
   with the record sets and files they reference; the report says it was reduced.

   Records that don't load from the original croissant either are pre-existing problems
   (formats mlcroissant can't read, commented headers, dead URLs, wrong checksums); mention them
   in `notes.md`. Fix them only when the plan can describe the file correctly (a `dialect`, an
   `atomic` type for a column the input croissant mistypes), and then list the fix in
   `notes.md` as a problem for the pipeline. For layouts that still don't load, the
   sampled-data checks are the evidence. Evidence you gathered outside the scripts (a member
   you extracted by hand, units you looked up) goes in `notes.md` with how you got it.

   Some problems a plan can't fix: a column the croissant declares but the files don't have
   (the plan can only describe columns that exist), and `null_values`, which mlcroissant
   ignores, so a numeric column holding `-` or `NA` still fails to load; type such a column
   `atomic: sc:Text` and report the type. Files that are deliberately invalid (test fixtures,
   examples of bad input) go in `skipped`, not in the plan.

7. **Write `notes.md`**: what you covered (layouts, record sets, coverage numbers from the
   report), what you left out and why, choices a reviewer should know about (local terms,
   low-confidence types, prefixes Biolink doesn't list for a class), and specific questions
   for the maintainers.

## Maintainer answers

Answers from a knowledge base's maintainers live in `semantics_answers.json` next to its
croissant (or wherever `--answers` points), not in a plan, so they survive every rerun:

```json
{"kb": "robokop", "answers": [
  {"question": "What does `slope` mean on GTEx edges?",
   "answer": "The slope of the linear fit of allele count against expression.",
   "by": "Jane Doe (maintainer)", "date": "2026-10-20",
   "applies_to": {"column": "slope"},
   "spec": {"property": "biolink:effect_size", "type": "STATO:0000656"},
   "prefixes": {"STATO": "http://purl.obolibrary.org/obo/STATO_"}}
]}
```

- `applies_to.column` (and optionally `.layout`, a plan layout id) says where it applies. The
  `spec` keys are column-spec keys (`reference/plan-format.md`). They override the plan's,
  and the column gets `source: maintainer`, `confidence: high` and the answer as its `basis`.
  A column the plan doesn't mention is added (optional) to every layout whose record sets
  have it.
- `"skip": "reason"` instead of `spec`: the column is left untyped and listed in `skipped` (a
  maintainer says the meaning isn't known).
- `prefixes` and `local_term` add what the answer's terms need.
- An answer without `applies_to` is just recorded.
- `check.py` reports what each answer did. An answer that matches no column is a warning.

When someone answers a question from `notes.md`, add it to this file (you may write there,
though not to the croissant). Don't edit the answer into the plan.

## Rules

- Base every statement on the data or the knowledge base's documentation. Column names alone
  justify at most `medium` confidence; a guess is `low` and goes in the maintainer questions.
- Don't rewrite identifiers or data. Prefix annotations describe values, they don't change them.
- Don't invent codes, prefixes or terms. Check Biolink terms with `vocab.py`; check prefixes with
  `vocab.py prefix`.
- Keep the input croissant untouched and write only in the output directory, except for
  recording maintainer answers in `semantics_answers.json`.
- If a script fails in a way the plan can't fix, stop and report the failure with its output
  rather than working around it.

## Reference

- `reference/plan-format.md` — the plan, field by field, with examples
- `reference/vocabulary.md` — choosing terms, Biolink pointers, identifiers, common mistakes
- `reference/dialects.md` — describing how to read a file (CSVW dialects, format specs), and
  what readers do with it
- `reference/croissant-constructs.md` — what each plan entry becomes in Croissant, our
  conventions, and mlcroissant details the scripts handle
