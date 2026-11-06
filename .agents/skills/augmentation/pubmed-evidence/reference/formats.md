# Formats you write or read by hand

The gold set and the three LLM replies. All are JSON or JSONL; ids are the ones in
[work-files.md](work-files.md).

## Gold set

A curator's claims for some abstracts of some pairs, and the edges of those pairs to score.
`augment.py evaluate` reads it (default `<work>/gold.jsonl`, or `--gold FILE`). One JSON object
per line, of two kinds; other keys are ignored.

An **abstract line** holds the curator's claims for one abstract of a pair:

```json
{"pair_id": "CHEBI:45783|MONDO:0011996", "pmid": "12345",
 "gold_claims": [
   {"polarity": "negative",
    "statement": {"subject": "CHEBI:45783", "predicate": "biolink:treats",
                  "object": "MONDO:0011996", "qualified_predicate": null, "qualifiers": {}},
    "also_supports": [
      {"subject": "CHEBI:45783", "predicate": "biolink:in_clinical_trials_for",
       "object": "MONDO:0011996", "qualified_predicate": null, "qualifiers": {}}],
    "claim_role": "primary_finding", "hedged": false,
    "negative_finding_type": "demonstrated_absence",
    "context": {"species": "NCBITaxon:9606", "anatomy": null, "notes": "adults over 65"},
    "evidence_sentence": "..."}]}
```

- `gold_claims` are in the shape the label step stores (`label.stored_claim`): CURIEs for the
  subject and object, `biolink:` predicates, only the qualifiers set, the species as an
  NCBITaxon CURIE. The anatomy may be a name, grounded as the run's are, or an UBERON CURIE.
  `[]` means the abstract states nothing about the pair.
- The claims count as step 6 counts them: the example's own statement is denied, so it supports
  nothing, but its `also_supports` entry supports an `in_clinical_trials_for` edge. A
  `hypothesis` claim, or any claim of a paper PubMed marks retracted, counts for nothing.
- The `pair_id`'s node order and the PMID's spelling (`"PMID:12345"`, a number) are normalized.
- Statements are read Biolink's way round, the curator's as the run's, with the categories in
  the work directory's `entities.jsonl`. A claim that fits its nodes neither way, or one about
  another pair, makes the whole line invalid.

An **edge line** names one edge to score:

```json
{"edge": {"subject": "CHEBI:45783", "predicate": "biolink:treats", "object": "MONDO:0011996",
          "primary_knowledge_source": "infores:drugcentral", "...": "..."}}
```

- `edge` is the record as the graph's `edges.jsonl` has it; its id is the one step 6 gives it
  (its own `id`, else a hash of the record).
- It needs no label: its expected outcome is step 6's refinement on its pair's gold claims,
  whether it is supported, with which PMIDs, and what it proposes. An edge whose pair has no
  gold abstract is left out of every score.

An abstract line whose `gold_claims` is null or missing is skipped as not yet curated, and
counted. A malformed line is skipped with a warning. For a repeated abstract or edge, the last
line wins.

- **Drawing its pairs:** `augment.py evaluate --draw N [--seed S]` draws N pairs from step 6's
  `refinements.jsonl`, stratified by each edge's outcome (`unsupported`, `supported`, or
  `refined`: supported with proposals or links) and predicate family (the scope rule that kept
  the edge), so that each outcome appears. It writes them to `work/gold_pairs.jsonl`
  ([work-files.md](work-files.md)).
- **Making one:** run `augment.py label --pairs work/gold_pairs.jsonl`. Each line of
  `label_sample.jsonl` is one (pair, PMID) with the abstract and the model's claims; adding
  `gold_claims` (written fresh, not copied from the model's) makes it an abstract line. Then add
  an edge line for each edge of those pairs to score.
- **Labeling it:** each candidate model labels exactly the gold set's pairs, with
  `augment.py label --pairs FILE --model M` and the pairs file it was drawn from. A pairs file
  has one JSON object per line with a `pair_id` (either node first; other keys are ignored), so
  one can be written by hand too. Pairs select didn't keep, or with nothing to read, aren't
  labeled, and the label step lists them.
- **Scoring it:** `augment.py evaluate [--gold FILE] [--run M] [--run M2@label@2fc040f2]`.
  The default run is the one in `label_run.json`; a model without a prompt version is scored
  once per prompt version it has claims for. Both caches are needed. Without the UBERON file,
  anatomy compares by equality, with a warning. An anatomy name the groundings cache doesn't
  have is grounded as step 4 grounds it (a free lookup, cached); a failed lookup leaves it
  ungrounded, counted and warned of. Both sides' proposals lose their species, as step 6's do,
  unless `--species-in-proposals` keeps it. `work/evaluate_report.md` has the scores
  ([work-files.md](work-files.md)).

## Label reply (step 3)

The model replies in strict structured output: the request sends the pair's schema
(`label.reply_schema`), one per set of scope rules, and the service returns an object that fits
it:

```json
{"abstracts": [
   {"abstract": "A1", "study_design": "animal",
    "claims": [
      {"polarity": "positive",
       "statement": {"subject": "X", "predicate": "affects", "qualified_predicate": "causes",
                     "qualifiers": {"object_aspect_qualifier": "expression",
                                    "object_direction_qualifier": "decreased", "...": "none"}},
       "also_supports": [],
       "claim_role": "primary_finding", "hedged": false, "negative_finding_type": null,
       "context": {"species": "mouse", "anatomy": "liver", "notes": null},
       "evidence_sentence": "..."}]},
   {"abstract": "A2", "study_design": "narrative_review", "claims": []}]}
```

- **Positions, not ids:** abstracts are `A1`...`A20`, an enum the schema fixes, in the order the
  request lists them; code maps them back to PMIDs. Every property is required, so a field the
  model leaves empty is `null`, or `none` for a qualifier slot.
- **Statements** name `X` or `Y` as the subject; the other node is the object. `predicate` is
  one of the pair's predicates, without `biolink:`. `qualified_predicate` is `causes` or `null`.
  `qualifiers` has every enum-valued qualifier slot Biolink has, each a value or `none`; which
  ones the statement may take is checked in code. The slots aren't nullable because
  Anthropic's strict output allows at most 16 parameters with union types (`anyOf`) in a
  request, and 13 nullable slots, written out for `also_supports` too, would be 26.
- **Context:** `species` is one of human, mouse, rat, zebrafish, fly, worm, yeast, dog, pig,
  macaque, rabbit or `other`; `anatomy` and `notes` are free text.
- **Stored:** code stores each answer with X and Y replaced by the pair's CURIEs, the object
  made explicit, predicates as `biolink:` CURIEs, only the qualifiers set, the species as its
  NCBITaxon term (`other` as none) and texts with collapsed whitespace. A claim given twice in
  one answer is kept once. Every answer records the model that answered.
- **Checked:** each claim is then checked (DESIGN.md, step 3); one failing a check stays in the
  cache, with its errors, and counts for nothing.
- **Failed calls are retried, not cached.** Nothing from a call is stored if the service
  rejects it, refuses it or cuts it short, if its reply isn't JSON, or if it names a position
  beyond the request's or answers an abstract twice. An abstract the reply leaves out gets no
  answer and is asked again next run; the summary counts these.

### Quotes

`quotes.quote_found(quote, title, abstract)` decides whether an evidence sentence is in its
abstract. It compares after Unicode NFKC, lower case, collapsed whitespace, dashes and curly
quotes made plain, brackets removed and outer punctuation stripped, so it accepts "GAMT-D is one
of three ..." for "(GAMT-D) is one of three ...", but not a paraphrase. An ellipsis (`...`, `…`
or `[...]`) splits a quote into at most 3 parts that must appear in order in the same text,
each at least 20 characters, so two entity names joined by an ellipsis don't pass. A claim
whose sentence isn't found is kept, and the repair pass (below) asks for its sentence again; if
that finds none, the claim counts for nothing.

## Repair reply (end of step 3)

Strict structured output, with `repair.REPLY_SCHEMA`, for one claim whose evidence sentence
wasn't found:

```json
{"quote": "Imatinib bound the ABL1 kinase domain"}
```

`quote` is the sentence the claim rests on, verbatim, or `null` when the abstract has none.
Code checks it with `quote_found`, as it checks the first pass's, and stores the answer either
way (a repair is asked once); a sentence still not found counts as none. A failed call stores
nothing and is sent again next run.

## Review reply (step 5)

Strict structured output too, with `review.REPLY_SCHEMA`:

```json
{"reviews": [{"item": "R1", "verdict": "reject", "reason": "One or two sentences."}]}
```

`item` is the claim's position in the request (`R1`...`R40`), and `verdict` is `upheld` or
`reject`. An item beyond the request's fails the call, and so does another verdict (strict
output prevents one, but a deployment that doesn't enforce it could send it). An item's first
verdict counts, and later ones are counted as repeats. A claim the reply leaves out stays
unreviewed and is sent again next run; until then step 6 holds its pair.
