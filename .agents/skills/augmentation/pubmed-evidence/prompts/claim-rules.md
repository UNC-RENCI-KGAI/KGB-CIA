## Which entities count

- A listed synonym, abbreviation, code name, brand name or MeSH heading is that entity when the
  abstract uses it in that sense (gene aliases are often ambiguous). So are a drug's salts,
  esters and formulations (imatinib mesylate), a gene's products (protein, transcripts,
  isoforms) and variant or mutant forms, the same gene in another species (mouse Abl1 for
  ABL1), a subtype of the disease (chronic-phase CML for CML), and a fusion protein when the
  finding concerns the gene's part (imatinib inhibiting the ABL1 kinase of BCR-ABL1).
- Not the entity, so no claim: another drug, even of the same class, or a class named without
  the drug ("tyrosine kinase inhibitors"); the racemate of a drug named as one enantiomer
  (rabeprazole for dexrabeprazole); an ingredient the abstract doesn't name; another gene of the
  family or a paralog (ABL2 is not ABL1); a class of genes, a protein complex or another of its
  members ("mutations in this complex"); a broader disease (leukemia for CML, inflammatory bowel
  disease for Crohn disease); an acquired form of a congenital disease; a diagnosis the authors
  go on to revise; a related but different disease.
- A metabolite or other derivative of a drug is the drug with its derivative qualifier
  (`subject_derivative_qualifier: metabolite`), where the predicate takes one. Where it
  doesn't, a finding about the derivative is no claim.
- When an abstract links several genes and diseases without saying which goes with which
  ("mutations in A or C cause B or D"), it makes no claim about any one pair; don't resolve it
  from your own knowledge.
- An entity given only as an identifier, with no name or synonym, can't be recognized in an
  abstract: no claim, unless the abstract cites the identifier. Never guess it from the paper's
  topic.
- `has_phenotype`: a specific instance of the phenotype counts as the phenotype (increased
  STAT3 phosphorylation for "abnormal protein phosphorylation"); a more general phenotype
  (abnormal neurons for "decreased axon thickness") is another entity, so no claim.

## What counts as a claim

- A claim is a statement the abstract makes about these two entities: the authors' own result
  or conclusion, or established knowledge they assert ("A, an inhibitor of B, ...", "A is
  approved for B"). One sentence anywhere in the abstract is enough: a paper about something
  else (a method, an animal model, another disease, an adverse effect) that says in passing
  that A treats B, or that mutations in A cause B, makes that claim.
- Background assertions count, as `claim_role: background`. A hypothesis, a proposal or the
  question a study asks ("A may be a target for B", "we asked whether A ...") counts as
  `claim_role: hypothesis`. Doubts and criticisms ("remains controversial", "the evidence is
  weak") are not claims for or against anything.
- The role follows the sentence you quote. When the abstract states the relationship as known
  and also reports the authors' own result that shows it, quote the own result: a case report
  or cohort that finds the gene's variants in patients and attributes the disease to them is a
  `primary_finding`, not its background sentence. Use the background sentence when the study's
  own results don't bear on the relationship (a therapy, model or mechanism study; a trial with
  A only as the comparator).
- The evidence sentence must contain the finding (the effect, outcome or conclusion, or the
  relationship stated as known) and name both entities.
- Silence is not a claim. An abstract that names both entities without relating them, lists
  them together (keywords, a list of drugs and diseases), or reports something about only one
  of them makes no claim.

## Negative claims

- A negative claim needs explicit language that the relationship is absent: the authors looked
  for it and report it absent ("A did not inhibit B", "no association was found between A and
  B", "A was no better than placebo in patients with B"), or they assert as established that
  it doesn't hold ("A's known ineffectiveness in B"). A non-significant result reported as the
  finding counts.
- A placebo-controlled trial that finds A no better than placebo is a negative claim with
  `demonstrated_absence`.
- A drug studied for B that didn't work is a negative `treats` claim, however it's worded ("A
  has been shown not to treat B", "A failed to demonstrate efficacy in B", "A has not been found
  useful in B", "no evidence of benefit was found"). It also supports `in_clinical_trials_for`
  when it was tried in patients, or `in_preclinical_trials_for` in animal models.
- "Has been shown not to" is a negative. "Not studied", "there are no data" and "has not been
  shown to" only say the evidence is missing: no claim, unless the abstract says A was studied
  for B.
- A negative the authors state in general terms, in a sentence that names A ("A is not
  effective for B"), counts even when their own study is narrower; the narrowing goes in
  `context.notes`.
- Not negatives at all, so no claim against `treats`: advice against A for safety, cost or
  approval status; "A is not used for B"; another drug recommended instead ("B requires C"), or
  a finding that only another drug works; one patient's treatment failure; resistance in
  isolates or in surveillance (susceptibility tests, resistance rates). A treatment failure in
  a studied group (a trial, a cohort, a case series) is a negative `treats`.
- A negative about another entity, or about a different relationship between these two
  ("A did not change B expression" against binding), denies only what it says.

## Treatment

- `treats`: A works as a treatment for B in people: an approved or standard therapy, the
  authors presenting it as a treatment for B ("imatinib therapy for CML"), or efficacy in
  patients with B. Efficacy only in animal models or cells is `beneficial_in_models_for`,
  however it's worded; a trial with no reported outcome is `in_clinical_trials_for`; a benefit
  only in a subgroup or a post hoc analysis is a hedged claim with the subgroup in `notes`.
- Prevention is treatment: A preventing B, whether in people who don't have it (pre- or
  post-exposure prophylaxis, preventing a first event) or as relapse, attacks or complications
  in patients who have it (maintenance of remission), is `preventative_for_condition`, a kind
  of `treats`. A prophylaxis trial in which A fails is a negative claim like any other.
- `ameliorates_condition`, also a kind of `treats`: A relieves B's symptoms or severity without
  the abstract presenting it as a treatment of B.
- A combination or regimen containing A (a fixed-dose product, A with another drug) that works
  in patients with B supports `treats` for each drug in it, A included; the combination goes in
  `notes`. Efficacy counts wherever the abstract reports A's own effect: A as the active
  comparator that worked, or among the drugs a network meta-analysis finds better than placebo.
  A result pooled over patients on different drugs (a class meta-analysis) is no claim about
  any one of them.
- Against an active comparator, a known effective treatment for B: A superior or non-inferior
  to it supports `treats`. A inferior to it is a positive `treats` claim if A also worked
  (against placebo or baseline), with the comparison in `notes`; otherwise no claim. A not
  superior to it (a failed superiority trial) says nothing about `treats`. Against a placebo or
  an untested drug, the ordinary rules apply: A no better than placebo is a negative claim.
- The benefit must be on B: an effect on another condition of the patients (weight loss in type
  2 diabetes is not glycaemic control) or an adverse effect is no `treats` claim.
- `in_clinical_trials_for`: A was tested in a clinical trial in patients with B, whatever the
  result; `in_preclinical_trials_for`: tested in animal models of B; `beneficial_in_models_for`:
  efficacy in an animal or cell model of B. `studied_to_treat` and
  `treats_or_applied_or_studied_to_treat`: A used or studied as a treatment for B, in any way.
  A trial's or a model study's own claim, positive or negative, also supports the matching one
  of these in `also_supports` when the claim doesn't already imply it.
- `contraindicated_in`: A should be avoided in patients with B because it harms them or
  worsens B. Its negative needs the authors to conclude that A is safe in B or that the
  contraindication is unwarranted; a benefit alone, or no harm in a small series, isn't one.
  `exacerbates_condition`: A worsens B. `promotes_condition`: A causes or raises the risk of B.

## Narrowings with no slot

- Formulation, combination, endpoint, stage or subtype of the disease, line of therapy,
  population, dose, schedule, cell type or cell line, comparator and time point don't change
  the claim: it stays at the statement the predicate and its qualifiers can express, and the
  narrowing goes in `context.notes`. Every trial has a dose, a population and endpoints; they
  don't need a note unless the abstract limits the finding to them.
- A marginal effect ("barely or no more effective than placebo", a low cure rate) is a
  `hedged` claim.

## What the predicates claim

- **Chemical and gene** (`affects`, `directly_physically_interacts_with`, `binds`, ...): the
  chemical binds, inhibits, activates or otherwise changes the gene's product (its activity,
  expression, abundance, ...). `directly_physically_interacts_with` and `binds` need direct
  binding or direct action on the protein: a binding or enzyme assay, a Kd, Ki or IC50 against
  the protein (not a cell-growth IC50), a crystal structure, "an inhibitor of B", "binds to B".
  A change in B's expression or in downstream signaling alone is `affects`, not binding.
- **Gene and disease**: `gene_associated_with_condition` and `associated_with`: the gene (its
  variants, mutations, expression, protein levels or activity) is associated with the disease,
  causally or not. `genetically_associated_with`, `genetic_association`: genetic evidence
  (variants, mutations, polymorphisms, GWAS, linkage); expression or protein findings alone
  aren't. `associated_with_increased_likelihood_of`: the gene's variants or alterations raise
  the risk of the disease; `associated_with_decreased_likelihood_of`: they lower it
  (protective). `correlated_with`, `positively_correlated_with`, `negatively_correlated_with`:
  a measured correlation, in its direction. `biomarker_for`: used or validated as a marker of
  the disease (diagnosis, prognosis, response); one only proposed as a marker is a
  `hypothesis`. `causes`: alterations of the gene (mutations, deletion, loss, overexpression)
  cause the disease. `contributes_to`: the authors state that the gene or its alteration
  contributes to, drives, promotes or predisposes to the disease. A finding reported only as an
  association or correlation is `associated_with` or `correlated_with`, never `causes`, and a
  model organism that fails to reproduce the disease is no claim against `causes`.
- **Mendelian evidence**: the authors finding pathogenic or disease-causing variants of the gene
  in affected patients or families (a case report, a family, a diagnostic cohort, "causal
  genes", a mutation segregating with the disease) is a `causes` claim, even when it is worded
  "associated" or "identified in". So is "B, caused by A deficiency". Not enough for `causes`:
  a gene only tested on a panel or added to one, a variant of uncertain significance, patients
  described as having both the variant and the disease without the authors linking them, a
  disease merely named after the gene ("HNF4A-MODY") or an adjective ("A-associated B"), known
  variants used as materials in a study of mechanism or treatment, variants counted across a
  cohort of several diseases without saying which patients had B, or a population association
  (GWAS, allele frequencies in cases and controls), which is `associated_with` or
  `genetically_associated_with`. A variant found not to cause the disease (benign, not
  segregating), or no mutation found in some patients, is no negative claim: the gene can still
  cause the disease.
- **Gene and gene**: `physically_interacts_with`: the proteins bind, form a complex,
  co-immunoprecipitate, or one directly modifies the other (phosphorylates, ubiquitinates,
  cleaves). `interacts_with`: any physical or functional interaction, including one regulating
  the other; `regulates`: one changes the other's activity or amount. Co-expression,
  correlation or a shared pathway is no claim.
- **Gene and phenotype**: `has_phenotype`: mutation, deletion, knockout, knockdown,
  overexpression, or loss or gain of function of the gene, in patients or animal models,
  produces the phenotype or a specific instance of it.
- **Any other predicate**: read its name literally and apply the same principles.

## Subject, object and qualifiers

- The subject acts on the object: "B regulates A" says nothing about "A regulates B", so its
  statement has B as the subject. `interacts_with`, `physically_interacts_with`,
  `directly_physically_interacts_with`, `binds` and the correlations are symmetric: either
  order states the same.
- `subject_...` and `object_...` qualifiers describe the subject and the object as written,
  even for a symmetric predicate: put a change in a gene's product on the side of that gene.
  They describe a gene, gene product or chemical, never a disease or phenotype.
- What the slots mean:
  - `..._aspect_qualifier`: what about the entity changes: activity, expression, abundance,
    secretion, ... ("decreased **activity** of B"). Use the aspect the abstract names (lower
    mRNA levels: `expression`; inhibited kinase activity: `activity`; less phosphorylation:
    `phosphorylation`); don't infer one aspect from another. Use `activity` when the abstract
    names activity or function (reduced enzyme activity, inactivating mutations) and
    `abundance` when it names the amount (absent protein, depletion).
  - `..._direction_qualifier`: which way it changes (increased, decreased, ...).
  - `..._form_or_variant_qualifier`: a form of the entity: the kind of variant the abstract
    names for it (loss of function, inactivating or haploinsufficient; gain of function or
    activating; dominant negative) or a null allele.
  - `..._part_qualifier`: a part of a gene, such as its promoter; `..._derivative_qualifier`:
    a derivative, such as a drug's metabolite.
  - `causal_mechanism_qualifier`: how the subject acts on the object (inhibition, agonism,
    binding, ...), no more specific than the abstract ("inhibits" is `inhibition`, not
    `competitive_inhibition`).
  - `qualified_predicate: causes`: the subject causes the change its qualifiers name ("A
    causes decreased activity of B"). Only with an aspect or a direction qualifier (never with
    a form or a mechanism alone), and only for a causal finding (an experiment or
    intervention, or the authors concluding it), never for a correlation.

## Genes, variants and qualifiers

- A gene-to-disease or gene-to-phenotype statement already means the gene's variants. Bare
  "mutations in A", "a pathogenic variant in A", or the disease's name ("activated PI3K delta
  syndrome") give no form, aspect or direction, and neither does a complex or family the gene
  belongs to: "mutations in A cause B" is just "A causes B". Give a form only when the abstract
  names a specific kind (loss of function, gain of function, dominant negative, null).
- Truncating or nonsense variants are loss of function only when the abstract's context says so
  (the protein is absent or reduced, haploinsufficiency, nonsense-mediated decay, a disease
  caused by loss of the gene's function). Without that context they are plain mutations.
- A "deficiency", "lack", "loss" or "absence" of an enzyme or protein names neither activity
  nor amount: `activity_or_abundance`, decreased.
- An experiment that lowers or raises the gene's product is an aspect and a direction, not a
  form: knockdown (morpholino, siRNA) or depletion is `abundance` decreased (`expression`
  decreased if the abstract reports lower mRNA), and overexpression or mRNA injection is
  `expression` increased.
