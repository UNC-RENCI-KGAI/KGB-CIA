# Question file format

One JSON file per kind of edge to check. `question.py` checks it before any script runs and
names every problem it finds.

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | Lowercase letters, digits and `_`. Names the work directory (`data/cleaning/evidence/<name>/`) and the drop list (`data/cleaning/drop_lists/<name>.jsonl`). |
| `select` | yes | Which edges to check: edge fields and the values allowed for each, usually `predicate` and `primary_knowledge_source`. A value can be a string or a list. Every key must match. |
| `instructions` | yes | The question, in terms of what Jev sees: `text` (title and abstract), `subject` and `object` (name and identifier, as `name [CURIE]`) and `claim` (the edge in words, qualifiers in brackets: `imatinib interacts with (inhibition) ABL1`). End with "Judge only what `text` says." |
| `options` | yes | The answers, in order, each with `name`, `role` (`supports`, `contradicts` or `silent`), `what` (when to choose it) and optionally `examples`. At least one must support, and at least one must contradict or be silent. |
| `threshold` | no | Probability a role must reach for an article to count as supporting or contradicting. Default 0.7. |
| `model` | no | Jev version. Default `jev-1.13.0`. |

## Writing good options

- Contradicting and silent options go first; Jev leans toward earlier options.
- Make `silent` cover the common ways an abstract doesn't bear on the edge: one entity isn't
  mentioned, the result is about a different entity, or the two are only listed together.
- Say what counts as support in terms the abstract would use (a Ki, an IC50, "inhibited",
  "associated with"), so Jev doesn't need the predicate's Biolink definition.
- Separate the ways an edge can be wrong when they mean different things for a curator: the
  wrong mechanism, and no effect at all.
- Examples are short paraphrases, not quotes from the abstracts you'll check.

## Worked example: drug to target, with a mechanism

Drug-target edges carry a mechanism as `causal_mechanism_qualifier` (inhibition, agonism, channel
block), which appears in `claim`. This question asks whether the abstract reports the drug acting
on the target in that way.

```json
{
  "name": "drug_target_mechanism",
  "select": {
    "predicate": "biolink:interacts_with",
    "primary_knowledge_source": ["infores:drugcentral", "infores:gtopdb", "infores:chembl"]
  },
  "instructions": "Does `text` report that `subject` acts on `object` the way `claim` says? `subject` and `object` give each entity's name, the other names it goes by (a development code, a gene symbol), and its identifier. Judge only what `text` says, not what is generally known.",
  "options": [
    {
      "name": "different_action",
      "role": "contradicts",
      "what": "`text` reports `subject` acting on `object`, but in a different way from `claim`: an agonist where the claim says inhibition or antagonism, an activator where it says inhibitor, or the reverse.",
      "examples": ["X acted as a partial agonist at the receptor (claim: antagonism)"]
    },
    {
      "name": "no_activity",
      "role": "contradicts",
      "what": "`text` reports that `subject` was tested on `object` and showed no binding or no meaningful effect.",
      "examples": ["no significant inhibition at 10 uM", "did not bind the receptor"]
    },
    {
      "name": "not_reported",
      "role": "silent",
      "what": "`text` does not report `subject` acting on `object`: one of them isn't mentioned, the activity reported is on a different protein, or the two are only listed together.",
      "examples": ["a review of kinase inhibitors that never names this target"]
    },
    {
      "name": "acts_as_claimed",
      "role": "supports",
      "what": "`text` reports `subject` binding or acting on `object` consistently with `claim` (for example a Ki, IC50 or EC50, or a stated inhibition, agonism or channel block).",
      "examples": ["imatinib inhibited ABL1 with an IC50 of 0.1 uM"]
    }
  ]
}
```

On 40 DrugCentral drug-target edges this gathered 41 edge-abstract pairs from 22 papers; the
whole set is 1,248 edges and 1,257 pairs.
