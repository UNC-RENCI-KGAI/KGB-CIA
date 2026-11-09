"""Cut the subgraph a user asked for out of the full graph.

The agent turns the user's request into a focus file (format in the focus-subgraph skill's
reference): seed rules that pick the nodes the request is about, and how far to reach out from
them. This module applies it reproducibly, streaming over nodes.jsonl and edges.jsonl so only
node ids are held in memory.

Seed rules, any number, combined as a union:

- `{"ids": [CURIE, ...]}`: these nodes.
- `{"name": TEXT, "category": CLASS, "taxon": TAXON}`: nodes whose name contains TEXT as a word
  (any case), optionally only of a Biolink category and only of a taxon (NCBITaxon:9606).
- `{"linked_to": [CURIE, ...], "predicate": [PREDICATE, ...], "transitive": bool}`: nodes that
  are the subject of such an edge to one of those nodes, e.g. the subclasses of a disease;
  with transitive, their subclasses too.
- `{"source": INFORES}`: every node an edge from that primary knowledge source touches, e.g.
  the user's own dataset.

From the seeds the subgraph reaches out `hops` steps (default 1) along edges, not expanding
through hub nodes with more than `max_degree` edges (default 2000), which would pull in most of
the graph. Its edges are every edge between its nodes, optionally only from `sources` or with
`predicates`.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from collections.abc import Iterator
from pathlib import Path

DEFAULT_HOPS = 1
DEFAULT_MAX_DEGREE = 2000
EXAMPLES = 10
SEED_KINDS = ("ids", "name", "linked_to", "source")


class FocusError(ValueError):
    pass


def load_focus(path: Path) -> dict:
    """Read and check a focus file, filling in defaults."""
    focus = json.loads(Path(path).read_text())
    problems = []
    if not re.fullmatch(r"[a-z0-9_]+", focus.get("name", "")):
        problems.append("name: lowercase letters, digits and _ only (it names the output)")
    if not focus.get("request"):
        problems.append("request: the user's request, in their words")
    seeds = focus.get("seeds") or []
    if not seeds:
        problems.append("seeds: at least one seed rule")
    for i, seed in enumerate(seeds):
        kinds = [k for k in SEED_KINDS if k in seed]
        if len(kinds) != 1:
            problems.append(f"seeds[{i}]: exactly one of {', '.join(SEED_KINDS)}")
        elif kinds == ["linked_to"] and not seed.get("predicate"):
            problems.append(f"seeds[{i}]: linked_to needs a predicate")
    hops = focus.setdefault("hops", DEFAULT_HOPS)
    if not isinstance(hops, int) or not 0 <= hops <= 3:
        problems.append("hops: 0 to 3")
    focus.setdefault("max_degree", DEFAULT_MAX_DEGREE)
    if problems:
        raise FocusError(f"{path}:\n  " + "\n  ".join(problems))
    return focus


def _as_list(value) -> list:
    return value if isinstance(value, list) else [value]


def read_jsonl(path: Path) -> Iterator[dict]:
    with open(path) as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def _edge_allowed(focus: dict, edge: dict) -> bool:
    if focus.get("sources") and edge.get("primary_knowledge_source") not in focus["sources"]:
        return False
    if focus.get("predicates") and edge.get("predicate") not in focus["predicates"]:
        return False
    return True


def find_seeds(focus: dict, nodes_path: Path, edges_path: Path) -> tuple[set[str], list[dict]]:
    """The seed node ids, and per rule what it matched (count and example names)."""
    seeds: set[str] = set()
    matched: list[set[str]] = [set() for _ in focus["seeds"]]
    names: dict[str, str] = {}

    name_rules = []
    for i, seed in enumerate(focus["seeds"]):
        if "name" in seed:
            pattern = re.compile(rf"\b{re.escape(seed['name'])}", re.IGNORECASE)
            name_rules.append((i, pattern, _as_list(seed.get("category") or []), seed.get("taxon")))
    id_rules = [(i, set(_as_list(s["ids"]))) for i, s in enumerate(focus["seeds"]) if "ids" in s]
    for node in read_jsonl(nodes_path):
        node_id = node.get("id")
        if not node_id:
            continue
        for i, wanted in id_rules:
            if node_id in wanted:
                matched[i].add(node_id)
                names[node_id] = node.get("name") or ""
        for i, pattern, categories, taxon in name_rules:
            if categories and not set(categories) & set(node.get("category") or []):
                continue
            if taxon and taxon not in _as_list(node.get("taxon") or node.get("in_taxon") or []):
                continue
            if pattern.search(node.get("name") or ""):
                matched[i].add(node_id)
                names[node_id] = node.get("name") or ""

    link_rules = [
        (i, set(_as_list(s["linked_to"])), set(_as_list(s["predicate"])), s.get("transitive"))
        for i, s in enumerate(focus["seeds"])
        if "linked_to" in s
    ]
    source_rules = [(i, set(_as_list(s["source"]))) for i, s in enumerate(focus["seeds"])
                    if "source" in s]  # fmt: skip
    if link_rules or source_rules:
        children: dict[int, dict[str, set[str]]] = {i: defaultdict(set) for i, *_ in link_rules}
        for edge in read_jsonl(edges_path):
            for i, wanted in source_rules:
                if edge.get("primary_knowledge_source") in wanted:
                    matched[i].update((edge["subject"], edge["object"]))
            for i, _, predicates, _ in link_rules:
                if edge.get("predicate") in predicates:
                    children[i][edge["object"]].add(edge["subject"])
        for i, targets, _, transitive in link_rules:
            frontier, found = set(targets), set()
            while frontier:
                step = set().union(*(children[i].get(t, ()) for t in frontier)) - found
                found |= step
                frontier = step if transitive else set()
            matched[i] = found

    report = []
    for seed, ids in zip(focus["seeds"], matched, strict=True):
        seeds |= ids
        examples = sorted(ids)[:EXAMPLES]
        report.append(
            {
                "rule": seed,
                "nodes": len(ids),
                "examples": [f"{names.get(i, '')} [{i}]".strip() for i in examples],
            }
        )
    return seeds, report


def expand(focus: dict, seeds: set[str], edges_path: Path) -> tuple[set[str], dict]:
    """Seeds plus everything within `hops` allowed edges, not expanding through hubs."""
    keep = set(seeds)
    frontier = set(seeds)
    hubs: Counter = Counter()
    for _ in range(focus["hops"]):
        if not frontier:
            break
        degree: Counter = Counter()
        reached: dict[str, set[str]] = defaultdict(set)
        for edge in read_jsonl(edges_path):
            if not _edge_allowed(focus, edge):
                continue
            s, o = edge["subject"], edge["object"]
            if s in frontier:
                degree[s] += 1
                reached[s].add(o)
            if o in frontier:
                degree[o] += 1
                reached[o].add(s)
        step = set()
        for node, neighbors in reached.items():
            if node not in seeds and degree[node] > focus["max_degree"]:
                hubs[node] = degree[node]  # kept, but not expanded through
                continue
            step |= neighbors
        frontier = step - keep
        keep |= step
    return keep, {"hubs_not_expanded": dict(hubs.most_common(EXAMPLES))}


def link_or_copy(source: Path, target: Path) -> None:
    if target.exists() or target.is_symlink():
        target.unlink()
    try:
        os.symlink(source.resolve(), target)
    except OSError:
        target.write_bytes(source.read_bytes())


def whole_graph(input_dir: Path, output_dir: Path) -> dict:
    """No focus: the user's graph is the whole graph. Link it rather than copying 100M edges."""
    output_dir.mkdir(parents=True, exist_ok=True)
    for name in ("nodes.jsonl", "edges.jsonl"):
        link_or_copy(input_dir / name, output_dir / name)
    report = {"request": None, "input": str(input_dir.resolve()), "whole_graph": True}
    (output_dir / "focus_report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def extract(focus: dict, input_dir: Path, output_dir: Path) -> dict:
    """Write the focused subgraph and focus_report.json to output_dir, and return the report."""
    nodes_path, edges_path = input_dir / "nodes.jsonl", input_dir / "edges.jsonl"
    seeds, seed_report = find_seeds(focus, nodes_path, edges_path)
    keep, expansion = expand(focus, seeds, edges_path)

    output_dir.mkdir(parents=True, exist_ok=True)
    by_source, by_predicate = Counter(), Counter()
    used: set[str] = set()
    with open(output_dir / "edges.jsonl", "w") as out:
        for edge in read_jsonl(edges_path):
            if edge["subject"] in keep and edge["object"] in keep and _edge_allowed(focus, edge):
                out.write(json.dumps(edge) + "\n")
                used.update((edge["subject"], edge["object"]))
                by_source[edge.get("primary_knowledge_source")] += 1
                by_predicate[edge.get("predicate")] += 1
    nodes_out = 0
    with open(output_dir / "nodes.jsonl", "w") as out:
        for node in read_jsonl(nodes_path):
            # Seeds stay even without edges, so the user sees what their request matched
            if node.get("id") in used or node.get("id") in seeds:
                out.write(json.dumps(node) + "\n")
                nodes_out += 1

    report = {
        "request": focus["request"],
        "input": str(input_dir.resolve()),
        "focus": focus,
        "seeds": len(seeds),
        "seed_rules": seed_report,
        "nodes": nodes_out,
        "edges": sum(by_source.values()),
        "edges_by_source": dict(by_source.most_common()),
        "edges_by_predicate": dict(by_predicate.most_common()),
        **expansion,
    }
    (output_dir / "focus_report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def summary(report: dict) -> str:
    if report.get("whole_graph"):
        return f"No focus given: the user's graph is the whole graph at {report['input']}."
    lines = [f"Request: {report['request']}", f"Seeds: {report['seeds']:,}"]
    for rule in report["seed_rules"]:
        shown = ", ".join(rule["examples"][:3])
        lines.append(f"  {json.dumps(rule['rule'])}: {rule['nodes']:,} ({shown})")
    lines.append(f"Subgraph: {report['nodes']:,} nodes, {report['edges']:,} edges")
    for source, n in list(report["edges_by_source"].items())[:8]:
        lines.append(f"  {source}: {n:,} edges")
    if report["hubs_not_expanded"]:
        lines.append(f"Hubs not expanded: {', '.join(report['hubs_not_expanded'])}")
    return "\n".join(lines)
