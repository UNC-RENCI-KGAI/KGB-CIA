"""Apply and check each source's semantics plan, skipping sources whose inputs haven't changed.

Writing a plan is the skill's expensive, LLM step, so this stage reuses plans. Each source ends
in one of these states, recorded in OUTPUT/metadata_report.json:

- unchanged: croissant, plan, maintainer answers and skill scripts all match the last passing
  run, so nothing ran.
- passed: the plan was applied and the check passed. No agent was needed.
- check_errors: the plan was applied but the check found errors. The bundle is kept, and the
  check runs again next time.
- needs_agent: there's no plan, or the plan no longer fits this croissant, so an agent has to
  follow the croissant-semantics skill for this source.

Column names decide whether a plan fits, so a croissant whose URLs, checksums or dates changed
is applied and checked again without an agent.
"""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_SCRIPTS = REPO_ROOT / ".agents/skills/metadata/croissant-semantics/scripts"
PLAN = "semantics_plan.json"
ANSWERS = "semantics_answers.json"
STATE = "semantics_state.json"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def skill_digest() -> str:
    """One hash over the skill's scripts: a changed check has to look at every source again."""
    files = sorted(p for p in SKILL_SCRIPTS.iterdir() if p.suffix in {".py", ".json"})
    return hashlib.sha256("".join(_digest(p) for p in files).encode()).hexdigest()


def find_croissant(directory: Path) -> Path | None:
    for name in ("croissant.json", "croissant.json.gz"):
        if (directory / name).exists():
            return directory / name
    return None


def fingerprint(croissant: Path, plan: Path, skill: str) -> dict:
    answers = croissant.parent / ANSWERS
    return {
        "croissant": _digest(croissant),
        "plan": _digest(plan),
        "answers": _digest(answers) if answers.exists() else None,
        "skill": skill,
    }


def _load_plan_tools():
    """apply_plan's own matching and common's layout ids, so "fits" means what apply_plan means."""
    sys.path.insert(0, str(SKILL_SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location("apply_plan", SKILL_SCRIPTS / "apply_plan.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(SKILL_SCRIPTS))
    return module


def _read_croissant(path: Path) -> dict:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def unplanned_layouts(croissant: dict, plan: dict) -> list[str]:
    """Layouts the plan neither matches nor names in `skipped`, to flag for a person or an agent.

    Raises the skill's PlanError when a plan entry matches nothing, meaning the plan doesn't fit.
    """
    tools = _load_plan_tools()
    planned = tools.match_record_sets(croissant, plan)
    skipped = json.dumps(plan.get("skipped") or [])
    found = set()
    for record_set in croissant.get("recordSet") or []:
        layout = tools.layout_id(tools.column_names(record_set))
        if record_set["@id"] not in planned and layout not in skipped:
            found.add(layout)
    return sorted(found)


def _script(name: str, *args: str | Path) -> subprocess.CompletedProcess:
    command = ["uv", "run", "--script", str(SKILL_SCRIPTS / name), *map(str, args)]
    return subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, check=False)


def _needs_agent(name: str, reason: str) -> dict:
    return {"source": name, "status": "needs_agent", "reason": reason}


def process(source: Path, output: Path, prior_plans: Path | None, skill: str, force: bool) -> dict:
    name = source.name
    croissant = find_croissant(source)
    bundle = output / name
    plan = bundle / PLAN
    if not plan.exists() and prior_plans and (prior_plans / name / PLAN).exists():
        bundle.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(prior_plans / name / PLAN, plan)
    if not plan.exists():
        return _needs_agent(name, "no plan yet")

    suffix = ".gz" if croissant.suffix == ".gz" else ""
    semantic = bundle / f"croissant.semantic.json{suffix}"
    current = fingerprint(croissant, plan, skill)
    state_path = bundle / STATE
    if not force and semantic.exists() and state_path.exists():
        state = json.loads(state_path.read_text())
        if state.get("status") == "passed" and state.get("inputs") == current:
            unplanned = state.get("unplanned_layouts", [])
            return {"source": name, "status": "unchanged", "unplanned_layouts": unplanned}
    state_path.unlink(missing_ok=True)

    document = _read_croissant(croissant)
    try:
        unplanned = unplanned_layouts(document, json.loads(plan.read_text()))
    except Exception as error:  # the skill's PlanError, loaded under its own module name
        if type(error).__name__ != "PlanError":
            raise
        return _needs_agent(name, f"plan no longer fits: {error}")

    applied = _script("apply_plan.py", croissant, plan, semantic)
    if applied.returncode:
        return _needs_agent(name, f"plan didn't apply: {applied.stderr.strip()[-1000:]}")
    report = bundle / "check_report.md"
    checked = _script("check.py", croissant, plan, semantic, "--report", report, "--load-records")
    status = "check_errors" if checked.returncode else "passed"
    state = {"status": status, "inputs": current, "unplanned_layouts": unplanned}
    state_path.write_text(json.dumps(state, indent=2) + "\n")
    return {"source": name, "status": status, "unplanned_layouts": unplanned}


def run(input_dir: Path, output_dir: Path, prior_plans: Path | None, force: bool = False) -> int:
    sources = (
        sorted(d for d in input_dir.iterdir() if d.is_dir() and find_croissant(d))
        if input_dir.exists()
        else []
    )
    if not sources:
        print(f"Metadata agent: no croissants found under {input_dir}/<source>/")
        return 1
    output_dir.mkdir(parents=True, exist_ok=True)
    skill = skill_digest()
    results = []
    # One source at a time: the skill's scripts sample each host's servers, gently.
    for source in sources:
        result = process(source, output_dir, prior_plans, skill, force)
        print(f"  {result['source']}: {result['status']}", flush=True)
        results.append(result)

    names = {source.name for source in sources}
    orphans = sorted(d.name for d in output_dir.iterdir() if d.is_dir() and d.name not in names)
    report = {"sources": results, "not_in_input": orphans}
    path = output_dir / "metadata_report.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

    counts = {}
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    summary = ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))
    print(f"Metadata agent: {summary}. See {path}.")
    if orphans:
        print(
            f"  {len(orphans)} bundles in {output_dir} have no croissant in {input_dir}, and "
            f"transformation will still build them: {', '.join(orphans)}"
        )
    built = any(r["status"] in {"unchanged", "passed", "check_errors"} for r in results)
    return 0 if built else 1
