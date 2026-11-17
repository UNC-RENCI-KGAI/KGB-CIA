# Evaluation pipeline

**Team:** @UNC-RENCI-KGAI/evaluation · Not an agent: runs outside the agent, by the team.

Run GNN / GCN / graph-embedding link prediction on the final graph and on the Pharos baseline
with an identical train/test edge split, and report the comparison.

Not started. The protocol is planned in
[#54](https://github.com/UNC-RENCI-KGAI/KGB-CIA/issues/54): several splits, a degree-only
baseline, and results split by whether both endpoints were seen in training. The baseline is
Pharos v319 from the [Pharos archive](https://opendata.ncats.nih.gov/public/pharos/archive/),
settled in [#78](https://github.com/UNC-RENCI-KGAI/KGB-CIA/issues/78), where the runs are
tracked. Record the baseline numbers here once they're final.
