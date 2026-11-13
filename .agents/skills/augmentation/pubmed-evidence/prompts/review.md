# Reviewing claims taken from abstracts

A first-pass model read PubMed abstracts about pairs of entities in a biomedical knowledge graph
and wrote down the claims each abstract makes about how the two are related. Code compares the
claims with the graph's edges, and the claims you get would count: an upheld claim adds its PMID
as support to each edge of the graph whose statement it states or narrows, and a claim more
specific than such an edge proposes a new edge with its statement. A negative claim counts only
through the positive statements it also supports. So the claims come to you first. Read each
claim against its abstract and answer **`upheld`** or **`reject`**. A rejected claim counts for
nothing.

The question for each claim: **does the abstract state this, at this level, with this
polarity?** Be strict and literal. Uphold only what the abstract itself states about these two
entities. A wrong rejection loses one claim; a wrong uphold can put a false edge in the graph.

## Reading a claim

- A claim's `polarity` says which way it goes: a positive claim says its statement holds, a
  negative claim says the abstract reports that it doesn't. The statement is always written as
  the positive one: a negative claim with "A treats B" says that A does not treat B.
- The statement has a subject, a predicate and qualifiers, in Biolink terms. The item gives it
  in words and structured. `also_supports` lists further positive statements the same finding
  shows, such as a failed trial that still shows A was tested in a clinical trial for B; they
  are part of the claim.
- `claim_role` says what the quoted sentence states: the authors' own result or conclusion
  (`primary_finding`), knowledge they assert or another study they cite (`background`), or a
  hypothesis or question (`hypothesis`). `hedged` marks a finding worded as a possibility or a
  marginal effect. A negative claim's `negative_finding_type` says whether the study was built
  to show the effect and didn't find it (`demonstrated_absence`) or only saw nothing
  (`failed_to_detect`).
- `context` says where the claim holds: the species studied, the organ or tissue, and in
  `notes` whatever else narrows it (a population, a dose, a cell line).
- Judge the claim on the whole abstract. The evidence sentence shows where the first pass
  looked, and code has checked that it is in the abstract, but it is not evidence by itself. A
  sentence taken out of context (background the authors go on to dispute, the question the study
  asks, a result about another entity in the same sentence) doesn't state the claim.
- Code has also checked that the qualifiers fit the predicate. You judge what the abstract says.

## Uphold or reject

Uphold a claim only if the abstract states all of it: its statement and each statement it also
supports, at the level given, with its polarity, about these two entities in these roles.

Reject it when:

- the abstract doesn't state it: it is inferred from what the abstract leaves out, from the
  paper's topic or from another finding, or it reads a hypothesis or a question as a finding;
- the abstract only poses it as a hypothesis or a question, whatever its `claim_role`,
  `hypothesis` included (a hedged finding, or background the abstract asserts, is not a
  hypothesis);
- it is about safety, cost, availability or approval status rather than whether the
  relationship holds;
- it has the opposite direction or effect, or the subject and object the wrong way round;
- it names the wrong entity: another drug, gene or disease, a class or family, a broader
  disease, or a metabolite without its derivative qualifier;
- it is more specific than the abstract: a narrower predicate, or a qualifier (aspect,
  direction, form, mechanism) or `causes` that the abstract doesn't give, or `treats` for a
  result in animals or cells;
- it is negative without explicit language that the relationship is absent: "not studied",
  "no data" and "has not been shown to" only say that evidence is missing;
- its species or tissue contradicts the abstract, such as a mouse study given as human.

A claim that says less than the abstract is not wrong: uphold one with a more general predicate
or fewer qualifiers than the abstract would allow, or marked `hedged` or `background` when it
needn't be. A narrowing that has no slot (a dose, a formulation, a population, an endpoint)
belongs in `notes`, not in the statement, as the rules below say; it doesn't make the claim
wrong.

## The rules the first pass followed

Judge each claim by the rules the first pass was given.

{{include claim-rules.md}}

## Input and reply format

The message lists the pair's two entities (id, name, category, other names). Then come the
claims, each headed by its id (R1, R2, ...) and the PMID of its abstract, with the claim in
words, each statement it also supports in words, the claim structured, and its evidence
sentence. Last come the abstracts the claims are from, each with its PMID and year, PubMed's
publication types, the title and the text.

Give exactly one verdict per claim, under its id: `upheld` or `reject`, with a reason of one or
two sentences on what in the abstract decided it.
