# Formats: the graph, the gold set and the model replies

All are JSON or JSONL; ids are the ones in [work-files.md](work-files.md).

## The graph, in and out

**Input** (`--input`): a directory with `nodes.jsonl` and `edges.jsonl`, the `Node` and `Edge`
records of the repository's `shared/schema.py`. For each file the step takes the first that
exists of `NAME`, `NAME.gz`, ORION's `normalized_NAME` and `normalized_NAME.gz`. An edge that
isn't a valid `Edge` record is skipped by select and written as read by apply; both reports
list it under `invalid_edges`.

**Output** (`--output`, which can't be the input directory): `nodes.jsonl` as read, and
`edges.jsonl` with every input edge in input order, then the proposed edges by id. Originals
are never removed, negated or replaced.

| Edge | Written as |
| --- | --- |
| checked | an `AugmentedEdge`: the record as read, an `id`, `augmentation: checked`, and the fields below |
| proposed | an `AugmentedEdge` with `augmentation: proposed`, the fields below, and this stage's provenance: `primary_knowledge_source: infores:kgbcia-augmentor`, `supporting_data_source: [infores:pubmed]`, `knowledge_level: knowledge_assertion`, `agent_type: text_mining_agent` |
| anything else | as read, with an `id`: edges out of scope, of a held pair (a claim that would count has no review, or names anatomy the ground step never looked up), or of a pair not fully labeled or not searched |

| Field | On a checked edge | On a proposed edge |
| --- | --- | --- |
| `id` | its own, or `kgbcia-edge:` plus a hash of the record | `kgbcia-augmentation:` plus a hash of its statement |
| `original_edge_ids` | its own id | the originals it refines |
| `publications` | the source's own, then the supporting PMIDs it didn't cite | its supporting PMIDs |
| `lit_support_score` | the number of abstracts that support it; 0 for an unsupported edge | the same, for its statement |
| `lit_supporting_publications` | their PMIDs | their PMIDs |
| `lit_added_publications` | those appended to `publications` | absent |
| `lit_refined_by` | the proposed edges more specific than it, and the edges of the pair that already make such a statement (which aren't added again) | absent |
| `lit_context_notes` | its supporting claims' notes and ungrounded anatomy names | the same, for its claims |
| `lit_evidence` | every abstract read for the pair (a `PublicationEvidence`), each claim marked with whether it `supports` this edge | the same, marked for this edge |

An unsupported edge is written as a checked edge with score 0 and its `publications` unchanged,
so "checked, nothing found" reads apart from "never checked". Feeding the stage its own output
adds nothing twice: the fields an earlier run wrote are stripped, the PMIDs it appended are
taken back out, and the edges it proposed are dropped before this run adds its own. DESIGN.md,
"Step 6: Refining the edges", has the rules.

## Gold set

A curator's claims for some abstracts of some pairs, and the edges of those pairs to score.
`augment.py evaluate` reads it (`--gold FILE`, default `<work>/gold.jsonl`). One JSON object
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
- It needs no label: its expected outcome is step 6's refinement on its pair's gold claims
  (whether it is supported, with which PMIDs, and what it proposes). An edge whose pair has no
  gold abstract is left out of every score.

An abstract line whose `gold_claims` is null or missing is skipped as not yet curated, and
counted. A malformed line is skipped with a warning. For a repeated abstract or edge, the last
line wins.

### Making and scoring one

SKILL.md's step 5 has the commands. What each does:

- **`evaluate --draw N [--seed S]`** draws N pairs from step 6's `refinements.jsonl` into
  `gold_pairs.jsonl` ([work-files.md](work-files.md)), stratified by each edge's outcome
  (`unsupported`, `supported`, or `refined`: supported with proposals or links) and predicate
  family (the scope rule that kept the edge), so that each outcome appears.
- **`label --pairs FILE`** labels exactly the pairs a pairs file names: one JSON object per
  line with a `pair_id` (either node first; other keys are ignored), so one can be written by
  hand too. Pairs select didn't keep, or with nothing to read, aren't labeled, and the step
  lists them. Each line of the `label_sample.jsonl` it writes is one (pair, PMID) with the
  abstract and the model's claims; adding `gold_claims` (written fresh, not copied from the
  model's) makes it an abstract line. Then add an edge line for each edge of those pairs to
  score. Each candidate model labels the same pairs file.
- **`evaluate [--gold FILE] [--run M] [--run M2@label@2fc040f2]`** scores the runs into
  `evaluate_report.md` ([work-files.md](work-files.md)).
  - The default run is the one in `label_run.json`. A model without a prompt version is scored
    once per prompt version it has claims for.
  - Both caches are needed. Without the UBERON file, one anatomy term is below another only
    when they're equal, with a warning.
  - An anatomy name the groundings cache doesn't have is grounded as step 4 grounds it (a free
    lookup, cached); a failed lookup leaves it ungrounded, counted and warned of.
  - Both sides' proposals lose their species, as step 6's do, unless `--species-in-proposals`
    keeps it.
  - It exits 1 when no run has answers for the gold abstracts.

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
- **Checked:** each claim is then checked (DESIGN.md, "Step 3: Claims"); one failing a check
  stays in the cache, with its errors, and counts for nothing.
- **Failed calls are retried, not cached.** Nothing from a call is stored if the service
  rejects it, refuses it or cuts it short, if its reply isn't JSON, or if it names a position
  beyond the request's or answers an abstract twice. An abstract the reply leaves out gets no
  answer and is asked again next run; the summary counts these.

### Quotes

`quotes.quote_found(quote, title, abstract)` decides whether an evidence sentence is in its
abstract.

- It compares after Unicode NFKC, lower case, collapsed whitespace, dashes and curly quotes
  made plain, brackets removed and outer punctuation stripped. So it accepts "GAMT-D is one of
  three ..." for "(GAMT-D) is one of three ...", but not a paraphrase.
- An ellipsis (`...`, `…` or `[...]`) splits a quote into at most 3 parts that must appear in
  order in the same text, each at least 20 characters, so two entity names joined by an
  ellipsis don't pass.
- A claim whose sentence isn't found is kept, and the repair pass repairs it: code puts back
  the whole sentences the quote was taken from (`quote_align.repair`), and the repair model is
  asked only when code can't place the quote. If neither finds one, the claim counts for
  nothing.

## Repair reply (end of step 3)

Strict structured output, with `repair.REPLY_SCHEMA`, for one claim whose evidence sentence
wasn't found and that code couldn't place (under half the quote's words are in the abstract):

```json
{"quote": "Imatinib bound the ABL1 kinase domain"}
```

`quote` is the sentence the claim rests on, verbatim, or `null` when the abstract has none.
Code checks it with `quote_found` and stores the answer either way (a repair is asked once); a
sentence still not found counts as none. A failed call stores nothing and is sent again next
run.

## Review reply (step 5)

Strict structured output, with `review.REPLY_SCHEMA`:

```json
{"reviews": [{"item": "R1", "verdict": "reject", "reason": "One or two sentences."}]}
```

`item` is the claim's position in the request (`R1`...`R40`), and `verdict` is `upheld` or
`reject`. An item beyond the request's fails the call, and so does another verdict (strict
output prevents one, but a deployment that doesn't enforce it could send it). An item's first
verdict counts, and later ones are counted as repeats. A claim the reply leaves out stays
unreviewed and is sent again next run; until then step 6 holds its pair.
