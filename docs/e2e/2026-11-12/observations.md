# End-to-end run of the KGB-CIA pipeline, laptop + AWS host: observations (2026-11-12)

> **Where the cited files are.** Paths written as `$S/...` are the run's working tree on the
> lead's laptop. The small reports they cite (stage timings, graph sizes, augmentation outcomes,
> costs, cut composition, cleaning flags, defects, Neo4j counts) are copied under `data/` beside
> this file. Everything else (runner logs, transformation and cleaning reports, the cut, the
> augmented graphs, the Neo4j dump) is in `s3://kgb-cia-972034964799/e2e/` and
> `neo4j-dumps/e2e-cardiomyopathy-2026-11-12/`, and on the kgb-graph host under `/opt/kgb-e2e/`.
> Tracking issue: #81.

External observer's report for the team lead. Nothing was run or changed by the observer; every
fact cites a file under `$S` =
`/private/tmp/claude-503/-Users-jchung-Documents-HACK-202610-AI-agent-KGbuilding/41d8fad0-4608-4ff5-aa1c-5f9b1350b6e0/scratchpad`.
Plan: `$S/e2e/PLAN.md` (ClinVar, eRAM, Monarch; run A = whole graph capped at 500 pairs,
run B = cardiomyopathy cut, uncapped). Code: main 823ef5e + PR 22 + PR 64 in `$S/wt-e2e`,
plus two commits made during the run (713e460, e3a562b) that exist only there. The local
report `$S/e2e/runs/local-observations.md` (D1-D10) is the baseline; D11-D20 are new.

## 1. Wall-clock timeline (UTC)

Sources: `$S/e2e/report/stages.json`, each run's `timeline.jsonl`, the agents' `timeline.md`,
`$S/e2e/runs/launch_paid.log`, file mtimes under `$S/e2e/` (local = UTC-5), and
`$S/wt-e2e` git log.

| Start | End | Where | What | Wall | Outcome |
|---|---|---|---|---|---|
| 19:57 | | laptop | Raw RoboCroissant files fetched; each a one-element JSON list, unwrapped (`raw-metadata/*.json`) | | |
| 19:58:55 | 20:19:30 | laptop, 3 Claude agents | Metadata (croissant-semantics): ClinVar 12 min, Monarch 14 min, eRAM 21 min | 20.6 min | 3 bundles |
| 20:19:12 | 20:20:08 | laptop | `local-2src`: transform+clean+describe+filter+select on ClinVar+Monarch (started 18 s before eRAM's agent finished) | 56 s | ClinVar 0 edges (D1) |
| 20:20:41 | 20:21:23 | laptop | `local-eram`: eRAM transformed alone, never merged | 42 s | 1,058 MB output (D3) |
| 20:35:14 | | laptop | Commit 713e460: `orion_runtime.py` keeps `#GeneID` (D1 fix); eRAM bundle re-applied as `metadata/eram-v2` | | |
| 20:35:38 | 20:37:30 | laptop | `local-3src`: all three sources; transformation 92 s, cleaning 11.5 s, select 8.3 s | 1 min 52 s | ClinVar 12,923 edges |
| 20:37:48 | 20:40:12 | laptop -> host | Code and bundle tarballs to S3; `bootstrap_host.sh` via SSM (dnf, uv, CPython 3.12, `uv sync`; about 5 min of host time per the lead) | 2.4 min send to first stage | |
| 20:40:12 | 20:42:09 | host (aarch64, 2 CPUs) | `aws-shared`: transformation 86.9 s, cleaning 16.2 s, describe 1.0 s, filter 0.0 s, select 13.1 s | 1 min 57 s | 42,858 nodes / 224,554 edges |
| ~20:32 | 20:42:30 | laptop agent | Focus agent reads `describe/description.md`, writes `focus/cardiomyopathy.json` | 10 min | 127 seeds |
| 20:44:16 | 20:44:23 | host | `aws-focus`: filter 5.6 s (1,435 nodes / 4,599 edges), select 1.6 s (2,964 pairs) | 7 s | |
| 20:46:29 | 21:28:05 | laptop | `laptop-augB-free`: select 1.4 s; PubMed 2,494.9 s for 2,957 pairs, no NCBI key | 41.6 min | 14,200 PMIDs |
| 20:50 | | user | Decision: A capped at `--limit 500`, B uncapped (PLAN.md section 5) | | |
| 21:28:08 | 21:36:55 | laptop | `laptop-augA`: select 8.7 s, PubMed 74.6 s (500 pairs, key set), dry run 2.9 s, full run 440.8 s | 8.8 min | $0.46; 2 edges proposed |
| 21:28:10 | 22:28:23 | laptop | `laptop-augB-paid`: dry run 2.4 s; full run 3,612.6 s (label 2.6 -> 58 min, repair, ground, review 1.5 min, apply) | 60.2 min | $4.82; 82 edges proposed |
| 22:30:04 | 22:30:47 | laptop -> host | `augmentation.tar.gz` to S3; `aws-final`: final cleaning ok (14.8 s, 1.6 s); `kgb-mcp check` **failed** on both graphs | 22 s | exit 1 x2 |
| 22:32:33 | 22:33:36 | laptop -> host | Commit e3a562b (`agents/mcp_interface/check.py`); code reshipped; `aws-final2`: both checks pass | 7 s | ready for dump |
| 22:34:10 | 22:39:25 | host -> laptop | `final-sync.tar.gz` back; `dump-B/kgb.dump` and `artifacts.tar.gz` built on the laptop with Docker (ORION amd64 under emulation), presigned URLs written | 5 min | Neo4j load pending |

Totals: 19:57 -> 22:39 is **2 h 42 min**. Host compute: **4 min 27 s** across 14 stages
(`stages.json`). Laptop compute for augmentation: **1 h 51 min**, of which PubMed without a
key was 41.6 min and labelling 2,129 pairs was 55 min. Agent time: 46 agent-minutes of
metadata (20.6 min wall) plus about 10 min for the focus file. The rest (about 35 min) was
packaging, SSM round trips, the two code fixes and the two reruns. Money: run B $4.82 (label
$2.81, quote repair $0.34, review $1.67; 82 edges proposed, 560 PMIDs added), run A $0.46
(label $0.30, repair $0.02, review $0.14; 2 edges proposed) from the "cost $" lines of
`augment_full_*.log` and `wt-e2e/data/augmentation-aws/*/work/apply_report.json`; AWS
month-to-date effectively $0 on credits, host standing cost about $2.70/day (`PLAN.md`).

## 2. Errors, failures and retries

| # | When | What happened | Cause | How it was got past | Source |
|---|---|---|---|---|---|
| E1-E3 | 20:06-20:18 | Metadata agents: ClinVar `check.py --plan-only` gave 2 false errors on null `prefix_codes` and dated papu VCF URLs 404; Monarch's mlcroissant validator failed on every entity group; eRAM's `vocab.py names` on 8,879 names raised HTTPError (chunk 07 kept failing at 500/100/10 names), the same validator failure, a `check.py` validator-cache bug that re-raised an empty error, and prefix patterns that silently matched nothing | `check.py` line ~251 lacks `is not None`; RoboCroissant `@context` has 15 keys and `apply_plan.py` adds two; bulk request too large; cache stores empty text; `fullmatch` only when `drop_unmatched` | Explained entries; undated record sets; `work/fix_context.py` (3 min); 18 chunks then single names (about 10 min); explained entry with message `""` plus a deleted cache file | `metadata/*/timeline.md`, `agent_report.md` |
| E4 | 20:19 | Transformation launched before the eRAM bundle existed; eRAM transformed apart, never merged | Runner does not wait for bundles | Rerun as `local-3src` at 20:35 | `runs/local-2src/`, `runs/local-eram/` |
| E5 | 20:19-20:35 | ClinVar wrote 0 of 14,269 edge rows (D1) | `orion_runtime.py` strips `#` from `#GeneID` | Commit 713e460 (5 lines, plus a 2,154-line `uv.lock` rewrite); rerun | `git show --stat 713e460` |
| E6 | 20:35 | eRAM re-applied as `eram-v2` "after the item_pattern fix" | Only `recordSet[2]/field[6]` changed (the disease-name pattern gained a trailing `.*$`); the symptom pattern `^(C\d{7})` + pipe, with no `.*$`, is unchanged | Not fixed: symptom layout still 0 of 1,663 rows (D2) | diff `metadata/eram` vs `eram-v2`; `wt-e2e/data/transformation/graphs/Kgbcia_eram/.../transform_report.json` |
| E7 | 20:46-21:28 | Run B PubMed took 41.6 min (1.5 req/s) | Shell without `NCBI_API_KEY` (the 21:05 waiter had it) | Accepted; run A with the key: 500 pairs in 75 s | `augment_pubmed_B.log` line 1, `launch_paid.log` |
| E8 | 21:55 | Label B: 1 of 2,129 calls "unreadable reply: answered twice: A2"; 1 abstract left out; 2 pairs written unchecked | Model output format | Stage exit 0 with a warning "rerun to retry"; not rerun | `augment_full_B.log` lines 37, 82-83, 116 |
| E9 | 22:30 | `kgb-mcp check` failed both graphs: "missing kgbcia_source_records [infores:kgbcia-augmentor]: 82" (cut), 2 (whole); "not ready for Neo4j dump preparation" | PR 22's proposed edges carry `publications` + `original_edge_ids` and by design no `kgbcia_source_records` (`pubmed-evidence/DESIGN.md` line 598); PR 64's check requires it | Commit e3a562b accepts and counts augmentor edges; `aws-final2` passed | `host/final-sync/runs/aws-final*/logs/mcp_check_*.log` |
| E10 | all runs | Runner reported 79, 52, 7 and 1 "error-looking lines" on successful stages and 0 on the two that exited 1; no `errors.log` for `aws-final` | `ERROR_RE` matches `failed` in `label: 106/2129 calls (0 failed, ...)`; a non-zero exit writes nothing | Logs read by hand | `e2e_run.py` line 34, `runs/*/errors.log`, `aws-final.out` |
| E11 | packaging | A macOS `tar` put AppleDouble `._*` files into one tarball for the host | `COPYFILE_DISABLE` not set | Re-made (the five tarballs kept under `e2e/` are clean) | lead's note |
| E12 | 21:28 | Dry run estimated B's label at $3.44 (actual $2.81) and A's at $0.43 ($0.30); review ($1.67, 35% of B's total) and repair ($0.34) are not estimated | Dry run covers labelling only | Costs stayed small because A was capped | `augment_label_dryrun_*.log`, `augment_full_*.log` |

Nothing crashed: all 27 recorded stages exited 0 except the two `mcp_check` runs of
`aws-final`. ORION, NodeNorm and the aarch64 build behaved; the host's numbers equal the
laptop's `local-3src` run exactly (42,858 / 224,554 after cleaning).

## 3. Defects

Confirmed from the local list, with the AWS evidence, then new ones. Severity as in the
local report.

| # | Defect | Status on the AWS run | Owner | Sev |
|---|---|---|---|---|
| D1 | ClinVar `#GeneID` header stripped, 0 edges | **Fixed in the worktree only** (713e460): 12,923 edges, 10,705 after cleaning (`host/reports/cleaning_report.json`); 63,385 `NCBIGene` ids still unresolved | transformation | high |
| D2 | eRAM symptom layout 0 of 1,663 rows (`fullmatch` vs `match`) | **Not fixed**: `eram-v2` changed a different pattern (E6); the host used `eram-v2` (same md5 as `metadata/bundles/eram`) and the symptom mappings still wrote 0 | metadata skill + plan | medium |
| D3 | eRAM derived fields on edges; 1 GB output | **Not fixed**: output 1,309.9 MB, peak RSS 2,091 MB on the host (`aws-shared/timeline.jsonl`); the eRAM gene mapping wrote 115,639 records from 2,097 rows | metadata `apply_plan.py` | medium |
| D4 | eRAM text-mined file included; merge drops provenance | **Now drives cost**: 113,706 `text_co_occurrence` edges make gene-disease 127,774 of 131,195 selected pairs (`host/reports/scope_report_whole.json`); in the cardiomyopathy cut 2,409 of 3,652 eRAM edges are text co-occurrence (count over `final/cardiomyopathy/edges.jsonl`); the focus agent warned "about 79% eRAM" (`focus/cardiomyopathy.md`) | metadata decision; see D13 | medium |
| D5 | Monarch coverage (39,862 edges) | Unchanged (`describe/description.md` Sources table) | RoboCroissant | medium |
| D6 | 34K spurious "invalid syntax" diagnostics | Unchanged (same runtime) | transformation | low |
| D7 | Cleaning drops/flags | 148,007 -> 42,858 nodes (unresolved 63,220, orphan 41,929, mostly ClinVar nodes without edges); 224,646 -> 224,554 edges; flags type_violation 29,243, unresolved_node 20,112, self_loop 201 (`host/reports/cleaning_report.json`). As designed; see D14 | none | n/a |
| D8 | Zero-row layouts not called out | Unchanged: describe says "3 of 3 sources built", `attention: []` while the symptom layout wrote nothing | transformation report, describe | low |
| D9 | `select` skipped table shows `BiologicalEntity`/`NamedThing` | Unchanged (`scope_report_whole.json` skipped rows) | augmentation | low |
| D10 | Runner RSS cumulative; no outcome assertion | Confirmed: all five `aws-shared` stages report 2,091.0 MB; `disk_free` is machine-wide (fell 3.2 GB during PubMed B, whose output is 4.8 MB); the host runner ran on system Python 3.9.25 (`summary.json`) | runner | low |
| **D11** | **Edge contract mismatch between PR 22 and PR 64.** | Augmentor edges have no `kgbcia_source_records` by design (DESIGN.md 598, 764: "They carry none for now"); `kgb-mcp check` called that an error and both final graphs "not ready" (E9). Fixed by e3a562b, worktree only | mcp_interface + augmentation | high |
| **D12** | **Runner error heuristic wrong both ways** | (E10): `failed` in progress lines gives 79 false positives in one stage; a stage with exit 1 gets "0 error-looking lines" and no `errors.log` entry; `stop_on_fail: false` carried on with only the exit code to show | runner | medium |
| **D13** | **`select` keys the literature skip rule on predicate only.** | `DEFAULT_RULES` skips `biolink:occurs_together_in_literature_with` (`scope.py` line 124) and ignores `knowledge_level`/`agent_type`, so eRAM's `text_co_occurrence` edges (predicate `gene_associated_with_condition`) enter the `query` tier; an uncapped run A would have been hours of PubMed and roughly $120-$215 (B's $1.63 and A's $0.92 per 1,000 pairs, extrapolated) | augmentation | high |
| **D14** | **eRAM `has_phenotype` objects resolve to Disease** | 20,179 type violations (`range: biolink:Disease`) and 201 self-loops, all eRAM (`cleaning_report.json`); description.md shows "Disease has phenotype Disease 20,950". The plan types the UMLS CUIs as PhenotypicFeature; NodeNorm says Disease. The focus agent noticed it too | metadata plan (eRAM); the flag is correct | medium |
| **D15** | **PubMed stage silent about the missing NCBI key** | (E7): it prints NCBI's weekend advice but not "no API key"; 2,957 pairs took 41.6 min vs 75 s for 500 with the key. Because keys live only in the laptop Keychain (`PLAN.md` section 1), every keyed step forced a laptop/host hop with 400 MB tarballs through S3 both ways | augmentation; secrets policy | medium |
| **D16** | **Label failure leaves edges unchecked with exit 0** | (E8): the "rerun `kgb-augment label`" advice is only in the log; `apply_report.json` records `edges_not_fully_labeled: 2` and nothing stops the pipeline. Dry run omits review and repair (E12) | augmentation | low |
| **D17** | **Metadata stage friction, all three sources** | raw files are one-element JSON lists; all three hit the missing Croissant 1.0 `@context` keys; `check.py` has three bugs (null-prefix filter, validator cache, `fullmatch`/`match`); `vocab.py names` cannot batch; `sample.py --dump` cannot read PDF; SKILL.md overflows one read. The agents attribute about half of their 46 minutes to tooling (`metadata/*/agent_report.md`) | metadata skill, RoboCroissant | medium |
| **D18** | **No pipeline driver.** | Nine hand-written `stages_*.json`, five shell scripts, `aws ssm send-command` (24 KB output cap), `nohup` + polling, and tarballs through S3 for code, bundles, cuts, augmentation output and final graphs. `bootstrap_host.sh` does `rm -rf KGB-CIA` and re-extracts, so each code fix meant a new tarball and re-bootstrap (E5, E9). A `stages_tailtest.json` host run was never synced back; the two fix commits exist only in `$S/wt-e2e` | runner, deployment | medium |
| **D19** | **Focus skill assumes a hierarchy the graph lacks** | no `subclass_of` among the 8 predicates, the `--around` footer still promises subclass listing, no `--estimate` (the agent wrote `tools/estimate_cut.py`), and the `taxon` advice would drop most Mondo subtypes (`focus/cardiomyopathy.md`) | filter skill | low |
| **D20** | **Whole-graph copies** | `kgb-filter` with no focus writes 384.9 MB and `final_cleaning_whole` another 398.9 MB of an unchanged graph (0 drops): three near-identical 400 MB copies per run (`timeline.jsonl` output_mb) | filter, cleaning | low |

## 4. Streamlining suggestions, ranked by payoff

1. **One driver, one run directory.** `kgb-pipeline run <manifest>` that waits for every listed
   bundle, runs transformation -> cleaning -> describe -> filter -> select -> pubmed -> dry-run
   gate -> label/review -> final cleaning -> mcp check, and stops when a source has 0 edges, a
   mapping reads rows and writes none, or a stage exits non-zero. It replaces the nine
   `stages_*.json`, five scripts and the S3 shuttling; it would have prevented E4, caught E5/E6
   before ORION ran, and made E9 a one-command rerun. Structured outcome lines replace the
   `failed` regex (D12); per-stage RSS via `psutil` replaces `RUSAGE_CHILDREN` (D10).
2. **A shared edge contract with a test** (D11): the proposed-edge model in `shared/schema.py`,
   consumed by `kgb-mcp check`, run in CI on the augmentation toy graph. PR 22 and PR 64 met
   for the first time at 22:30 on the host.
3. **`select` honours `knowledge_level` and `agent_type`** (D13): skip or tier
   `text_co_occurrence` by default and say so in the scope report; make the dry run cover
   label, repair and review (E12) and print the gate decision against a budget. This decides
   whether a whole-graph user pays $0.46 or $200.
4. **`kgb-transform --check`** reading the first rows of every mapped file with the runtime's
   own reader (D1, D2): one reader, one `#` rule, one `fullmatch` for check and compiler.
5. **Keys where the data is** (D15): Parameter Store under `/kgb-cia/` on the host, or a
   Session Manager export, so PubMed and the paid steps run beside the graph instead of
   round-tripping through S3; warn when `NCBI_API_KEY` is unset and the job exceeds 500 pairs.
6. **Metadata skill defaults** (D17, D3): full Croissant 1.0 `@context`, entity-column
   `derived` fields under the entity, one-element lists unwrapped, the three `check.py` bugs
   fixed, `vocab.py names` batched and resumable, and the one-call layout quick-start all three
   agents asked for.
7. **Host code sync as a command, not a tarball** (D18): push the integration branch and
   `git fetch` on the host; keep the venv across bootstraps; sync the run directory back at the
   end of every run.
8. **Reports call out losses** (D8, D14): a `lost` list in `transformation_report.json`;
   0-edge layouts and range violations under describe-graph's attention.
9. **Filter and final cleaning write markers, not copies, for the whole graph** (D20); add
   `kgb-filter --estimate` and a "no hierarchy here" hint in the focus skill (D19).
10. **Retry label failures in the pipeline** (D16): one automatic `label` rerun when
    `edges_not_fully_labeled > 0`, then fail.

## 5. What a new user would hit first

1. **No command runs the pipeline.** They must read `PLAN.md`, write `stages_*.json` by hand
   and learn `UV_PROJECT_ENVIRONMENT` + `uv run --no-sync` (every `argv` in `timeline.jsonl`
   carries it) before anything runs.
2. **Metadata is 12-21 minutes of agent time per source**, and all three trip on the same
   croissant defects (one-element list, missing `@context`, `.tsv.gz` encoding).
3. **Silent data loss.** "0 generation errors" and "3 of 3 sources built" while a layout wrote
   nothing (D2, D8); only ORION's `transform_report.json` says so.
4. **Keys and cost.** Without `NCBI_API_KEY` the free step is 5x slower; without the Azure
   key the paid steps fail; an uncapped whole graph is 131K pairs (about 30 h of PubMed without
   a key, 5 h with it, $120-$215 of model calls at this run's rates), and nothing stops them
   until they read the dry-run line themselves.
5. **`kgb-mcp check` fails any augmented graph** on the code in the team repo today (D11).
6. **The runner's own report misleads**: 79 "error-looking lines" on a successful stage, 0 on a
   failed one (D12).

## Neo4j load

Filled in by the lead after the observer's pass. `install_e2e_on_host.sh` ran on the host at
22:37:45 UTC (`helm upgrade --install kgb-e2e` in namespace `kgb-e2e`, `values-e2e.yaml`, dump
fetched by the chart's init container from a 2-hour presigned S3 URL). The pod
`kgb-e2e-kgb-mcp-neo4j-0` was Running within a minute; the script's own `rollout status` and pod
lookup used the wrong names (`kgb-e2e-neo4j`), so it reported an error after the install had
succeeded; the verification was rerun by hand and the script corrected. First attempt to pass the
URL failed because the env file was unquoted (`&` in the URL split the command).

Counts read with `cypher-shell` at 22:40 UTC: 1,435 nodes, 4,681 relationships, 82 with
`primary_knowledge_source = infores:kgbcia-augmentor`; predicates match the final cut file
exactly (gene_associated_with_condition 3,318, has_phenotype 818, model_of 263,
has_mode_of_inheritance 147, causes 133, two singletons). `CREATE (x:Probe)` was rejected
(read-only database). Hong's `kgb-mcp` release and namespace were untouched; host memory
3.3 GB used of 15.7 GB with both Neo4j releases running. The release is ClusterIP only and
costs nothing beyond the host; it stays until the lead decides to `helm uninstall kgb-e2e`.
