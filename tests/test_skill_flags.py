"""Every option a skill's scripts accept is explained in that skill's own docs.

An agent only learns a flag exists, and when to use it, from the skill's Markdown (SKILL.md and
whatever it points to). A flag added to a script but not to the docs is one the agent never
uses, or guesses at. The docs should say when to use it, not just what it does.
"""

import ast
import re
from pathlib import Path

import pytest

SKILLS = Path(__file__).parents[1] / ".agents" / "skills"


def skill_dirs() -> list[Path]:
    return sorted(path.parent for path in SKILLS.glob("*/*/SKILL.md"))


def script_flags(skill: Path) -> dict[str, set[str]]:
    """Long options passed to add_argument in the skill's scripts, by script."""
    found: dict[str, set[str]] = {}
    for script in sorted(skill.rglob("*.py")):
        if {"tests", "__pycache__"} & set(script.parts):
            continue
        for node in ast.walk(ast.parse(script.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_argument":
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and str(arg.value).startswith("--"):
                        found.setdefault(str(script.relative_to(skill)), set()).add(arg.value)
    return found


def documented(skill: Path, flag: str) -> bool:
    docs = "\n".join(path.read_text() for path in skill.rglob("*.md"))
    return re.search(re.escape(flag) + r"(?![\w-])", docs) is not None


def test_there_are_skills_to_check():
    assert skill_dirs(), f"no SKILL.md under {SKILLS}"


@pytest.mark.parametrize("skill", skill_dirs(), ids=lambda path: path.name)
def test_every_script_flag_is_documented(skill):
    missing = [
        f"{script} {flag}"
        for script, flags in script_flags(skill).items()
        for flag in sorted(flags)
        if not documented(skill, flag)
    ]
    assert not missing, (
        f"{skill.name}: these options appear in no Markdown file of the skill, so an agent "
        "won't know when to use them. Say when to use each (or remove it):\n  "
        + "\n  ".join(missing)
    )
