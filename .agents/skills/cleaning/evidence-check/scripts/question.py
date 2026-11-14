"""The question file shared by the evidence-check scripts: loading it, and what an answer means.

A question file (format in reference/question-format.md) says which edges to check, what to ask
about each piece of evidence, and what each answer option means for the edge: it `supports` the
edge, `contradicts` it, or is `silent` (the text doesn't bear on it). A `contradicts` option
can carry a `flag`: the text shows the edge is real but a detail is wrong (a drug acting on the
target, but not as the edge's qualifier says), so the edge is kept and flagged, not dropped.
Standard library only.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROLES = ("supports", "contradicts", "silent")
DEFAULT_MODEL = "jev-1.13.0"
DEFAULT_THRESHOLD = 0.7
# a text-mined edge none of whose abstracts supports it: kept and flagged, since "not stated in
# the abstracts we read" is not "wrong"
UNSUPPORTED_FLAG = "unsupported_by_abstract"
UNSUPPORTED_REASON = "none of the abstracts it cites supports this text-mined edge"
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
        if "flag" in o:
            if o.get("role") != "contradicts":
                problems.append(f"option {o.get('name')}: only a contradicts option can flag")
            if not re.fullmatch(r"[a-z0-9_]+", str(o["flag"])):
                problems.append(f"option {o.get('name')}: flag: lowercase letters, digits and _")
            if not o.get("flag_reason"):
                problems.append(
                    f"option {o.get('name')}: flag_reason: one plain sentence a user reads on "
                    "the edge"
                )
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


def flag_list_path(q: dict) -> Path:
    """Where its flag list goes: the folder beside the drop lists, which kgb-clean also reads."""
    return CLEANING_DIR / "flag_lists" / f"{q['name']}.jsonl"


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


def role_probabilities(q: dict, probabilities: dict) -> dict:
    """Sum an answer's option probabilities by role. `contradicts` counts only the options that
    drop; a flagging option's probability goes under `flags`, by flag."""
    totals = dict.fromkeys(ROLES, 0.0)
    flags: dict[str, float] = {}
    for o in q["options"]:
        p = probabilities.get(o["name"], 0.0)
        if o.get("flag"):
            flags[o["flag"]] = flags.get(o["flag"], 0.0) + p
        else:
            totals[o["role"]] += p
    return {
        **{role: round(p, 6) for role, p in totals.items()},
        "flags": {flag: round(p, 6) for flag, p in flags.items()},
    }


def flag_reasons(q: dict) -> dict[str, str]:
    """Each flag's sentence for a user: the question's flagging options, and UNSUPPORTED_FLAG."""
    reasons = {o["flag"]: o["flag_reason"] for o in q["options"] if o.get("flag")}
    return {**reasons, UNSUPPORTED_FLAG: UNSUPPORTED_REASON}


def text_based(edge: dict) -> bool:
    """Whether the edge was itself read out of text, so text that doesn't support it is evidence
    against it. A curated edge only loses when its evidence contradicts it."""
    return (
        edge.get("agent_type") == "text_mining_agent"
        or edge.get("knowledge_level") == "text_co_occurrence"
    )


def edge_verdict(q: dict, based_on_text: bool, documents: list[dict]) -> tuple[str, str | None]:
    """(verdict, flag) from the role probabilities of each of an edge's documents. The verdict
    is keep, drop, flag (kept, with the flag) or unconfirmed (kept).

    Any document that supports the edge keeps it. One that contradicts it, counting dropping and
    flagging options together, drops it, or flags it when the flagging options carry more of
    that probability (the edge is real, a detail is wrong). Otherwise a text-mined edge none of
    whose documents supports it is flagged UNSUPPORTED_FLAG, and a curated one is unconfirmed:
    "not in the abstract" isn't "wrong".
    """
    t = q["threshold"]
    if any(d["supports"] >= t for d in documents):
        return "keep", None
    for d in sorted(
        documents, key=lambda d: -(d["contradicts"] + sum((d.get("flags") or {}).values()))
    ):
        flags = d.get("flags") or {}
        if d["contradicts"] + sum(flags.values()) < t:
            break
        top = max(flags, key=flags.get, default=None)
        if top and flags[top] > d["contradicts"]:
            return "flag", top
        return "drop", None
    if based_on_text and all(d["supports"] < 1 - t for d in documents):
        return "flag", UNSUPPORTED_FLAG
    return "unconfirmed", None
