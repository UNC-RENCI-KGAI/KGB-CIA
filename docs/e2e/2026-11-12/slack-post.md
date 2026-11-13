# Slack post for the Nov 12 end-to-end run

Paste the block below as one message.

---

🚀 We ran the whole pipeline end to end today: three raw metadata files (ClinVar, Monarch, eRAM) → every agent → a queryable Neo4j graph on our AWS host.

⏱️ 2 h 42 min wall clock, 4.5 min of host compute, 💸 $5.28 of model calls. User B's cardiomyopathy graph is live: 1,435 nodes, 4,681 edges, 82 of them proposed from the literature 📚

🔍 It also found 20 things worth fixing, three of them blockers. Six PRs are up and six child issues are open, all hanging off one tracking issue:
https://github.com/UNC-RENCI-KGAI/KGB-CIA/issues/81

🧊 Scope, to be clear: the **Nov 9 graph stays our frozen demo and submission graph**, and nothing here changes that. The tracking issue and its PRs are improvements to the pipeline on `main`, for after the submission or whenever a team has room. Each team decides what to carry over and when. PR #80 writes the rule down: fixes are welcome on main, the submission graph stays Nov 9.

📄 The full observer report is in PR #83. If an issue is in your area, it is yours to take or to park 🙌
