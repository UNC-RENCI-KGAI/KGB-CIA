# AGENTS.md: shared/

Code every stage depends on, owned by the leads. A change here can break all six stages at
once, so it needs the `integration` label and review from the teams on each side of any handoff
it touches.

- `schema.py` is the contract between stages. Prefer adding optional fields with defaults over
  renaming or removing fields, since each stage reads and writes these records independently.
  Keep `primary_knowledge_source`, `knowledge_level` and `agent_type` required on `Edge`, and
  keep `Node` and `Edge` flat and Biolink-named, like ORION's KGX output. `tests/test_smoke.py` round-trips a `Graph` through
  JSON; extend it when you add a field.
- `pipeline.py` must keep `--dry-run` working with no network and no data: `make demo` runs it
  in CI and must always pass on `main`. Stage order is the `STAGES` list.
- `nodenorm.py` calls the NodeNorm API over the network (default: the public Translator
  instance; set `NODENORM_URL` for the RENCI-hosted one). Tests must not hit it; mock `httpx`
  or test `preferred_id` against a canned response.
