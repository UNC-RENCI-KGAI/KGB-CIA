"""Pipeline stage: the evidence check, between augmentation and the final cleaning pass.

Runs the evidence-check skill (`.agents/skills/cleaning/evidence-check/`) unattended, for every
question it finds:

- the questions the skill ships in its `questions/` folder (the defaults), and
- any question file in `data/cleaning/evidence/*.json`, where an agent following the skill
  writes its own. One with the same `name` as a default replaces it.

For each question it gathers the PubMed abstracts the selected edges cite (`gather_evidence.py`,
on augmentation's output), has Jev answer the question for each (`judge_evidence.py`, through
`uv run --script`, which installs the TypeSafe SDK), and writes the drop and flag lists
(`build_drop_list.py`) to `data/cleaning/drop_lists/` and `data/cleaning/flag_lists/`, where the
final cleaning pass applies them.

The check is optional, as the skill says: without `TYPESAFE_API_KEY`, Jev is asked nothing. The
judge step writes the requests it would send, and this stage says so and carries on, leaving
that question's lists as they were. A list left from an earlier run still applies: it names
statements by subject, predicate, object, qualifiers and source, and a statement the literature
contradicted is wrong in any graph that has it.

Because the check is optional, it doesn't stop the pipeline either. Without a key the judge runs
in this environment (it imports the TypeSafe SDK only to send), so no network or `uv` install is
needed just to write requests. A question whose step fails (NCBI unreachable, TypeSafe down, `uv`
missing) is reported and its lists are left as they were, the other questions still run, and the
stage returns 0 so the final cleaning pass and the MCP interface run on what there is. `--strict`
returns nonzero instead, for running a question by hand.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

DESCRIPTION = (
    "Check edges against the PubMed abstracts they cite with Jev (evidence-check skill), and "
    "write drop and flag lists for the final cleaning pass; skipped without TYPESAFE_API_KEY."
)

SKILL = Path(__file__).resolve().parents[2] / ".agents/skills/cleaning/evidence-check"
SCRIPTS = SKILL / "scripts"
DEFAULT_QUESTIONS = SKILL / "questions"
USER_QUESTIONS = Path("data/cleaning/evidence")
DEFAULT_GRAPH = Path("data/augmentation")
KEY_VARIABLE = "TYPESAFE_API_KEY"


def find_questions(*folders: Path) -> list[Path]:
    """Question files by `name`, later folders replacing earlier ones, sorted by name."""
    found: dict[str, Path] = {}
    for folder in folders:
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else ():
            name = json.loads(path.read_text()).get("name") or path.stem
            found[name] = path
    return [found[name] for name in sorted(found)]


def run_step(script: str, args: list[str]) -> int:
    """Run one of the skill's scripts. With a key, the judge runs through `uv run --script`,
    which installs the TypeSafe SDK its header names; without one it only writes requests, which
    needs no SDK, so it runs in this environment like the others. A command that can't start
    (`uv` not installed) is a failed step, not a crash."""
    path = str(SCRIPTS / f"{script}.py")
    if script == "judge_evidence" and os.environ.get(KEY_VARIABLE):
        command = ["uv", "run", "--script", path, *args]
    else:
        command = [sys.executable, path, *args]
    sys.stdout.flush()  # keep this stage's lines in order with the script's
    try:
        return subprocess.run(command).returncode
    except OSError as e:
        print(f"  could not run {script}: {e}")
        return 127


def check(question: Path, graph: Path | None) -> int:
    """Gather, judge and build for one question. Nonzero only when a step fails."""
    name = json.loads(question.read_text())["name"]
    work = USER_QUESTIONS / name
    print(f"Evidence check {name} ({question})")
    # gather_evidence writes nothing when no selected edge cites PubMed; don't judge an old file
    (work / "evidence.jsonl").unlink(missing_ok=True)
    rc = run_step("gather_evidence", [str(question), *([str(graph)] if graph else [])])
    if rc:
        return rc
    if not (work / "evidence.jsonl").exists():
        print(f"  {name}: nothing to check")
        return 0
    rc = run_step("judge_evidence", [str(question)])
    if rc:
        return rc
    if not (work / "answers.jsonl").exists():
        print(
            f"  {name}: Jev wasn't asked (no {KEY_VARIABLE}); the requests are in "
            f"{work / 'requests.jsonl'}, and {name}'s drop and flag lists are unchanged"
        )
        return 0
    if not os.environ.get(KEY_VARIABLE):
        print(f"  {name}: no {KEY_VARIABLE}; building from the answers already cached")
    return run_step("build_drop_list", [str(question)])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument(
        "--graph",
        type=Path,
        default=None,
        help="graph to check (default: the latest stage's output, data/augmentation first); "
        "the pipeline passes data/augmentation, the graph the final cleaning pass reads",
    )
    parser.add_argument(
        "--question",
        type=Path,
        action="append",
        help="check only this question file (repeatable); use it to run one question by hand. "
        f"Default: the skill's questions/ and {USER_QUESTIONS}/*.json",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="return nonzero when a question's step fails (default: report it, leave that "
        "question's lists as they were, and carry on, so the pipeline isn't stopped)",
    )
    args = parser.parse_args(argv)

    questions = args.question or find_questions(DEFAULT_QUESTIONS, USER_QUESTIONS)
    if not questions:
        print("Evidence check: no question files; nothing to check")
        return 0
    if args.graph and not (args.graph / "edges.jsonl").exists():
        print(f"Evidence check: no edges.jsonl in {args.graph}; run augmentation first")
        return 1
    if not os.environ.get(KEY_VARIABLE):
        print(
            f"Evidence check: {KEY_VARIABLE} isn't set, so Jev won't be asked. Each question's "
            "requests are written for later, and the pipeline carries on."
        )
    failed: list[tuple[Path, int]] = []
    for question in questions:
        rc = check(question, args.graph)
        if rc:
            print(f"  {question} failed with exit code {rc}; its lists are unchanged")
            failed.append((question, rc))
    if not failed:
        return 0
    print(
        f"Evidence check: {len(failed)} of {len(questions)} question(s) failed: "
        + ", ".join(str(q) for q, _ in failed)
    )
    if args.strict:
        return failed[0][1]
    print("  carrying on: the final cleaning pass applies the lists there are")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
