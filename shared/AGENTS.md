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
- `pubmed/` calls NCBI's E-utilities (and NodeNorm, for the pair search) over the network, and
  caches what it fetches in SQLite. Set `NCBI_API_KEY` (10 requests a second instead of 3) and
  `NCBI_EMAIL` (NCBI asks for one); `EUTILS_URL` points it at another server and
  `KGB_PUBMED_CACHE` at another cache file (default `~/.cache/pubmed/pubmed.sqlite`, one per
  user wherever a command runs; the old default, `data/cache/pubmed.sqlite` relative to where
  the command runs, is still used, with a warning, while only it exists). Agents that
  share the cache file share what each fetched. Only one process may write to a cache file at
  a time: SQLite's locking isn't reliable on a network drive, so two people running
  augmentation and the evidence check at once against a cache on the RENCI drive can hit lock
  errors or corrupt it. Run them one after the other, or keep a local cache each and have one
  person refresh the shared copy. Keep the abstract text format (`parse_efetch`)
  and the cache's table and column names as they are: quotes checked against the text, caches
  keyed by it, and the augmentation agent's existing `pubmed.sqlite` depend on them. Tests must
  not hit the network; pass `EUtils` an `httpx.Client` with `httpx.MockTransport` and NodeNorm
  as a fake function, as `tests/test_pubmed.py` does.
