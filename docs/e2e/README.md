# End-to-end runs

One folder per run, named by date, holding the observer's report and the small reports it
cites. Graph files and logs never go here (see `.gitignore`); they stay in the team bucket.

- `2026-11-12/`: ClinVar, Monarch and eRAM from three raw RoboCroissant files to a Neo4j graph on
  the kgb-graph host, two simulated users, defects D1-D20. Tracking issue #81.
