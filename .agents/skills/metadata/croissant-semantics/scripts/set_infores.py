#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "pyyaml", "bioregistry", "jsonpath-ng", "openpyxl", "truststore"]
# ///
"""Set each knowledge base's own infores id on finished bundles, without redoing their semantics.

    set_infores.py ROOT                       # every bundle under ROOT
    set_infores.py ROOT disease-ontology ...  # only these bundles (directory names)
    set_infores.py ROOT --dry-run             # say what would be set, change nothing
    set_infores.py ROOT --ids ids.json        # override choices: {"bundle": "infores:x", ...}

A bundle is a directory holding semantics_plan.json and croissant.semantic.json(.gz). For each,
the id is the one in --ids, else the one already in its plan, else the catalog entry whose id,
name or synonym is exactly the knowledge base's name, else a proposed id in the catalog's style
(infores:bioplex). It's written into the plan (infores, and infores_proposed for a proposed id)
and the croissant (rcv:infores, rcv:inforesProposed) as apply_plan.py would; nothing else in
either file changes, and neither the data nor the original croissant is read.

Proposed ids are used straight away: graph builders accept ids the catalog lacks (ORION warns).
Each still needs registering: their catalog entries are written to ROOT/infores_to_register.yaml,
for a pull request to the infores registry. Name only the bundles that need an id, since a
changed croissant makes graph builders rebuild that source.
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import vocab  # noqa: E402
from apply_plan import apply_infores  # noqa: E402
from check import Findings, check_source_infores  # noqa: E402
from common import load_croissant, save_croissant, text  # noqa: E402

PLAN = "semantics_plan.json"
TO_REGISTER = "infores_to_register.yaml"


def bundles(root: Path) -> dict[str, Path]:
    return {p.parent.name: p.parent for p in sorted(root.rglob(PLAN))}


def semantic_croissant(bundle: Path) -> Path:
    for name in ("croissant.semantic.json", "croissant.semantic.json.gz"):
        if (bundle / name).exists():
            return bundle / name
    raise FileNotFoundError(f"{bundle} has no croissant.semantic.json(.gz)")


def choose(bundle: Path, override: str | None = None) -> dict:
    """The bundle's id, {"infores", "proposed", "why"}: the one given in --ids, else the one
    already in its plan, else what the plan's own evidence says (vocab.suggest_infores), with
    the kb, directory and croissant name as the knowledge base's names."""
    plan = json.loads((bundle / PLAN).read_text())
    if override:
        proposed = override not in vocab.infores()
        return {"infores": override, "proposed": proposed, "why": "given in --ids"}
    if plan.get("infores"):
        proposed = bool(plan.get("infores_proposed"))
        return {"infores": plan["infores"], "proposed": proposed, "why": "already in the plan"}
    title = text(load_croissant(semantic_croissant(bundle)).get("name"))
    choice = vocab.suggest_infores(plan, [plan.get("kb"), bundle.name], [title])
    if choice["proposed"] and "similar" in choice["why"]:
        choice["why"] += "; pass --ids if one of them is this source"
    return choice


def plan_text(raw: str, plan: dict) -> str:
    """The plan's file with only its infores lines changed: written after the "kb" line in that
    line's indentation, so the rest of a hand- or tool-formatted plan keeps its layout. Falls back
    to rewriting the whole file when that doesn't give exactly the plan."""
    lines = raw.split("\n")
    kb = next((i for i, ln in enumerate(lines) if re.match(r'\s*"kb":.*,\s*$', ln)), None)
    if kb is not None:
        indent = re.match(r"\s*", lines[kb]).group()
        # only the plan's own lines, at the kb line's depth (prefixes declare "infores" too)
        own = re.compile(rf'{indent}"infores(_proposed)?":')
        added = [f'{indent}"infores": {json.dumps(plan["infores"])},']
        if plan.get("infores_proposed"):
            added.append(f'{indent}"infores_proposed": true,')
        rest = [ln for ln in lines[kb + 1 :] if not own.match(ln)]
        text_ = "\n".join(lines[: kb + 1] + added + rest)
        try:
            if json.loads(text_) == plan:
                return text_
        except json.JSONDecodeError:
            pass
    return json.dumps(plan, indent=1, ensure_ascii=False) + "\n"


def apply(bundle: Path, choice: dict, catalog: dict) -> list[tuple[str, str]]:
    """Write the id into the bundle's plan and croissant; the infores check's findings."""
    plan_path, croissant_path = bundle / PLAN, semantic_croissant(bundle)
    raw = plan_path.read_text(encoding="utf-8")
    plan = json.loads(raw)
    ids = {"infores": choice["infores"]}
    if choice["proposed"]:
        ids["infores_proposed"] = True
    rest = {k: v for k, v in plan.items() if k not in ("kb", "infores", "infores_proposed")}
    plan = {**({"kb": plan["kb"]} if "kb" in plan else {}), **ids, **rest}
    croissant = load_croissant(croissant_path)
    apply_infores(croissant, plan)
    plan_path.write_text(plan_text(raw, plan), encoding="utf-8")
    save_croissant(croissant, croissant_path)
    f = Findings()
    check_source_infores(plan, croissant, f, catalog, bundle.name)
    return [(level, msg) for level, _, msg in f.items]


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("root", type=Path, help="directory holding the bundles")
    ap.add_argument("names", nargs="*", help="only these bundles (directory names)")
    ap.add_argument("--ids", type=Path, help='JSON {"bundle": "infores:x"} overriding the choice')
    ap.add_argument("--dry-run", action="store_true", help="report the choices, change nothing")
    args = ap.parse_args()
    found = bundles(args.root)
    if not found:
        sys.exit(f"No bundles ({PLAN}) under {args.root}")
    if missing := [n for n in args.names if n not in found]:
        sys.exit(f"No such bundles under {args.root}: {', '.join(missing)}")
    overrides = json.loads(args.ids.read_text()) if args.ids else {}
    catalog = vocab.infores()
    errors = 0
    for name in args.names or sorted(found):
        choice = choose(found[name], overrides.get(name))
        mark = "proposed" if choice["proposed"] else "registered"
        print(f"{name}: {choice['infores']} ({mark}): {choice['why']}")
        if args.dry_run:
            continue
        for level, msg in apply(found[name], choice, catalog):
            errors += level == "error"
            if level == "error":
                print(f"  error: {msg}")
    if not args.dry_run:
        errors += write_to_register(args.root, found)
    sys.exit(1 if errors else 0)


def write_to_register(root: Path, found: dict[str, Path]) -> int:
    """Catalog entries for every proposed id under root, from all its bundles (not just this
    run's), so the file is complete however the bundles were set."""
    entries = []
    for bundle in found.values():
        plan = json.loads((bundle / PLAN).read_text())
        if plan.get("infores_proposed") and plan.get("infores"):
            croissant = load_croissant(semantic_croissant(bundle))
            entries.append(vocab.infores_entry(croissant, plan["infores"], *["not_provided"] * 2))
    path = root / TO_REGISTER
    if not entries:
        path.unlink(missing_ok=True)
        return 0
    path.write_text(
        f"# Proposed infores ids, used in the graph but not yet in the infores catalog.\n"
        f"# Add these entries to infores_catalog.yaml in a pull request to\n"
        f"# {vocab.INFORES_REGISTRY} (check each field, and set knowledge_level\n"
        f"# and agent_type), then raise INFORES_VERSION in vocab.py.\n" + "".join(entries),
        encoding="utf-8",
    )
    print(
        f"\n{len(entries)} proposed infores ids are in use but need registering: their catalog "
        f"entries are in {path}, for a pull request to {vocab.INFORES_REGISTRY}"
    )
    return 0


if __name__ == "__main__":
    main()
