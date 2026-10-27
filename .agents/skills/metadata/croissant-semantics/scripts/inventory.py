#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""List the layouts (distinct sets of columns) in a croissant, or describe one layout.

inventory.py CROISSANT                 # all layouts, largest first
inventory.py CROISSANT --layout L1a2b  # one layout: columns, descriptions, files
inventory.py CROISSANT --json out.json # machine-readable summary
"""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import (
    as_list,
    column_names,
    content_size,
    distribution_index,
    layouts,
    load_croissant,
    record_set_files,
    text,
)


def summarize(croissant: dict) -> list[dict]:
    dists = distribution_index(croissant)
    out = []
    for lid, sets in layouts(croissant).items():
        files = [dists[i] for rs in sets for i in record_set_files(rs) if i in dists]
        sized = [(content_size(d) or 0, d) for d in files]
        smallest = min(sized, key=lambda x: x[0])[1] if sized else {}
        typed = sum(
            1
            for rs in sets
            for f in rs.get("field") or []
            if any(":" in t and not t.startswith("sc:") for t in as_list(f.get("dataType")))
        )
        out.append(
            {
                "layout": lid,
                "record_sets": len(sets),
                "columns": column_names(sets[0]),
                "example_record_set": text(sets[0].get("name")) or sets[0].get("@id"),
                "example_file": smallest.get("contentUrl") or smallest.get("name"),
                "fields_total": sum(len(rs.get("field") or []) for rs in sets),
                "fields_with_semantic_type": typed,
            }
        )
    return out


ALL = False


def describe(croissant: dict, lid: str) -> None:
    groups = layouts(croissant)
    if lid not in groups:
        sys.exit(f"No layout {lid}. Run without --layout to list them.")
    sets = groups[lid]
    dists = distribution_index(croissant)
    print(f"Layout {lid}: {len(sets)} record set(s)\n")
    # the pipeline infers atomic types per file, so one column can be typed differently in different
    # record sets
    types_seen: dict[str, Counter] = {}
    for rs in sets:
        for f in rs.get("field") or []:
            types_seen.setdefault(text(f.get("name")), Counter())[
                ", ".join(as_list(f.get("dataType")))
            ] += 1
    print("Columns (descriptions from the first record set):")
    for f in sets[0].get("field") or []:
        kind = (
            "jsonPath" if "jsonPath" in ((f.get("source") or {}).get("extract") or {}) else "column"
        )
        seen = types_seen[text(f.get("name"))]
        types = (
            ", ".join(as_list(f.get("dataType")))
            if len(seen) == 1
            else "MIXED: " + "; ".join(f"{t} in {n}" for t, n in seen.most_common())
        )
        print(f"  - {text(f.get('name'))}  [{types}; {kind}]")
        if text(f.get("description")):
            print(f"      {text(f.get('description'))[:200]}")
    print(f"\nRecord sets ({'all' if ALL else 'up to 15'} of {len(sets)}; --all for every one):")
    for rs in sets if ALL else sets[:15]:
        print(f"  - {text(rs.get('name'))}  ({rs.get('@id')})")
        if text(rs.get("description")):
            print(f"      {text(rs.get('description'))[:200]}")
    print("\nFiles (up to 10):")
    seen = 0
    for rs in sets:
        for i in record_set_files(rs):
            d = dists.get(i, {})
            print(
                f"  - {d.get('contentUrl') or d.get('name') or i}  {d.get('contentSize', '')} "
                f"{', '.join(map(str, as_list(d.get('encodingFormat'))))}"
                + (f"  (inside {d['containedIn']})" if d.get("containedIn") else "")
            )
            seen += 1
            if seen >= 10 and not ALL:
                return


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("croissant")
    ap.add_argument("--layout")
    ap.add_argument("--json", help="write the summary to this file")
    ap.add_argument(
        "--all", action="store_true", help="with --layout: list every record set and file"
    )
    ap.add_argument("--top", type=int, default=60, help="layouts to print (default 60)")
    ap.add_argument(
        "--common-columns",
        action="store_true",
        help="list column names by how many record sets have them (for knowledge bases with many "
        "layouts that share core columns; plan those with match.has_columns)",
    )
    args = ap.parse_args()
    global ALL
    ALL = args.all
    croissant = load_croissant(args.croissant)
    if args.layout:
        if args.json:
            sets = layouts(croissant).get(args.layout) or sys.exit(f"No layout {args.layout}")
            Path(args.json).write_text(
                json.dumps(
                    {
                        "layout": args.layout,
                        "columns": [
                            {
                                "name": text(f.get("name")),
                                "dataType": as_list(f.get("dataType")),
                                "description": text(f.get("description")),
                            }
                            for f in sets[0].get("field") or []
                        ],
                        "record_sets": [
                            {"id": rs.get("@id"), "name": text(rs.get("name"))} for rs in sets
                        ],
                    },
                    indent=1,
                )
            )
            print(f"Wrote {args.json}")
            return
        describe(croissant, args.layout)
        return
    if args.common_columns:
        from collections import Counter

        sets = croissant.get("recordSet") or []
        counts = Counter(c for rs in sets for c in set(column_names(rs)))
        print(f"{len(sets)} record sets; columns in at least 2% of them:")
        for c, n in counts.most_common():
            if n < max(2, 0.02 * len(sets)):
                break
            print(f"  {n:6}  {100 * n / len(sets):5.1f}%  {c}")
        return
    summary = summarize(croissant)
    if args.json:
        groups = layouts(croissant)
        Path(args.json).write_text(
            json.dumps(
                [
                    {**s, "record_set_names": [text(rs.get("name")) for rs in groups[s["layout"]]]}
                    for s in summary
                ],
                indent=1,
            )
        )
    total_rs = sum(s["record_sets"] for s in summary)
    total_fields = sum(s["fields_total"] for s in summary)
    print(
        f"{text(croissant.get('name'))}: {total_rs} record sets, {total_fields} fields, "
        f"{len(summary)} layouts\n"
    )
    running = 0
    for s in summary[: args.top]:
        running += s["fields_total"]
        cols = ", ".join(s["columns"][:8]) + (
            f", … (+{len(s['columns']) - 8})" if len(s["columns"]) > 8 else ""
        )
        print(
            f"{s['layout']}  {s['record_sets']:5} rs  {len(s['columns']):3} cols  "
            f"cum {100 * running / max(total_fields, 1):5.1f}% of fields  e.g. "
            f"{s['example_record_set']}"
        )
        print(f"           {cols}")
    if len(summary) > args.top:
        print(f"\n… {len(summary) - args.top} more layouts (use --top)")
    # documentation shipped with the data (README.pdf inside a zip, a data dictionary): the best
    # source of meanings
    docs = [
        d
        for d in croissant.get("distribution") or []
        if re.search(
            r"readme|licen[cs]e|dictionary|codebook|\.pdf$|\.md$|\.docx?$",
            str(d.get("contentUrl") or d.get("name") or ""),
            re.I,
        )
    ]
    # READMEs, dictionaries and codebooks before other PDFs (training material, papers)
    docs.sort(
        key=lambda d: (
            not re.search(
                r"readme|dictionary|codebook|fields|columns",
                str(d.get("contentUrl") or d.get("name") or ""),
                re.I,
            )
        )
    )
    if docs:
        print(
            f"\nDocumentation files ({len(docs)}; read these first, and cite them as "
            "data_dictionary):"
        )
        for d in docs[:20]:
            inside = [x["@id"] for x in as_list(d.get("containedIn")) if isinstance(x, dict)]
            print(
                f"  - {d.get('contentUrl') or d.get('name')}"
                + (f"  (inside {inside[0]})" if inside else "")
            )
        if len(docs) > 20:
            print(f"  … {len(docs) - 20} more")
    # files no record set reads: what the croissant lists but doesn't describe (often the large
    # main files a generator skipped); a big share is a generator gap to report in notes.md
    used = {i for rs in croissant.get("recordSet") or [] for i in record_set_files(rs)}
    files = [
        d
        for d in croissant.get("distribution") or []
        if d.get("@type") in ("cr:FileObject", "cr:FileSet") and d.get("@id") not in used
    ]
    files = [
        d for d in files if not any(d.get("@id") == x.get("@id") for x in docs)
    ]  # documentation is listed above
    containers = {
        x["@id"]
        for d in croissant.get("distribution") or []
        for x in as_list(d.get("containedIn"))
        if isinstance(x, dict) and "@id" in x
    }
    files = [d for d in files if d.get("@id") not in containers]  # archives holding used members
    if files:
        biggest = sorted(files, key=lambda d: -(content_size(d) or 0))[:8]
        print(
            f"\nFiles no record set reads: {len(files)} of "
            f"{len(croissant.get('distribution') or [])} listed. If the source's main data is "
            "among them, the croissant doesn't describe it: say so in notes.md (a generator "
            "problem). Largest:"
        )
        for d in biggest:
            size = content_size(d)
            print(
                f"  - {d.get('contentUrl') or d.get('name')}"
                + (f"  ({size / 1e6:,.0f} MB)" if size else "")
            )
    positional = [
        s["layout"]
        for s in summary
        if s["columns"] and all(re.fullmatch(r"column_\d+", c) for c in s["columns"])
    ]
    if positional:
        print(
            f"\nLayouts whose columns are named column_1, column_2, …: {', '.join(positional[:10])}"
            + (" …" if len(positional) > 10 else "")
            + ". The generator found no header (or ignored one), and layouts group files only by "
            "column count, so unrelated files can share one: plan them by record_sets or "
            "record_set_name, and check each file's first row (dialects.py detect --record-set)"
        )
    top10 = sum(s["record_sets"] for s in summary[:10])
    if len(summary) > 30 and top10 < 0.3 * total_rs:
        # only worth it when record sets really share columns (versions of one table), not when
        # every layout is a
        # different file
        from collections import Counter

        sets = croissant.get("recordSet") or []
        shared = Counter(c for rs in sets for c in set(column_names(rs))).most_common(1)
        if shared and shared[0][1] >= 0.3 * len(sets):
            print(
                "\nMany small layouts that share columns (e.g. "
                f"{shared[0][0]!r} is in {100 * shared[0][1] / len(sets):.0f}% of record sets): "
                "probably versions "
                "of the same tables. Run with --common-columns and plan the shared columns with "
                "match.has_columns."
            )


if __name__ == "__main__":
    main()
