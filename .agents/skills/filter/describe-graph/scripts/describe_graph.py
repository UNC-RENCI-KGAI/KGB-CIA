#!/usr/bin/env python3
"""Describe a built knowledge graph from ORION's own metadata, and how a user's data joins it.

    describe_graph.py [TRANSFORMATION] [--user SOURCE ...] [--output DIR]

TRANSFORMATION is kgb-transform's output directory (default data/transformation), or its
graphs/ directory of per-source ORION workspaces. For each source this reads the workspace's
`*.meta.json` (fetch, parse and normalization status and counts) and the build's QC file
`<source>_<build>.json` (node types, predicates, subject-predicate-object type counts, edges with
publications), so it never scans the graph itself.

Totals come from the most finished stage it finds: the cleaning report in the sibling cleaning/
directory if that report was made from this run's merged/ graph, else merged/merge-metadata.json,
else the sum over sources.

`--user` names the user's own source(s), by source id or plain name ("human protein atlas").
With it, the script also reads each built source's node ids (resolved ones only) to count the
nodes sources share: how much of the user's data is already in the graph, and which sources
cover the rest.

Writes description.json (everything), description.md (for an agent to read and retell) and
overview.html (a self-contained page with charts) to --output. Standard library only.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

TOP_CATEGORIES = 12
MAX_RECOMMENDED = 5
MIN_GAIN = 0.01  # stop recommending when a source adds under 1% of the user's nodes


# ---------------------------------------------------------------------------------------------
# Reading ORION metadata


def _stable_normalization(meta: dict) -> tuple[str, str, dict] | None:
    """(parsing id, normalization id, normalization record) of the newest stable build."""
    found = []
    if meta.get("fetch_status") != "stable":
        return None
    for parsing_id, parsing in (meta.get("parsings") or {}).items():
        if parsing.get("parsing_status") != "stable":
            continue
        for norm_id, norm in (parsing.get("normalizations") or {}).items():
            if norm.get("normalization_status") == "stable":
                found.append((norm.get("normalization_time", ""), parsing_id, norm_id, norm))
    if not found:
        return None
    _, parsing_id, norm_id, norm = max(found, key=lambda item: item[0])
    return parsing_id, norm_id, norm


def _failure(meta: dict) -> str:
    """Where a source that didn't build stopped, in a few words."""
    if meta.get("fetch_status") != "stable":
        error = meta.get("fetch_error") or meta.get("fetch_status") or "not fetched"
        return f"download failed: {error}"
    for parsing in (meta.get("parsings") or {}).values():
        if parsing.get("parsing_status") != "stable":
            return f"parsing {parsing.get('parsing_status')}: {parsing.get('parsing_error', '')}"
        for norm in (parsing.get("normalizations") or {}).values():
            if norm.get("normalization_status") != "stable":
                return f"normalization {norm.get('normalization_status')}"
    return "no stable build"


def read_source(workspace: Path) -> dict:
    """One source's status and counts from its ORION workspace."""
    metas = sorted(workspace.glob("*/data/*/*.meta.json"), key=lambda p: p.stat().st_mtime)
    record = {"source_id": workspace.name, "built": False}
    if not metas:
        record["problem"] = "no ORION metadata"
        return record
    meta_path = metas[-1]
    meta = json.loads(meta_path.read_text())
    record["version"] = meta.get("source_version")
    stable = _stable_normalization(meta)
    if stable is None:
        record["problem"] = _failure(meta)
        return record
    parsing_id, norm_id, norm = stable
    info = norm.get("normalization_info", {})
    record.update(
        built=True,
        nodes=info.get("final_normalized_nodes", 0),
        edges=info.get("final_normalized_edges", 0),
        unresolved_nodes=info.get("node_normalization_failures", 0),
        normalized_dir=str(meta_path.parent / f"parsed_{parsing_id}" / f"normalized_{norm_id}"),
    )
    qc_path = None
    for build_id, build in (meta.get("builds") or {}).items():
        if build.get("parsing_version") == parsing_id and build.get("normalization_version") == (
            norm_id
        ):
            record["name"] = build.get("name")
            record["infores"] = build.get("identifier")
            record["description"] = build.get("description")
            candidate = (
                meta_path.parent / f"{meta.get('source_id', workspace.name)}_{build_id}.json"
            )
            if candidate.exists():
                qc_path = candidate
    if qc_path:
        qc = json.loads(qc_path.read_text())
        pubs = qc.get("edges_with_publications") or {}
        record.update(
            node_types={_first(t["type"]): t["count"] for t in qc.get("node_types", [])},
            predicates=qc.get("predicate_totals", {}),
            spo=[
                (_first(t["subject_type"]), t["predicate"], _first(t["object_type"]), t["count"])
                for t in qc.get("s-p-o_types", [])
            ],
            edges_with_publications=sum(pubs.values()) if isinstance(pubs, dict) else int(pubs),
            primary_knowledge_sources=qc.get("primary_knowledge_sources", []),
        )
    return record


def _first(types: list) -> str:
    return types[0] if types else "biolink:NamedThing"


def read_totals(merged: Path | None, cleaned: Path | None, sources: list[dict]) -> dict:
    """Graph-wide node and edge counts, from the most finished stage available."""
    if cleaned and (cleaned / "cleaning_report.json").exists():
        report = json.loads((cleaned / "cleaning_report.json").read_text())
        return {
            "basis": "cleaned graph",
            "nodes": report["nodes"]["out"],
            "edges": report["edges"]["out"],
            "cleaning": {
                "nodes_in": report["nodes"]["in"],
                "edges_in": report["edges"]["in"],
                "dropped_edges": report.get("dropped_edges", {}),
                "dropped_nodes": report.get("dropped_nodes", {}),
            },
        }
    if merged and (merged / "merge-metadata.json").exists():
        meta = json.loads((merged / "merge-metadata.json").read_text())
        return {
            "basis": "merged graph",
            "nodes": meta.get("final_node_count", 0),
            "edges": meta.get("final_edge_count", 0),
            "duplicate_edges_merged": meta.get("edges_diff"),
        }
    built = [s for s in sources if s["built"]]
    return {
        "basis": "sum over sources, before merging (shared nodes and edges counted twice)",
        "nodes": sum(s["nodes"] for s in built),
        "edges": sum(s["edges"] for s in built),
    }


# ---------------------------------------------------------------------------------------------
# Overlap between sources


def read_node_ids(normalized_dir: str) -> set[str]:
    """Resolved node ids of one source (unresolved nodes can't join anything)."""
    ids = set()
    with open(Path(normalized_dir) / "normalized_nodes.jsonl") as f:
        for line in f:
            node = json.loads(line)
            if node.get("equivalent_identifiers"):
                ids.add(node["id"])
    return ids


def overlap(sources: list[dict], user: list[str]) -> dict:
    """Nodes shared between built sources, and how the user's sources join the rest.

    Each node id gets a bitmask of the sources that have it, so memory is one int per node.
    """
    built, missing = [], []
    for s in sources:
        if s["built"]:
            nodes_file = Path(s["normalized_dir"]) / "normalized_nodes.jsonl"
            (built if nodes_file.exists() else missing).append(s)
    for s in missing:
        print(f"warning: {s['source_id']} has no normalized_nodes.jsonl; left out", file=sys.stderr)
    names = [s["source_id"] for s in built]
    masks: dict[str, int] = defaultdict(int)
    for bit, source in enumerate(built):
        for node_id in read_node_ids(source["normalized_dir"]):
            masks[node_id] |= 1 << bit
    by_mask = Counter(masks.values())
    del masks

    shared = Counter()
    for mask, count in by_mask.items():
        members = [i for i in range(len(names)) if mask >> i & 1]
        for a in range(len(members)):
            for b in range(a + 1, len(members)):
                shared[(names[members[a]], names[members[b]])] += count
    result = {
        "left_out": [s["source_id"] for s in missing],
        "shared_nodes": [
            {"a": a, "b": b, "nodes": n} for (a, b), n in shared.most_common() if n > 0
        ],
    }
    if not user:
        return result

    user_bits = 0
    for source_id in user:
        if source_id not in names:
            raise SystemExit(f"--user {source_id}: its normalized_nodes.jsonl is missing")
        user_bits |= 1 << names.index(source_id)
    other_bits = ((1 << len(names)) - 1) & ~user_bits
    user_masks = {mask: n for mask, n in by_mask.items() if mask & user_bits}
    total = sum(user_masks.values())
    connected = sum(n for mask, n in user_masks.items() if mask & other_bits)
    per_source = Counter()
    for mask, n in user_masks.items():
        for i, name in enumerate(names):
            if other_bits >> i & 1 and mask >> i & 1:
                per_source[name] += n

    # Greedy cover: each step picks the source that connects the most user nodes not yet covered
    recommended, covered = [], 0
    remaining = dict(user_masks)
    while len(recommended) < MAX_RECOMMENDED and total:
        gains = Counter()
        for mask, n in remaining.items():
            for i, name in enumerate(names):
                if other_bits >> i & 1 and mask >> i & 1 and name not in recommended:
                    gains[name] += n
        if not gains:
            break
        name, gain = gains.most_common(1)[0]
        if gain / total < MIN_GAIN:
            break
        bit = 1 << names.index(name)
        remaining = {mask: n for mask, n in remaining.items() if not mask & bit}
        covered += gain
        recommended.append({"source_id": name, "adds": gain, "cumulative": covered})

    result["user"] = {
        "sources": user,
        "nodes": total,
        "connected_nodes": connected,
        "connected_share": connected / total if total else 0.0,
        "shared_with": dict(per_source.most_common()),
        "recommended_sources": recommended,
    }
    return result


# ---------------------------------------------------------------------------------------------
# Finding things, so the caller doesn't have to


def find_layout(path: Path) -> tuple[Path, Path | None, Path | None]:
    """(graphs, merged, cleaned) from kgb-transform's output directory or its graphs/ directory.

    merged is OUTPUT/merged when it has merge metadata. cleaned is the sibling cleaning/ directory
    when its report says it cleaned that merged graph, so a stale cleaning run is never used.
    """
    output = path if (path / "graphs").is_dir() else path.parent
    graphs = output / "graphs" if (output / "graphs").is_dir() else path
    merged = output / "merged"
    merged = merged if (merged / "merge-metadata.json").exists() else None
    cleaned = output.parent / "cleaning"
    report = cleaned / "cleaning_report.json"
    if merged and report.exists():
        cleaned_input = json.loads(report.read_text()).get("settings", {}).get("input", "")
        if not cleaned_input or Path(cleaned_input).resolve() != merged.resolve():
            cleaned = None
    else:
        cleaned = None
    return graphs, merged, cleaned


def _plain(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_").removeprefix("kgbcia_")


def resolve_user(names: list[str], sources: list[dict]) -> list[str]:
    """Source ids for the names a user gave: exact id, then plain name, then a unique substring.

    Raises SystemExit listing the built sources when a name matches none or several.
    """
    built = [s["source_id"] for s in sources if s["built"]]
    resolved = []
    for name in names:
        if name in built:
            resolved.append(name)
            continue
        want = _plain(name)
        exact = [b for b in built if _plain(b) == want]
        partial = [b for b in built if want in _plain(b)]
        matches = exact or partial
        if len(matches) == 1:
            resolved.append(matches[0])
            continue
        listing = ", ".join(source_label(b) for b in (matches or built))
        problem = "matches several sources" if matches else "matches no built source"
        raise SystemExit(f"--user {name!r} {problem}. Choose from: {listing}")
    return resolved


# ---------------------------------------------------------------------------------------------
# Putting it together


def describe(
    graphs: Path,
    merged: Path | None = None,
    cleaned: Path | None = None,
    user: list[str] | None = None,
) -> dict:
    sources = [read_source(w) for w in sorted(p for p in graphs.iterdir() if p.is_dir())]
    user = resolve_user(user or [], sources)
    built = [s for s in sources if s["built"]]
    node_types, predicates, spo = Counter(), Counter(), Counter()
    for s in built:
        node_types.update(s.get("node_types", {}))
        predicates.update(s.get("predicates", {}))
        for subject, predicate, obj, count in s.get("spo", []):
            spo[(subject, predicate, obj)] += count
    description = {
        "date": date.today().isoformat(),
        "graphs": str(graphs),
        "totals": read_totals(merged, cleaned, sources),
        "sources_built": len(built),
        "sources_total": len(sources),
        "edges_with_publications": sum(s.get("edges_with_publications", 0) for s in built),
        "edges_built": sum(s["edges"] for s in built),
        "node_types": dict(node_types.most_common()),
        "predicates": dict(predicates.most_common()),
        "spo": [
            {"subject": s, "predicate": p, "object": o, "edges": n}
            for (s, p, o), n in spo.most_common()
        ],
        "sources": [{k: v for k, v in s.items() if k not in ("spo",)} for s in sources],
    }
    if user:
        description["overlap"] = overlap(sources, user)
    return description


def label(curie: str) -> str:
    """'biolink:SequenceVariant' -> 'Sequence variant'; 'biolink:has_part' -> 'has part'."""
    name = curie.removeprefix("biolink:")
    if "_" in name or name.islower():
        return name.replace("_", " ")
    words = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", name)
    return " ".join([words[0]] + [w.lower() for w in words[1:]]) if words else name


def source_label(source_id: str) -> str:
    return source_id.removeprefix("Kgbcia_").replace("_", " ")


def short(n: float) -> str:
    """12,841,209 -> '12.8M'."""
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= size:
            value = n / size
            return f"{value:.1f}{unit}" if value < 100 else f"{value:.0f}{unit}"
    return f"{n:,.0f}"


def markdown(d: dict) -> str:
    t = d["totals"]
    built = [s for s in d["sources"] if s["built"]]
    lines = [
        f"# Graph description ({d['date']})",
        "",
        f"- **Nodes:** {t['nodes']:,}  **Edges:** {t['edges']:,}  ({t['basis']})",
        f"- **Sources built:** {d['sources_built']} of {d['sources_total']}",
        f"- **Kinds of things (node categories):** {len(d['node_types'])}",
        f"- **Kinds of relationships (predicates):** {len(d['predicates'])}",
        f"- **Edges citing publications:** {d['edges_with_publications']:,} of "
        f"{d['edges_built']:,} built",
    ]
    if "cleaning" in t:
        c = t["cleaning"]
        lines.append(
            f"- **Cleaning:** {c['edges_in']:,} edges in, {t['edges']:,} kept; dropped "
            + ", ".join(f"{k} {v:,}" for k, v in c["dropped_edges"].items())
        )
    lines += ["", "## Node categories", "", "| Category | Nodes |", "|---|--:|"]
    lines += [f"| {label(k)} | {v:,} |" for k, v in d["node_types"].items()]
    lines += ["", "## Most common connections", "", "| Subject | Predicate | Object | Edges |"]
    lines += ["|---|---|---|--:|"]
    lines += [
        f"| {label(r['subject'])} | {label(r['predicate'])} | {label(r['object'])} "
        f"| {r['edges']:,} |"
        for r in d["spo"][:25]
    ]
    lines += ["", "## Sources", "", "| Source | infores | Nodes | Edges | Unresolved nodes |"]
    lines += ["|---|---|--:|--:|--:|"]
    for s in sorted(built, key=lambda s: -s["edges"]):
        lines.append(
            f"| {source_label(s['source_id'])} | {s.get('infores', '')} | {s['nodes']:,} | "
            f"{s['edges']:,} | {s['unresolved_nodes']:,} |"
        )
    not_built = [s for s in d["sources"] if not s["built"]]
    if not_built:
        lines += ["", "## Sources not built", ""]
        lines += [f"- {source_label(s['source_id'])}: {s['problem']}" for s in not_built]
    user = d.get("overlap", {}).get("user")
    if user:
        lines += [
            "",
            "## How the user's data connects",
            "",
            f"{user['connected_nodes']:,} of the user's {user['nodes']:,} resolved nodes "
            f"({user['connected_share']:.0%}) also appear in other sources.",
            "",
            "Recommended sources to focus on, in order (each adds the nodes it newly connects):",
            "",
        ]
        for i, r in enumerate(user["recommended_sources"], 1):
            share = r["cumulative"] / user["nodes"]
            lines.append(
                f"{i}. {source_label(r['source_id'])}: +{r['adds']:,} nodes "
                f"({share:.0%} connected so far)"
            )
        if not user["recommended_sources"]:
            lines.append("None: no other source shares the user's nodes.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------------
# The HTML overview

CSS = """
.viz-root{color-scheme:light;--surface-1:#fcfcfb;--surface-2:#f3f2ef;--text-primary:#0b0b0b;
--text-secondary:#52514e;--text-muted:#6f6e69;--rule:#dcdbd6;--series-1:#2a78d6;--series-2:#eb6834;
--link:#8a8984}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{
color-scheme:dark;--surface-1:#1a1a19;--surface-2:#242422;--text-primary:#f0efec;
--text-secondary:#c3c2b7;--text-muted:#9c9b93;--rule:#3a3a37;--series-1:#3987e5;--series-2:#d95926;
--link:#7d7c75}}
:root[data-theme="dark"] .viz-root{color-scheme:dark;--surface-1:#1a1a19;--surface-2:#242422;
--text-primary:#f0efec;--text-secondary:#c3c2b7;--text-muted:#9c9b93;--rule:#3a3a37;
--series-1:#3987e5;--series-2:#d95926;--link:#7d7c75}
body{margin:0}
.viz-root{background:var(--surface-1);color:var(--text-primary);min-height:100vh;
font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;padding:32px}
.page{max-width:1040px;margin:0 auto}
h1{font-size:28px;margin:0 0 4px}h2{font-size:19px;margin:40px 0 4px}
.sub{color:var(--text-secondary);margin:0 0 24px}.note{color:var(--text-secondary);margin:0 0 16px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.kpi{background:var(--surface-2);border-radius:8px;padding:14px 16px}
.kpi .v{font-size:30px;font-weight:600;font-variant-numeric:tabular-nums}
.kpi .l{color:var(--text-secondary);font-size:13px}
.card{background:var(--surface-2);border-radius:8px;padding:16px}
svg text{fill:var(--text-primary);font:12px system-ui,sans-serif}
svg .muted{fill:var(--text-secondary)}
.link{stroke:var(--link);fill:none;opacity:.45}.link:hover,.link:focus{stroke:var(--series-1);opacity:1}
.dot{fill:var(--series-1);stroke:var(--surface-2);stroke-width:2}.dot.user{fill:var(--series-2)}
.bar{fill:var(--series-1)}.bar.user{fill:var(--series-2)}
.hit{fill:transparent}.row:hover .bar,.row:focus .bar{opacity:.8}
.legend{display:flex;gap:16px;color:var(--text-secondary);font-size:13px;margin:8px 0}
.legend i{display:inline-block;width:12px;height:12px;border-radius:2px;margin-right:6px;
vertical-align:-1px}
#tip{position:fixed;pointer-events:none;background:var(--surface-1);color:var(--text-primary);
border:1px solid var(--rule);border-radius:6px;padding:6px 10px;font-size:13px;display:none;
max-width:320px;box-shadow:0 2px 8px rgba(0,0,0,.15)}
#tip b{display:block;font-size:15px}
details{margin-top:24px}summary{cursor:pointer;color:var(--text-secondary)}
table{border-collapse:collapse;margin:8px 0 16px;font-size:13px}
td,th{padding:3px 10px;border-bottom:1px solid var(--rule);text-align:left}
td.n{text-align:right;font-variant-numeric:tabular-nums}
ul.problems{color:var(--text-secondary)}
"""

JS = """
const tip=document.getElementById('tip');
function show(e){const t=e.currentTarget;tip.replaceChildren();const b=document.createElement('b');
b.textContent=t.dataset.v;tip.append(b,document.createTextNode(t.dataset.l));
tip.style.display='block';const r=e.clientX!==undefined&&e.clientX?
{x:e.clientX,y:e.clientY}:(()=>{const q=t.getBoundingClientRect();return{x:q.right,y:q.top}})();
tip.style.left=Math.min(r.x+14,innerWidth-340)+'px';tip.style.top=(r.y+14)+'px'}
function hide(){tip.style.display='none'}
document.querySelectorAll('[data-v]').forEach(el=>{el.setAttribute('tabindex','0');
el.addEventListener('pointermove',show);el.addEventListener('focus',show);
el.addEventListener('pointerleave',hide);el.addEventListener('blur',hide)});
"""


def _esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def _tip(value: str, text: str) -> str:
    return f'data-v="{_esc(value)}" data-l="{_esc(text)}"'


def circle_network(
    nodes: list[tuple[str, str, float, bool]],
    links: list[tuple[str, str, float, str]],
    size: int = 620,
) -> str:
    """Nodes on a circle, sized by sqrt(weight); links curve through the middle, width by log.

    nodes: (key, label, weight, highlighted). links: (key_a, key_b, weight, tooltip text).
    """
    if not nodes:
        return ""
    pad = 130  # room for labels left and right of the circle
    cx, cy = size / 2 + pad, size / 2
    radius = size / 2 - 90
    biggest = max(w for _, _, w, _ in nodes) or 1
    pos, parts = {}, []
    for i, (key, *_) in enumerate(nodes):
        angle = 2 * math.pi * i / len(nodes) - math.pi / 2
        pos[key] = (cx + radius * math.cos(angle), cy + radius * math.sin(angle), angle)
    top = max((w for *_, w, _ in links), default=1) or 1
    for a, b, weight, text in sorted(links, key=lambda link: link[2]):
        width = 1 + 7 * math.log1p(weight) / math.log1p(top)
        xa, ya, angle = pos[a]
        if a == b:
            ox, oy = math.cos(angle) * 46, math.sin(angle) * 46
            px, py = -math.sin(angle) * 22, math.cos(angle) * 22
            d = (
                f"M{xa:.1f},{ya:.1f} C{xa + ox + px:.1f},{ya + oy + py:.1f} "
                f"{xa + ox - px:.1f},{ya + oy - py:.1f} {xa:.1f},{ya:.1f}"
            )
        else:
            xb, yb, _ = pos[b]
            mx, my = (xa + xb) / 2, (ya + yb) / 2
            qx, qy = mx + (cx - mx) * 0.55, my + (cy - my) * 0.55
            d = f"M{xa:.1f},{ya:.1f} Q{qx:.1f},{qy:.1f} {xb:.1f},{yb:.1f}"
        parts.append(
            f'<path class="link" d="{d}" stroke-width="{width:.1f}" '
            f"{_tip(short(weight), text)}><title>{_esc(text)}</title></path>"
        )
    for key, name, weight, highlighted in nodes:
        x, y, angle = pos[key]
        r = 6 + 22 * math.sqrt(weight / biggest)
        cls = "dot user" if highlighted else "dot"
        parts.append(
            f'<circle class="{cls}" cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" '
            f"{_tip(short(weight), name)}><title>{_esc(name)}</title></circle>"
        )
        lx, ly = x + math.cos(angle) * (r + 8), y + math.sin(angle) * (r + 8)
        anchor = "start" if math.cos(angle) > 0.2 else "end" if math.cos(angle) < -0.2 else "middle"
        dy = 14 if math.sin(angle) > 0.5 else -4 if math.sin(angle) < -0.5 else 4
        parts.append(
            f'<text x="{lx:.1f}" y="{ly + dy:.1f}" text-anchor="{anchor}">{_esc(name)}</text>'
        )
    return (
        f'<svg viewBox="0 0 {size + 2 * pad} {size}" width="100%" '
        f'style="max-width:{size + 2 * pad}px" role="img">' + "".join(parts) + "</svg>"
    )


def bar_chart(rows: list[tuple[str, float, bool, str]], unit: str) -> str:
    """Horizontal bars, one row each: (label, value, highlighted, tooltip text)."""
    if not rows:
        return ""
    row_h, label_w, width = 26, 240, 760
    plot_w = width - label_w - 70
    top = max(v for _, v, _, _ in rows) or 1
    parts = []
    for i, (name, value, highlighted, text) in enumerate(rows):
        y = i * row_h
        w = max(plot_w * value / top, 2)
        cls = "bar user" if highlighted else "bar"
        parts.append(
            f'<g class="row" {_tip(f"{value:,.0f} {unit}", text)}>'
            f'<rect class="hit" x="0" y="{y}" width="{width}" height="{row_h}"/>'
            f'<text x="{label_w - 8}" y="{y + 17}" text-anchor="end">{_esc(name)}</text>'
            f'<rect class="{cls}" x="{label_w}" y="{y + 5}" width="{w:.1f}" height="{row_h - 10}" '
            f'rx="4"/><text class="muted" x="{label_w + w + 6:.1f}" y="{y + 17}">'
            f"{_esc(short(value))}</text></g>"
        )
    height = len(rows) * row_h
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img">' + "".join(parts) + "</svg>"
    )


def overview_html(d: dict) -> str:
    t = d["totals"]
    user_info = d.get("overlap", {}).get("user")
    user_ids = set(user_info["sources"]) if user_info else set()
    built = [s for s in d["sources"] if s["built"]]

    kpis = [
        (short(t["nodes"]), "things (nodes)"),
        (short(t["edges"]), "connections (edges)"),
        (f"{d['sources_built']} of {d['sources_total']}", "sources built"),
        (str(len(d["node_types"])), "kinds of things"),
        (str(len(d["predicates"])), "kinds of connection"),
        (
            f"{d['edges_with_publications'] / d['edges_built']:.0%}" if d["edges_built"] else "0%",
            "connections citing a publication",
        ),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><div class="v">{_esc(v)}</div><div class="l">{_esc(lab)}</div></div>'
        for v, lab in kpis
    )

    # Category map: the top categories by node count; the rest fold into "Other"
    top = list(d["node_types"])[:TOP_CATEGORIES]
    fold = {c: (c if c in top else "Other") for c in d["node_types"]}
    weights = Counter()
    for c, n in d["node_types"].items():
        weights[fold[c]] += n
    pair_edges, pair_preds = Counter(), defaultdict(Counter)
    for r in d["spo"]:
        a = fold.get(r["subject"], "Other")
        b = fold.get(r["object"], "Other")
        key = tuple(sorted((a, b)))
        pair_edges[key] += r["edges"]
        pair_preds[key][label(r["predicate"])] += r["edges"]
    cat_nodes = [(c, label(c) if c != "Other" else "Other", weights[c], False) for c in weights]
    cat_links = []
    for (a, b), n in pair_edges.items():
        if a not in weights or b not in weights:
            continue
        preds = "; ".join(f"{p} {short(v)}" for p, v in pair_preds[(a, b)].most_common(3))
        name_a = label(a) if a != "Other" else "Other"
        name_b = label(b) if b != "Other" else "Other"
        cat_links.append((a, b, n, f"{name_a} – {name_b}: {preds}"))
    category_svg = circle_network(cat_nodes, cat_links)

    source_rows = [
        (
            source_label(s["source_id"]),
            s["edges"],
            s["source_id"] in user_ids,
            f"{source_label(s['source_id'])}: {s['nodes']:,} nodes, "
            f"{s['unresolved_nodes']:,} unresolved",
        )
        for s in sorted(built, key=lambda s: -s["edges"])
    ]
    legend = (
        '<div class="legend"><span><i style="background:var(--series-1)"></i>Pre-built sources'
        '</span><span><i style="background:var(--series-2)"></i>Your data</span></div>'
        if user_ids
        else ""
    )

    sections = [
        f"<h1>Your knowledge graph at a glance</h1>"
        f'<p class="sub">{_esc(d["date"])} · counts from the {_esc(t["basis"])}</p>'
        f'<div class="kpis">{kpi_html}</div>',
        "<h2>What's in it</h2>"
        '<p class="note">Each circle is a kind of thing, sized by how many there are. A line '
        "joins two kinds when the graph connects them; thicker lines mean more connections. "
        "Hover for the most common relationships.</p>"
        f'<div class="card">{category_svg}</div>',
        f"<h2>Where it came from</h2>"
        f'<p class="note">Connections contributed by each source, before merging.</p>{legend}'
        f'<div class="card">{bar_chart(source_rows, "edges")}</div>',
    ]

    if user_info:
        share = user_info["connected_share"]
        recommended = {r["source_id"] for r in user_info["recommended_sources"]}
        shared_rows = [
            (
                source_label(name) + (" ★" if name in recommended else ""),
                n,
                False,
                f"{n / user_info['nodes']:.0%} of your nodes are also in {source_label(name)}",
            )
            for name, n in list(user_info["shared_with"].items())[:15]
        ]
        source_nodes = [
            (
                s["source_id"],
                source_label(s["source_id"]),
                s["nodes"],
                s["source_id"] in user_ids,
            )
            for s in built
        ]
        source_links = [
            (
                r["a"],
                r["b"],
                r["nodes"],
                f"{source_label(r['a'])} and {source_label(r['b'])} share {r['nodes']:,} nodes",
            )
            for r in d["overlap"]["shared_nodes"]
        ]
        steps = "".join(
            f"<li>{_esc(source_label(r['source_id']))}: connects {r['adds']:,} more of your "
            f"nodes ({r['cumulative'] / user_info['nodes']:.0%} so far)</li>"
            for r in user_info["recommended_sources"]
        )
        sections.append(
            "<h2>How your data connects</h2>"
            f'<div class="kpis"><div class="kpi"><div class="v">{share:.0%}</div>'
            f'<div class="l">of your {user_info["nodes"]:,} items are already in the graph</div>'
            "</div></div>"
            f'<p class="note" style="margin-top:16px">Sources that share the most of your items. '
            f"★ marks the ones we recommend focusing on:</p><ol>{steps}</ol>"
            f'<div class="card">{bar_chart(shared_rows, "shared nodes")}</div>'
            '<p class="note" style="margin-top:16px">How the sources overlap: a line joins two '
            "sources that share items, thicker for more.</p>"
            f'{legend}<div class="card">{circle_network(source_nodes, source_links)}</div>'
        )

    not_built = [s for s in d["sources"] if not s["built"]]
    if not_built:
        items = "".join(
            f"<li>{_esc(source_label(s['source_id']))}: {_esc(s['problem'])}</li>"
            for s in not_built
        )
        sections.append(f"<h2>Not built yet</h2><ul class='problems'>{items}</ul>")

    def table(head: list[str], rows: list[list]) -> str:
        h = "".join(f"<th>{_esc(c)}</th>" for c in head)
        body = "".join(
            "<tr>"
            + "".join(
                f'<td class="n">{v:,}</td>' if isinstance(v, int) else f"<td>{_esc(v)}</td>"
                for v in row
            )
            + "</tr>"
            for row in rows
        )
        return f"<table><tr>{h}</tr>{body}</table>"

    sections.append(
        "<details><summary>All the numbers as tables</summary>"
        + table(["Category", "Nodes"], [[label(k), v] for k, v in d["node_types"].items()])
        + table(
            ["Subject", "Predicate", "Object", "Edges"],
            [
                [label(r["subject"]), label(r["predicate"]), label(r["object"]), r["edges"]]
                for r in d["spo"][:50]
            ],
        )
        + table(
            ["Source", "infores", "Nodes", "Edges", "Unresolved nodes"],
            [
                [
                    source_label(s["source_id"]),
                    s.get("infores", ""),
                    s["nodes"],
                    s["edges"],
                    s["unresolved_nodes"],
                ]
                for s in sorted(built, key=lambda s: -s["edges"])
            ],
        )
        + "</details>"
    )
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Knowledge graph overview</title><style>{CSS}</style></head>"
        f'<body><div class="viz-root"><div class="page">{"".join(sections)}</div>'
        f'<div id="tip"></div></div><script>{JS}</script></body></html>\n'
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "transformation",
        type=Path,
        nargs="?",
        default=Path("data/transformation"),
        help="kgb-transform's output directory, or its graphs/ (default: data/transformation)",
    )
    parser.add_argument(
        "--user", nargs="+", default=[], help="the user's own source(s), by id or plain name"
    )
    parser.add_argument("--output", type=Path, default=Path("data/describe"), help="output dir")
    args = parser.parse_args(argv)
    if not args.transformation.is_dir():
        print(f"{args.transformation} is not a directory; run kgb-transform first", file=sys.stderr)
        return 1

    graphs, merged, cleaned = find_layout(args.transformation)
    d = describe(graphs, merged, cleaned, args.user)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "description.json").write_text(json.dumps(d, indent=2) + "\n")
    (args.output / "description.md").write_text(markdown(d))
    (args.output / "overview.html").write_text(overview_html(d))
    t = d["totals"]
    print(
        f"{t['nodes']:,} nodes, {t['edges']:,} edges ({t['basis']}); "
        f"{d['sources_built']} of {d['sources_total']} sources built."
    )
    print(f"Wrote description.json, description.md and overview.html to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
