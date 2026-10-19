# AGENTS.md: evaluation/

The evaluation pipeline is run by the team, not by the agent. Do not add it to `STAGES` in
`shared/pipeline.py`, and do not make any stage under `agents/` call it: "evaluation stays
outside the agent" is a team design rule.

It compares GNN / GCN / graph-embedding link prediction on our final graph against the Pharos
baseline. The two runs must use an identical train/test edge split, or the comparison means
nothing.

Which Pharos artifact counts as the baseline is still an open question (see the open-question
issue and `docs/agent_plan.md`). Don't pick one silently; record the choice and the baseline
numbers in `README.md` once they exist.
