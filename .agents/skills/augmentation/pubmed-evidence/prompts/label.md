# Extracting claims from abstracts

You read PubMed abstracts about one pair of entities from a biomedical knowledge graph, X and Y,
and write down what each abstract claims about how they are related. Code compares your claims
with the graph's edges between X and Y: it adds the PMIDs of the claims that support an edge to
that edge, and proposes the more specific statements those claims make as new edges. So be
strict and literal: write down only what the abstract itself states about these two entities, at
the level it states it. When unsure, leave a claim out, or make it weaker (a more general
predicate, `hedged`, `background`) rather than stronger.

## The request

- The entities X and Y: name, identifier, Biolink category, synonyms and MeSH headings.
- The predicates a claim may use, by their Biolink names.
- The qualifiers each predicate takes, by which entity is the subject (`Q1`, ...), with their
  allowed values; long value lists are named (`one of DirectionQualifierEnum`) and given under
  "Qualifier values". A predicate or subject not listed takes no qualifiers.
- The abstracts `A1`, `A2`, ..., each starting with `## A1 (PMID:<number>, <year>)`, then
  PubMed's publication types when it has them, the title and the text.

## The five rules

1. **Negative claims need explicit language** ("did not", "no significant", "failed to", "was
   no better than placebo"). Never infer a negative from what an abstract leaves out.
2. **Claim at the level the abstract states, no higher and no lower.** Use the most specific
   predicate and qualifiers the abstract supports, and nothing it doesn't say: no direction,
   aspect or mechanism it doesn't give, no `treats` for a result in mice.
3. **Split mixed results.** A finding that differs by subgroup, tissue or species becomes
   several claims, each with its own context: "increased in the liver but decreased in the
   kidney" is two claims.
4. **Ground the entities.** Every claim is about X and Y themselves ("Which entities count",
   below). A drug's metabolite is the drug with a derivative qualifier, where the predicate
   takes one; a finding about another entity is no claim about this pair.
5. **Quote verbatim.** Every claim's `evidence_sentence` is copied from its abstract
   ("Quotes", below). Code checks it, and a claim whose sentence isn't found counts for
   nothing.

## A claim

Each claim is one finding about X and Y:

- **`polarity`**: `positive` if the abstract states that the relationship holds, `negative` if
  it states that it doesn't (rule 1). A negative claim denies its `statement`, which is written
  as the positive statement: "A did not improve B" is `negative` with the statement "A treats
  B".
- **`statement`**, in Biolink terms:
  - `subject`: `X` or `Y`, the entity that acts or has the property; the other is the object.
  - `predicate`: one of the request's predicates, the most specific the abstract supports.
  - `qualified_predicate`: `causes` or `null` (below).
  - `qualifiers`: the slots of the predicate's qualifier set that the abstract states, from
    their allowed values; every other slot `none`.
- **`also_supports`**: further positive statements the same finding shows that the claim's own
  statement doesn't already imply. A trial is the usual case: a null phase 3 trial of A in
  patients with B is a negative `treats` claim, and it also supports `in_clinical_trials_for`.
  Don't list a statement more general than a positive claim's own (it follows from it), or one
  a negative claim denies. Most claims have none.
- **`claim_role`**: what the quoted sentence states:
  - `primary_finding`: the authors' own result or conclusion, including a systematic review's
    or meta-analysis's pooled finding and a case report's own case;
  - `background`: established knowledge the authors assert, in the introduction or in a
    narrative review ("A, an inhibitor of B", "A is approved for B"), or another study's result
    they point to;
  - `hypothesis`: proposed, suspected or asked, not shown ("A may be a target for B").
- **`hedged`**: `true` when the sentence words the finding as a possibility ("may", "might",
  "could", "suggests", "appears to", "potential") or the effect is marginal.
- **`negative_finding_type`**, for negative claims only (`null` on positive ones):
  `demonstrated_absence` when the study was built to show the effect and didn't find it (a
  placebo-controlled trial, an adequately powered study, an equivalence finding) or the authors
  state as established that it doesn't hold; `failed_to_detect` when it saw nothing but
  couldn't rule the effect out (a small or underpowered study, a non-significant trend). When
  unsure, `failed_to_detect`.
- **`context`**, where the claim holds, as far as the abstract says:
  - `species`: the organism studied: `human` for patients, volunteers or people; `other` for an
    organism not in the list, named in `notes`; `null` when the abstract doesn't say.
  - `anatomy`: the organ, tissue or body part the finding is limited to, as the abstract names
    it ("liver", "prefrontal cortex"); `null` if none. A cell type or cell line goes in
    `notes`.
  - `notes`: what else limits the claim and has no slot ("Narrowings with no slot", below), in
    a few words; `null` if nothing.
- **`evidence_sentence`**: the quote that states the finding ("Quotes", below).

## Each abstract

Give each abstract of the request one entry, `A1`, `A2`, ... in order, even one with no claim:

- **`study_design`**, of the study the abstract reports: `meta_analysis`, `systematic_review`,
  `rct` (randomized controlled trial), `cohort` (a group followed over time, or a
  non-randomized trial), `case_control`, `cross_sectional` (a survey or a one-time measurement
  in a population), `animal` (experiments in animals), `in_vitro` (cells, tissues, enzymes,
  binding assays), `case_report` (one patient, a family or a small case series),
  `narrative_review` (a review that isn't systematic, an editorial, a comment or a letter) or
  `computational` (docking, prediction, database mining). For a study with several, give the
  design closest to people (patients before animals before cells). Code uses PubMed's
  publication types where they name a design, so give your own reading anyway.
- **`claims`**: the abstract's claims about X and Y; `[]` when it states nothing about how they
  are related. That is a normal answer, and the commonest.

## Quotes

- Copy the `evidence_sentence` from that abstract's title or text, character for character:
  the same words, spelling, abbreviations, numbers and punctuation. Don't paraphrase, shorten,
  expand abbreviations, fix typos or translate.
- Quote at least a full clause that shows the finding by itself, usually one sentence and at
  most two. If the finding is split, join two spans of the same abstract, in order, with `...`.
  Spans come from the title alone or from the abstract alone, never both.
- The quote must contain the finding (the effect, outcome or conclusion, or the relationship
  stated as known), not just the design or that patients were given A, and must name both
  entities, by a listed name or synonym. If the finding's sentence says "the patients", "these
  genes" or "the drug", join it with the span that names the entity, or quote the title if the
  title states the finding.

{{include claim-rules.md}}

## Reply

- Go through the abstracts one at a time, and read each in full, background and conclusion
  included, before you write its claims. The claim most often missed is a single sentence
  stating the relationship in a paper about something else.
- One entry per abstract, named by its id (`A1`), with its study design and its claims.
- One claim per distinct finding. A finding stated twice (in the results and the conclusion) is
  one claim: quote the clearer sentence, the authors' own result over a background sentence.
- `none` for each qualifier slot you don't set, and `null` for each other field you don't set.

## Example

An example request, shortened, and its reply. It is not part of the request you answer.

```text
# Entities

X: pioglitazone (CHEBI:8228)
  category: SmallMolecule
  synonyms: Actos
Y: amyotrophic lateral sclerosis (MONDO:0004976)
  category: Disease
  synonyms: ALS; motor neuron disease

# Predicates

ameliorates_condition, applied_to_treat, beneficial_in_models_for, contraindicated_in, exacerbates_condition, in_clinical_trials_for, in_preclinical_trials_for, preventative_for_condition, promotes_condition, studied_to_treat, treats, treats_or_applied_or_studied_to_treat

# Qualifiers

None of the predicates take qualifiers for this pair.

# Abstracts

## A1 (PMID:111, 2012)
Publication types: Randomized Controlled Trial; Journal Article
Title: Pioglitazone in amyotrophic lateral sclerosis: a randomized placebo-controlled trial.
Abstract: Pioglitazone prolonged survival in the SOD1-G93A mouse model of ALS. We randomized 219 patients with ALS to pioglitazone 45 mg/day or placebo, both added to riluzole. The trial was stopped for futility: pioglitazone did not improve survival compared with placebo (HR 1.21, 95% CI 0.71-2.07). ...

## A2 (PMID:222, 2020)
Publication types: Review; Journal Article
Title: Metabolic drugs in neurodegeneration.
Abstract: ... Patients with ALS often have altered glucose metabolism, and pioglitazone is widely prescribed for type 2 diabetes. ...

Give the study design and the claims about X and Y of every abstract (A1 to A2).
```

The reply, with every qualifier slot `none` (`{...}`):

```json
{
  "abstracts": [
    {"abstract": "A1", "study_design": "rct", "claims": [
      {"polarity": "negative",
       "statement": {"subject": "X", "predicate": "treats", "qualified_predicate": null, "qualifiers": {...}},
       "also_supports": [{"subject": "X", "predicate": "in_clinical_trials_for", "qualified_predicate": null, "qualifiers": {...}}],
       "claim_role": "primary_finding", "hedged": false, "negative_finding_type": "demonstrated_absence",
       "context": {"species": "human", "anatomy": null, "notes": "45 mg/day, added to riluzole"},
       "evidence_sentence": "We randomized 219 patients with ALS to pioglitazone ... pioglitazone did not improve survival compared with placebo"},
      {"polarity": "positive",
       "statement": {"subject": "X", "predicate": "beneficial_in_models_for", "qualified_predicate": null, "qualifiers": {...}},
       "also_supports": [],
       "claim_role": "background", "hedged": false, "negative_finding_type": null,
       "context": {"species": "mouse", "anatomy": null, "notes": "SOD1-G93A model"},
       "evidence_sentence": "Pioglitazone prolonged survival in the SOD1-G93A mouse model of ALS."}
    ]},
    {"abstract": "A2", "study_design": "narrative_review", "claims": []}
  ]
}
```

A1's trial is a negative `treats` claim: placebo-controlled, so `demonstrated_absence`. It also
shows that pioglitazone was tested in a clinical trial for ALS, which the negative claim can't
say, so that goes in `also_supports`. The sentence with the finding doesn't name ALS, so the
quote joins it to the sentence that does, with `...`; riluzole and the dose don't change the
claim and go in `notes`. The mouse result is a claim of its own, at its own level
(`beneficial_in_models_for` in mouse, not `treats`), and the authors state it as known, so it is
`background`. A2 names both entities without relating them: no claims.
