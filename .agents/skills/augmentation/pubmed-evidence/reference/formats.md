# Formats you write or read by hand

The gold set, the two LLM replies, and the files a Pi subagent works from. All are JSON or
JSONL; ids are the ones in [work-files.md](work-files.md).

## Gold set

Hand-labeled (statement, abstract) items that decide which models are good enough. One JSON
object per line:

```json
{"statement_id": "11f22785186c970d", "pmid": "24404629", "gold_label": "confirmed"}
```

- `gold_label` is `confirmed`, `contradicted`, `ambiguous` or `no_evidence` ("no evidence" and
  other spellings are normalized). `pmid` may be written `"PMID:24404629"` or as a number.
  Any other keys (`pair_id`, `sentence`, `notes`, ...) are ignored.
- **Making one:** `label_sample.jsonl` from `label_abstracts.py --sample N` is already in this
  format, with the model's `label`, `quote` and the abstract on each line and
  `gold_label: null`. Fill in `gold_label` (don't copy the model's) and use the file as the
  gold set; aim for 50–100 items covering every rule and all four labels.
- **Labeling it:** `label_abstracts.py --gold FILE --model M [--prompt P]` labels only the
  gold items (their pairs, those PMIDs). It needs a `statement_id` and a `pmid` on every line.
  A `--gold` run never changes `label_run.json`.
- **Scoring it:** `evaluate_models.py --gold FILE --run M [--run M2@label@2fc040f2]` (default
  gold file `<work>/gold.jsonl`, default run the one in `label_run.json`; a model without a
  prompt version is scored once per prompt version it has labels for). Lines with
  `gold_label: null` are skipped and counted; malformed lines are skipped with a warning; for a
  repeated item the last line wins. Decisive labels whose quote wasn't found are scored both
  as given and as `no_evidence` (what step 6 does with them).

## Label reply (step 5)

The model replies with one JSON object, as `prompts/label.md` ("Reply") asks:

```json
{"labels": [
   {"statement": "S1", "pmid": "12345678", "label": "confirmed", "quote": "...", "claim": "causal"},
   {"statement": "S2", "pmid": "12345678", "label": "ambiguous"}],
 "proposals": [
   {"statement": "S2", "qualifiers": {"object_direction_qualifier": "decreased"},
    "claim": "causal", "evidence": [{"pmid": "12345678", "quote": "..."}]}]}
```

- **Labels:** only `confirmed`, `contradicted` and `ambiguous` are listed; every (statement,
  PMID) of the request left out is stored as `no_evidence`. `quote` is required for
  `confirmed` and `contradicted`, optional for `ambiguous`. `claim` (`causal` or `associative`)
  is kept for decisive labels.
- **Proposals are changes**, not whole sets: the qualifiers to add or replace. Review and
  step 6 merge them onto the statement's own (`{**statement.qualifiers, **proposed}`), and
  code checks that merged set. Code also: strips `biolink:` from slot names and makes
  `qualified_predicate` a `biolink:` CURIE; drops items equal to the statement's own (a
  proposal left empty is dropped); drops `qualified_predicate` if the merged set has no
  `subject_*` or `object_*` qualifier; and adds `qualified_predicate: biolink:causes` to a
  causal proposal with a direction or aspect. Each evidence PMID becomes its own cache row.
- **Invalid proposals** (stored with `valid: false` and the `errors`, never reviewed or
  applied): a slot or value not offered for the statement (open-range context qualifiers are
  never offered); an associative claim on a set with `biolink:causes`; a generic variant form
  (`genetic_variant_form`, `mutant_form`, `polymorphic_form`) on a gene-to-disease or
  gene-to-phenotype statement; anything `biolink.validate_qualifiers` rejects.
- **Read leniently:** statement ids `S1`, `s1`, `1`, `S 1`, `S1.`; a PMID is the digits in the
  value (`PMID 123`, `PMID:123`, `123`); label values ignore case, spaces and surrounding
  punctuation. A proposal may give `pmid`/`quote` at the top level instead of `evidence`.
- **Unreadable replies are retried, not cached.** The call counts as failed and nothing from
  it is stored if: there is no `labels` list; a label isn't an object or has an invalid label;
  a label or proposal names an unknown statement id or PMID; `proposals` isn't a list; or a
  proposal isn't an object or has no `qualifiers` object or no evidence objects. A label
  for a combination the request didn't ask for is ignored and counted. If one combination is
  labeled twice, a decisive label with a found quote wins over `ambiguous`, which wins over a
  decisive label with a bad quote; `confirmed` and `contradicted` both quoted become
  `ambiguous`.

### Quotes

`quotes.quote_found(quote, title, abstract)` decides `quote_valid`. It compares after Unicode
NFKC, lower case, collapsed whitespace, dashes and curly quotes made plain, brackets removed
and outer punctuation stripped, so it accepts "GAMT-D is one of three ..." for "(GAMT-D) is one
of three ...", but not a paraphrase. An ellipsis (`...`, `…` or `[...]`) splits a quote into
at most 3 parts that must appear in order in the same text, each at least 20 characters, so
two entity names joined by an ellipsis don't pass. A label with a bad quote is kept
(`quote_valid: false`) but step 6 ignores it.

## Review reply (step 5b)

```json
{"reviews": [{"item": "R1", "verdict": "accept", "reason": "One or two sentences."}]}
```

`item` is the local id the request gives (`R1`, `R2`, ...); `verdict` is `accept` or `reject`.
A reply without a `reviews` list is a failed call. Entries with an unknown or repeated id or
another verdict are counted as invalid; items the reply leaves out aren't stored and are sent
again next run.

## Pi-subagent review: export and import

```text
review_labels.py --export work/review_items.jsonl --reviewer pi:gpt-6.1-sol
review_labels.py --import work/review_verdicts.jsonl --reviewer pi:gpt-6.1-sol
```

**Export** (no calls) writes the items that reviewer hasn't reviewed at this review prompt
version: contradicted labels with a found quote and valid proposals with a found quote, for
statements in `statements.jsonl` with a cached abstract. One self-contained line per item:

| Field | Content |
| --- | --- |
| `item_id` | Stable id, e.g. `c-04adbc1449d3` (`q-...` for a qualifier item) |
| `kind` | `contradicted` or `qualifier` |
| `statement_id`, `pair_id`, `pmid` | What it's about |
| `entities` | Both nodes: `name`, `category`, `synonyms` |
| `sentence`, `structured` | The edge in words and as Biolink JSON with its qualifiers |
| `quote`, `claim` | The first pass's |
| `qualifiers`, `resulting_qualifiers`, `resulting_statement` | Qualifier items: the change, the merged set, the merged set in words (null otherwise) |
| `title`, `abstract` | The cited abstract |
| `label_model`, `label_prompt_version`, `review_prompt_version` | Bookkeeping |

Next to it, `review_items.instructions.md` is `prompts/review.md` with its "Input and reply
format" section replaced by these line formats. Give the subagent both files.

**Verdict file**, one line per item:

```json
{"item_id": "c-04adbc1449d3", "verdict": "reject", "reason": "The negative finding is limited to one mutant."}
```

**Import** stores the verdicts as reviews by `--reviewer` (required; use `pi:<model>`) and,
when `label_run.json` names the reviewed run, names that review in it. Its review prompt
version is `--review-prompt-version`, by default recomputed from the current `--prompt`
(`review-pi@<hash>`, printed by the export), so import before editing the prompt. Ids that
aren't items of the run are counted as `unknown`, lines that don't parse or have another
verdict as `invalid`; neither is stored. An item without a verdict line is exported again
next time.
