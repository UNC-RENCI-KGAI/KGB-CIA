---
name: evidence-check
description: Check graph edges against the literature they cite. Write one multiple-choice question for a kind of edge, gather the PubMed abstracts each edge cites, have Jev (TypeSafe's typed-decision model) answer it for every abstract, and turn the answers into drop and flag lists that the cleaning stage applies. Use after merging, for any predicate whose edges cite publications.
---

# Evidence check

An edge that cites a paper is only as good as that paper. This skill asks, for each edge and
each abstract it cites, whether the abstract supports, contradicts or says nothing about the
edge. Edges the literature rejects go on a drop list that `kgb-clean` removes on every later run;
edges it shows are real but wrong in a detail go on a flag list, and `kgb-clean` writes the flag
and its reason onto the edge, where the MCP interface shows them to the user.
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
  `subject` and `object` (name, other names from NodeNorm, and identifier), and `claim` (the
  edge in words).
- `options`: the possible answers, each with a `role`. An option that means the edge is right
  `supports` it, one that means it's wrong `contradicts` it, and "the text doesn't say" is
  `silent`. When the text shows the edge is real but a detail is wrong (the drug acts on the
  target, but as an agonist where the edge says antagonism), give that `contradicts` option a
  `flag` and a `flag_reason`: the edge is kept and flagged instead of dropped, so the graph
  doesn't lose a real link over a wrong qualifier.

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

- `gather_evidence.py QUESTION [GRAPH]` reads the latest stage's graph there is:
  `data/augmentation`, else `data/filter`, else `data/cleaning`, else
  `data/transformation/merged`; it prints which. That is the graph the final cleaning pass
  applies the drop lists to. Pass a graph directory to check another one, such as one source's
  ORION output (`normalized_edges.jsonl`). Edges the augmentation stage already checked against
  PubMed or proposed (they carry `augmentation`) are skipped and counted, so none is paid for
  twice. It fetches abstracts from PubMed (NCBI E-utilities, no key needed) and caches them,
  so reruns only fetch new ones.
- `--limit N` checks a fixed, spread-out sample of N edges. Always start with one: read about
  ten answers in `verdicts.jsonl` against the abstracts, fix the question if Jev misreads it, and
  only then run without `--limit`.
- `judge_evidence.py` needs `TYPESAFE_API_KEY`, from `.env` or the environment. Without it, it sends nothing:
  it writes the exact requests to `requests.jsonl`, estimates the cost, and says where the key
  goes. Tell the user that, and carry on with the rest of the pipeline; the check is optional.
  It is resumable, and a failed call is retried on the next run.
- `build_drop_list.py` writes `verdicts.jsonl` (every edge, its verdict, and the answer for each
  article), `data/cleaning/drop_lists/<name>.jsonl` and `data/cleaning/flag_lists/<name>.jsonl`,
  which `kgb-clean` applies. It also writes `gold_set.tsv` beside `verdicts.jsonl`: one KGX TSV
  row per judged edge with `verdict` (`prune` for a dropped edge, `keep` for the rest, flagged
  and unconfirmed included), `confidence`, and the finer `evidence_check` verdict and `flag`.
  This is the form the challenge scores pruning in; hand it over when a user asks for the
  verdicts as a gold set.

Jev costs $0.042 per million input tokens: 1,257 DrugCentral drug-target abstracts come to about
a million tokens, or 4 cents.

## How verdicts are reached

Each article's answer is summed by role. An article supports or contradicts the edge when that
role's probability reaches `threshold` (default 0.7); flagging options count toward
contradicting.

- **Any article supports it:** kept (`keep`).
- **An article contradicts it and none supports it:** dropped, or flagged with the option's flag
  when the flagging options carry more of the probability than the dropping ones.
- **Every article is silent:** a curated edge is kept as `unconfirmed`. A text-mined edge
  (`agent_type` text_mining_agent, or `knowledge_level` text_co_occurrence) is flagged
  `unsupported_by_abstract`: its text is its only basis, but "not stated in the abstracts we
  read" is not "wrong", so it is kept for the user to weigh rather than dropped as the TMKP
  filter does.

The gold set's `confidence` is the judge's probability, from the article that says most, for
what the verdict rests on: support for `keep`, contradiction for `prune`, the edge being real
for a wrong-detail flag. An `unconfirmed` edge is kept with its (low) support probability:
nothing contradicts it, but nothing confirms it either.

Report the counts of keep, unconfirmed, flag and drop to the user with two or three dropped or
flagged edges and the abstract sentences behind them, so they can judge the call. To try another threshold,
change it in the question file and rerun `build_drop_list.py`; Jev isn't asked again.

## Measure precision

Before you report how well the check works, have a person label a sample:

```bash
uv run python $S/label_sample.py QUESTION          # draws 50 edges into label_sample.tsv
uv run python $S/label_sample.py QUESTION --score  # once the `label` column is filled
```

The sample takes every flagged and dropped edge (up to half of it), then splits the rest
between keep and unconfirmed, one article per edge, shuffled. It is blind: the judge's answers
go to `label_key.tsv`, so don't show the person that file. The person reads the abstract and fills
`label` with the role it has for the edge (`supports`, `contradicts`, `silent`, or a flag name),
which `--score` compares with the judge's, by verdict. Use `--size` when a different sample size
is wanted (more when the result will be reported per verdict), and `--seed` only to draw a fresh
sample instead of the same one again. Report agreement by verdict with the sample size, and keep
the labelled file out of git, with the run's other outputs.

## Limits

- Only the abstract is read, not the full paper. Results in tables or figures look `silent`.
- Other names come from NodeNorm. A code name NodeNorm doesn't know (SAR 1118 for lifitegrast)
  still makes a paper look silent.
- Precision is unmeasured until someone labels a sample (see "Measure precision").
