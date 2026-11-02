#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "pyyaml", "bioregistry", "jsonpath-ng", "openpyxl", "truststore"]
# ///
"""Set each knowledge base's own infores id on finished bundles, without redoing their semantics.

    set_infores.py suggest ROOT > ids.json     # a draft id for every bundle under ROOT
    set_infores.py apply ROOT ids.json         # write the reviewed ids into the bundles

A bundle is a directory holding semantics_plan.json and croissant.semantic.json(.gz). `apply`
sets the plan's infores (and infores_proposed) and the croissant's rcv:infores the way
apply_plan.py does, then runs check.py's infores check. Nothing else in either file changes, and
neither the data nor the original croissant is read.

In ids.json, each bundle (its directory name) maps to {"infores": ID}, with "proposed": true for
an id the catalog doesn't have yet. `suggest` marks the entries a person must decide with
"review": true and lists the catalog's candidates; `apply` skips entries still marked.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import vocab  # noqa: E402
from apply_plan import apply_infores  # noqa: E402
from check import Findings, check_source_infores  # noqa: E402
from common import load_croissant, save_croissant, text  # noqa: E402

PLAN = "semantics_plan.json"


def bundles(root: Path) -> dict[str, Path]:
    return {p.parent.name: p.parent for p in sorted(root.rglob(PLAN))}


def semantic_croissant(bundle: Path) -> Path:
    for name in ("croissant.semantic.json", "croissant.semantic.json.gz"):
        if (bundle / name).exists():
            return bundle / name
    raise FileNotFoundError(f"{bundle} has no croissant.semantic.json(.gz)")


def suggest(bundle: Path) -> dict:
    """An id for the bundle's knowledge base: one already in the plan, else the catalog entry
    whose id, name or synonym is exactly the knowledge base's name, else a proposed one. Anything
    less certain is marked for review."""
    plan = json.loads((bundle / PLAN).read_text())
    if plan.get("infores"):
        return {"infores": plan["infores"], "proposed": bool(plan.get("infores_proposed"))}
    name = text(load_croissant(semantic_croissant(bundle)).get("name"))
    names = [n for n in (plan.get("kb"), bundle.name, name) if n]
    for n in names:
        for not_exact, not_released, iid, entry in vocab.infores_matches(n.replace("_", " ")):
            if not not_exact and not not_released:
                return {"infores": iid, "why": f"catalog {entry.get('name')!r} is {n!r}"}
    candidates = {
        iid: entry.get("name")
        for n in names
        for _, _, iid, entry in vocab.infores_matches(n.replace("_", " "))[:5]
    }
    if candidates:
        return {"infores": None, "review": True, "candidates": candidates}
    return {
        "infores": vocab.proposed_infores(plan.get("kb") or bundle.name),
        "proposed": True,
        "review": True,
        "why": f"no catalog entry matches {names}; check the proposed id",
    }


def apply(bundle: Path, entry: dict, catalog: dict) -> list[tuple[str, str]]:
    """Write the id into the bundle's plan and croissant; the infores check's findings."""
    plan_path, croissant_path = bundle / PLAN, semantic_croissant(bundle)
    plan = json.loads(plan_path.read_text())
    ids = {"infores": entry["infores"]}
    if entry.get("proposed"):
        ids["infores_proposed"] = True
    rest = {k: v for k, v in plan.items() if k not in ("kb", "infores", "infores_proposed")}
    plan = {**({"kb": plan["kb"]} if "kb" in plan else {}), **ids, **rest}
    croissant = load_croissant(croissant_path)
    apply_infores(croissant, plan)
    plan_path.write_text(json.dumps(plan, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    save_croissant(croissant, croissant_path)
    f = Findings()
    check_source_infores(plan, croissant, f, catalog)
    return [(level, msg) for level, _, msg in f.items]


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("suggest")
    p.add_argument("root", type=Path)
    p = sub.add_parser("apply")
    p.add_argument("root", type=Path)
    p.add_argument("ids", type=Path)
    args = ap.parse_args()
    found = bundles(args.root)
    if not found:
        sys.exit(f"No bundles ({PLAN}) under {args.root}")
    if args.cmd == "suggest":
        print(json.dumps({n: suggest(b) for n, b in found.items()}, indent=1))
        return
    ids = json.loads(args.ids.read_text())
    catalog = vocab.infores()
    errors = 0
    for name, entry in ids.items():
        if name not in found:
            print(f"{name}: no such bundle under {args.root}", file=sys.stderr)
            errors += 1
        elif entry.get("review") or not entry.get("infores"):
            print(f"{name}: skipped, still marked for review", file=sys.stderr)
        else:
            for level, msg in apply(found[name], entry, catalog):
                print(f"{name}: {level}: {msg}", file=sys.stderr)
                errors += level == "error"
    for name in sorted(found.keys() - ids.keys()):
        print(f"{name}: not in {args.ids}, left as it is", file=sys.stderr)
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
