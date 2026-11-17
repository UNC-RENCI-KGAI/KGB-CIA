# Metadata agent

Stage 1. Works out what each source file means: which columns hold which kinds of node, what
relationship a row states, which identifier prefixes and codes it uses, and how the file is laid
out. It builds no graph itself; the transformation stage builds one from what it writes.

## How it works

Each source arrives with a [Croissant](https://mlcommons.org/croissant/) file from
[RoboCroissant](https://github.com/RoboBoulangerie/robo-croissant-pipeline), which describes the
files and columns but not their meaning. The
[`croissant-semantics`](../../.agents/skills/metadata/croissant-semantics/) skill has an agent
add the meaning, in Biolink terms: node categories, predicates, identifier prefixes, units, code
values and file dialects. It writes those choices as a plan, and its scripts check the plan
against samples of the real data and against NodeNorm before anything is built. The agent
chooses the mapping; code applies it, so the result can be reproduced.

## Run it

Ask an agent (Pi or Claude Code) to add semantics to a croissant, and it follows the skill's
`SKILL.md`. The skill's [README](../../.agents/skills/metadata/croissant-semantics/README.md)
explains how to run its scripts by hand.

`kgb-metadata`, which would run the skill for every source in one command, isn't implemented
yet. For now the skill runs once per source.

## Input / output

**Input:** a RoboCroissant `croissant.json` per source, kept in `data/cache/croissant/`.

**Output:** per source, `croissant.semantic.json` with `semantics_plan.json` and the check
reports. The transformation stage reads these (see
[`agents/transformation/README.md`](../transformation/README.md)).

## Limitations

- The skill runs one source at a time; there's no batch command yet.
- It can only describe the files a source's croissant lists. If the croissant leaves out a file,
  as Monarch's currently does, that file can't be in the graph.
