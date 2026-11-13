#!/usr/bin/env python3
"""Look up nodes in the graph, to turn a user's request into seed rules for a focus file.

    find_nodes.py TEXT [GRAPH] [--category CLASS] [--limit N]
    find_nodes.py --around CURIE [GRAPH]

With TEXT: nodes with a word in their name that starts with TEXT (any case), the same match a
`name` seed rule makes, with how many there are by category. With --around: the edges touching
one node, counted by predicate, direction and source, with examples, to find the predicate that
links a family, pathway or disease to its members (for a `linked_to` rule).

GRAPH is a directory with nodes.jsonl and edges.jsonl (default: data/cleaning, else
data/transformation/merged). Run with `uv run python` so the most specific category can be
shown (it needs the project's environment); otherwise it falls back to the first listed
category and says so.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

try:  # needs the project environment: run with `uv run python`
    from agents.cleaning.biolink_types import most_specific
except ImportError:
    most_specific = None

DEFAULT_GRAPHS = (Path("data/cleaning"), Path("data/transformation/merged"))
_warned = False


def kind_of(categories) -> str:
    """A node's type: its most specific Biolink categories, joined with " + ".

    The merge sorts each category list alphabetically, so the first entry is not the type
    (a protein's list starts with BiologicalEntity). To keep only nodes of a class, test
    membership instead: `"biolink:Protein" in categories`, which is what --category does.
    """
    global _warned
    categories = tuple(categories or ())
    if not categories:
        return "(none)"
    if most_specific is None:
        if not _warned:
            print(
                "note: not run with `uv run python`, so categories below are the first listed, "
                "not the most specific",
                file=sys.stderr,
            )
            _warned = True
        return categories[0]
    return " + ".join(most_specific(categories)) or "(none)"


def find_graph(given: Path | None) -> Path:
    for path in [given] if given else DEFAULT_GRAPHS:
        if (path / "nodes.jsonl").exists() and (path / "edges.jsonl").exists():
            return path
    places = given or " or ".join(map(str, DEFAULT_GRAPHS))
    raise SystemExit(f"No nodes.jsonl and edges.jsonl in {places}")


def read_jsonl(path: Path):
    with open(path) as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def search(graph: Path, text: str, category: str | None, limit: int) -> None:
    pattern = re.compile(rf"\b{re.escape(text)}", re.IGNORECASE)
    hits, by_category = [], Counter()
    for node in read_jsonl(graph / "nodes.jsonl"):
        categories = node.get("category") or []
        if category and category not in categories:
            continue
        if pattern.search(node.get("name") or ""):
            by_category[kind_of(categories)] += 1
            if len(hits) < limit:
                hits.append(node)
    total = sum(by_category.values())
    print(f"{total:,} nodes match {text!r}" + (f" in {category}" if category else ""))
    for cat, n in by_category.most_common():
        print(f"  {cat}: {n:,}")
    for node in hits:
        taxon = node.get("taxon") or node.get("in_taxon") or ""
        category = kind_of(node.get("category"))
        kind = f"{category}, {taxon}" if taxon else category
        print(f"  {node['id']}  {node.get('name', '')}  ({kind})")
    if total > len(hits):
        print(f"  ... {total - len(hits):,} more; narrow with --category or a longer phrase")


def around(graph: Path, curie: str) -> None:
    names = {}
    for node in read_jsonl(graph / "nodes.jsonl"):
        if node.get("id") == curie:
            names[curie] = node.get("name", "")
            print(f"{curie}  {node.get('name', '')}  ({kind_of(node.get('category'))})")
            break
    else:
        raise SystemExit(f"{curie} is not a node in {graph}")
    counts, examples = Counter(), defaultdict(list)
    for edge in read_jsonl(graph / "edges.jsonl"):
        if curie not in (edge.get("subject"), edge.get("object")):
            continue
        incoming = edge["object"] == curie
        key = (
            "incoming" if incoming else "outgoing",
            edge["predicate"],
            edge.get("primary_knowledge_source"),
        )
        counts[key] += 1
        if len(examples[key]) < 3:
            examples[key].append(edge["subject"] if incoming else edge["object"])
    if not counts:
        print("No edges touch it.")
    for (direction, predicate, source), n in counts.most_common():
        arrow = f"<- {predicate}" if direction == "incoming" else f"-> {predicate}"
        shown = ", ".join(examples[(direction, predicate, source)])
        print(f"  {arrow}  {n:,} edges from {source}; e.g. {shown}")
    print("Incoming edges list the node's members or subclasses when the predicate is member_of,")
    print("part_of or subclass_of: use them in a linked_to rule.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("text", nargs="?", help="text to find in node names")
    parser.add_argument("graph", nargs="?", type=Path, help="graph directory")
    parser.add_argument("--category", help="only nodes of this Biolink class")
    parser.add_argument("--limit", type=int, default=25, help="nodes to list (default 25)")
    parser.add_argument("--around", metavar="CURIE", help="summarize the edges touching a node")
    args = parser.parse_args(argv)
    if args.around:
        # With --around the one positional, if any, is the graph
        around(find_graph(Path(args.text) if args.text else args.graph), args.around)
    elif args.text:
        search(find_graph(args.graph), args.text, args.category, args.limit)
    else:
        parser.error("give TEXT to search for, or --around CURIE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
