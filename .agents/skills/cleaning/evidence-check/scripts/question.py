"""The question file shared by the evidence-check scripts: loading it, and what an answer means.

A question file (format in reference/question-format.md) says which edges to check, what to ask
about each piece of evidence, and what each answer option means for the edge: it `supports` the
edge, `contradicts` it, or is `silent` (the text doesn't bear on it). Standard library only.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROLES = ("supports", "contradicts", "silent")
DEFAULT_MODEL = "jev-1.13.0"
DEFAULT_THRESHOLD = 0.7
CLEANING_DIR = Path("data/cleaning")


class QuestionError(ValueError):
    pass


def load(path: Path) -> dict:
    """Read and check a question file, filling in defaults."""
    q = json.loads(Path(path).read_text())
    problems = []
    if not re.fullmatch(r"[a-z0-9_]+", q.get("name", "")):
        problems.append("name: lowercase letters, digits and _ only (it names the drop list)")
    if not q.get("instructions"):
        problems.append("instructions: say what to judge, in terms of `text`, `subject`, `object`")
    options = q.get("options") or []
    if len(options) < 2:
        problems.append("options: at least two")
    names = [o.get("name") for o in options]
    if len(set(names)) != len(names) or not all(names):
        problems.append("options: every option needs a distinct name")
    for o in options:
        if o.get("role") not in ROLES:
            problems.append(f"option {o.get('name')}: role must be one of {', '.join(ROLES)}")
        if not o.get("what"):
            problems.append(f"option {o.get('name')}: describe it in `what`")
    roles = {o.get("role") for o in options}
    if "supports" not in roles:
        problems.append("options: at least one must have role supports")
    if not roles & {"contradicts", "silent"}:
        problems.append("options: at least one must contradict or be silent")
    if not q.get("select"):
        problems.append("select: say which edges to check (predicate, primary_knowledge_source)")
    if problems:
        raise QuestionError(f"{path}:\n  " + "\n  ".join(problems))
    q.setdefault("model", DEFAULT_MODEL)
    q.setdefault("threshold", DEFAULT_THRESHOLD)
    return q


def work_dir(q: dict) -> Path:
    """Where one question's evidence, requests and answers are kept."""
    return CLEANING_DIR / "evidence" / q["name"]


def drop_list_path(q: dict) -> Path:
    """Where its drop list goes: the folder kgb-clean reads."""
    return CLEANING_DIR / "drop_lists" / f"{q['name']}.jsonl"


def selects(q: dict, edge: dict) -> bool:
    """Whether an edge is one this question checks. Each select key lists allowed values."""
    for key, allowed in q["select"].items():
        allowed = allowed if isinstance(allowed, list) else [allowed]
        if edge.get(key) not in allowed:
            return False
    return True


def criteria(q: dict) -> dict:
    """The options in Jev's Choice format, in the file's order (Jev leans to earlier options)."""
    return {
        o["name"]: {"what": o["what"], **({"examples": o["examples"]} if o.get("examples") else {})}
        for o in q["options"]
    }


def role_probabilities(q: dict, probabilities: dict) -> dict[str, float]:
    """Sum an answer's option probabilities by role."""
    totals = dict.fromkeys(ROLES, 0.0)
    for o in q["options"]:
        totals[o["role"]] += probabilities.get(o["name"], 0.0)
    return {role: round(p, 6) for role, p in totals.items()}


def text_based(edge: dict) -> bool:
    """Whether the edge was itself read out of text, so text that doesn't support it is evidence
    against it. A curated edge only loses when its evidence contradicts it."""
    return (
        edge.get("agent_type") == "text_mining_agent"
        or edge.get("knowledge_level") == "text_co_occurrence"
    )


def edge_verdict(q: dict, based_on_text: bool, documents: list[dict[str, float]]) -> str:
    """keep, drop or unconfirmed, from the role probabilities of each of an edge's documents.

    Text-mined edge: dropped when no document supports it. Curated edge: dropped when a document
    contradicts it and none supports it; when all are silent it is kept, as unconfirmed.
    """
    t = q["threshold"]
    supported = any(d["supports"] >= t for d in documents)
    contradicted = any(d["contradicts"] >= t for d in documents)
    if supported:
        return "keep"
    if based_on_text:
        return "drop" if all(d["supports"] < 1 - t for d in documents) else "unconfirmed"
    return "drop" if contradicted else "unconfirmed"
