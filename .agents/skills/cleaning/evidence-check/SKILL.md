---
name: evidence-check
description: Check graph edges against the literature they cite. Write one multiple-choice question for a kind of edge, gather the PubMed abstracts each edge cites, have Jev (TypeSafe's typed-decision model) answer it for every abstract, and turn the answers into a drop list that the cleaning stage applies. Use after merging, for any predicate whose edges cite publications.
---

# Evidence check

An edge that cites a paper is only as good as that paper. This skill asks, for each edge and
each abstract it cites, whether the abstract supports, contradicts or says nothing about the
edge. Edges the literature rejects go on a drop list that `kgb-clean` removes on every later run.
It works for any predicate: you write the question, and the scripts do the rest the same way
each time.

The method follows the Translator TMKP treats filter (NCATSTranslator/translator-ingests#560):
one multiple-choice question per piece of text, put to Jev, which returns a probability for each
option instead of free text. A model judges each piece of evidence once. The drop list is a
fixed file, so the graph can be rebuilt without calling the model again.

## 1. Pick the edges

Look at the merged graph's predicates and sources (the `describe-graph` skill lists them) and
choose a kind of edge that cites publications: PMID CURIEs in `publications`, or PubMed links in
any field. Good candidates make a specific claim that an abstract could confirm or refute:

- drug to target, with a mechanism (inhibition, agonism);
- gene to disease;
- chemical to disease (treats, causes).

Membership and hierarchy edges (`has_part`, `subclass_of`) are poor candidates.

## 2. Write the question

Write a question file in `data/cleaning/evidence/` (format and a worked example:
[reference/question-format.md](reference/question-format.md)). It has three parts:

- `select`: which edges to check.
- `instructions`: what to judge, using the four things Jev sees: `text` (title and abstract),
  `subject` and `object` (name and identifier), and `claim` (the edge in words).
- `options`: the possible answers, each with a `role`. An option that means the edge is right
  `supports` it, one that means it's wrong `contradicts` it, and "the text doesn't say" is
  `silent`.

Order the options with `contradicts` and `silent` first, because Jev leans toward earlier
options. Always include a `silent` option: most abstracts don't mention every target a paper
measured, and "not in the abstract" is not the same as "wrong". Give each option a description
and one or two short examples in your own words.

## 3. Run it

```bash
S=.agents/skills/cleaning/evidence-check/scripts
uv run python $S/gather_evidence.py QUESTION --limit 50   # a trial on 50 edges first
uv run --env-file .env --script $S/judge_evidence.py QUESTION   # .env holds TYPESAFE_API_KEY
uv run python $S/build_drop_list.py QUESTION
```

- `gather_evidence.py QUESTION [GRAPH]` reads the merged graph (default
  `data/transformation/merged`). Pass a graph directory when the edges you want to check are in
  one source's ORION output instead (`normalized_edges.jsonl`). It fetches abstracts from PubMed
  (NCBI E-utilities, no key needed) and caches them, so reruns only fetch new ones.
- `--limit N` checks a fixed, spread-out sample of N edges. Always start with one: read about
  ten answers in `verdicts.jsonl` against the abstracts, fix the question if Jev misreads it, and
  only then run without `--limit`.
- `judge_evidence.py` needs `TYPESAFE_API_KEY`, from `.env` or the environment. Without it, it sends nothing:
  it writes the exact requests to `requests.jsonl`, estimates the cost, and says where the key
  goes. Tell the user that, and carry on with the rest of the pipeline; the check is optional.
  It is resumable, and a failed call is retried on the next run.
- `build_drop_list.py` writes `verdicts.jsonl` (every edge, its verdict, and the answer for each
  article) and `data/cleaning/drop_lists/<name>.jsonl`, which `kgb-clean` applies.

Jev costs $0.042 per million input tokens: 1,257 DrugCentral drug-target abstracts come to about
a million tokens, or 4 cents.

## How verdicts are reached

Each article's answer is summed by role. An article supports or contradicts the edge when that
role's probability reaches `threshold` (default 0.7).

- **Curated edges** (anything not text-mined): dropped only when an article contradicts the edge
  and none supports it. When every article is silent, the edge is kept as `unconfirmed`.
- **Text-mined edges** (`agent_type` text_mining_agent, or `knowledge_level`
  text_co_occurrence): the text is the edge's only basis, so an edge no article supports is
  dropped. This matches the TMKP filter.

Report the counts of keep, unconfirmed and drop to the user with two or three dropped edges and
the abstract sentences that sank them, so they can judge the call. To try another threshold,
change it in the question file and rerun `build_drop_list.py`; Jev isn't asked again.

## Limits

- Only the abstract is read, not the full paper. Results in tables or figures look `silent`.
- Names come from the graph. Papers that use a code name (FYX-051 for topiroxostat) can look
  silent; Jev sees the identifier too, but not synonyms.
- There is no labeled sample for these questions yet, so precision is unmeasured. For a result
  you report, label about 50 articles by hand and compare.
