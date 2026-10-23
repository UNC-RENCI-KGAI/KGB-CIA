# Croissant semantics

Adds what the data **means** to an existing Croissant file for a biomedical knowledge base:

- which columns together describe one thing (a gene, a disease), and its Biolink class;
- what a row asserts (a Biolink association and predicate);
- which identifier prefix a column's ids use, units, list separators, code values;
- how to read each file (a dialect: comment lines, header row, delimiter).

It uses only Croissant 1.1 constructs plus a few conventions of our own, and checks the result
against the real data. RoboCroissant (the metadata agent, `agents/metadata/`) describes the files
and columns; this adds the semantics on top.

[`DESIGN.md`](DESIGN.md) explains the approach and the conventions. [`SKILL.md`](SKILL.md) is
the workflow an agent follows.

## How it works

An agent (Claude Code, following `SKILL.md`) studies the croissant and samples the data, then
writes a **semantics plan** (JSON). Scripts apply the plan and check it; the agent never edits
the croissant by hand.

```
inventory.py   ->  layouts (record sets with the same columns), documentation files
sample.py      ->  real values, their shapes, whole-file profiles of code columns
dialects.py    ->  how each file is written (VCF, GAF, MITAB, CTD, GMT, ...)
vocab.py       ->  Biolink classes and slots, prefixes, ontology terms, infores ids, QUDT units
   (agent writes OUT/semantics_plan.json)
apply_plan.py  ->  OUT/croissant.semantic.json
check.py       ->  OUT/check_report.md: terms, prefixes, units, codes, ids against sampled data,
                   the MLCommons validator, records read through mlcroissant, coverage
```

Answers from a knowledge base's maintainers go in `semantics_answers.json` next to its
croissant; the scripts apply them to every plan for that knowledge base.

## Run it alone

The scripts are `uv run --script` files and install their own dependencies. From this
directory:

```bash
scripts/inventory.py path/to/croissant.json
scripts/sample.py path/to/croissant.json --layout L1a2b3c4d
scripts/apply_plan.py path/to/croissant.json OUT/semantics_plan.json OUT/croissant.semantic.json
scripts/check.py path/to/croissant.json OUT/semantics_plan.json OUT/croissant.semantic.json \
    --report OUT/check_report.md --load-records
```

Every script has `--help`. Downloads are cached in `~/.cache/croissant-semantics/`.

## Use it as a skill

It lives with the repository's shared skills, in `.agents/skills/metadata/` (see
[`.agents/skills/README.md`](../../README.md) for how Pi and Claude Code find them). Ask the
agent to add semantics to a croissant; it loads `SKILL.md` and the files in `reference/`.

## Tests

The tests aren't part of the repo's `make test`, because the scripts bring their own
dependencies. Run them from this directory:

```bash
uv run --with pytest --with requests --with jsonpath-ng --with pyyaml --with bioregistry \
    --with openpyxl --with truststore --with pypdf --with pyarrow pytest -q tests/
```

## Status

Tested on 20 knowledge bases from the RoboBoulangerie sandbox, over seven rounds of runs by
agents with no context from its development. Problems the runs found in the input croissants
are filed on `RoboBoulangerie/robo-croissant-pipeline`.
