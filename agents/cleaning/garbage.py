"""Garbage collection for a merged KGX graph: drop what can't be right, flag what looks wrong.

Reads `nodes.jsonl` and `edges.jsonl` (ORION's merged output), writes cleaned copies and
`cleaning_report.json`. Records pass through as raw JSON, so every Biolink slot and
`kgbcia_source_records` survives untouched.

An edge is dropped, checked in this order, when it:

- `malformed`: lacks a subject, predicate or object.
- `no_primary_knowledge_source`: has no provenance, so the MCP interface can't explain it.
- `dangling`: names a node that isn't in nodes.jsonl.
- `unresolved_node`: touches a node NodeNorm couldn't resolve (no equivalent identifiers).
  Pass `keep_unresolved=True` to keep these.
- `drop_list:<name>`: matches a precomputed drop list, such as an evidence check's output.

A node is dropped when it is `unresolved` (unless kept) or `orphan`: no kept edge touches it.
Edges kept but worth a look are counted as flags: `self_loop`, `knowledge_level_not_provided`
and `agent_type_not_provided`.

Drop lists key edges by subject, predicate, object and primary knowledge source, not by edge
id: the merge assigns new uuids on every run, so ids wouldn't match the next build.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

EdgeKey = tuple[str, str, str, str]

EXAMPLES_PER_REASON = 5


def edge_key(edge: dict) -> EdgeKey:
    """The identity of an edge across builds."""
    return (
        edge.get("subject", ""),
        edge.get("predicate", ""),
        edge.get("object", ""),
        edge.get("primary_knowledge_source", ""),
    )


def load_drop_list(path: Path) -> set[EdgeKey]:
    """Edge keys from a JSON-lines file, one edge per line with the four key fields.

    Other fields on a line (a reason, a score) are allowed and ignored here.
    """
    keys = set()
    with open(path) as f:
        for number, line in enumerate(f, 1):
            if not line.strip():
                continue
            record = json.loads(line)
            key = edge_key(record)
            if not all(key):
                raise ValueError(
                    f"{path}:{number}: a drop-list line needs subject, predicate, object and "
                    "primary_knowledge_source"
                )
            keys.add(key)
    return keys


def is_resolved(node: dict) -> bool:
    """Whether NodeNorm resolved the node. ORION leaves equivalent_identifiers empty when not."""
    return bool(node.get("equivalent_identifiers"))


def prefix(curie: str) -> str:
    return curie.split(":", 1)[0] if ":" in curie else "(no prefix)"


def read_jsonl(path: Path) -> Iterator[dict]:
    with open(path) as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


@dataclass
class Report:
    """What garbage collection did, for cleaning_report.json."""

    settings: dict
    nodes_in: int = 0
    nodes_out: int = 0
    edges_in: int = 0
    edges_out: int = 0
    dropped_edges: Counter = field(default_factory=Counter)
    dropped_edges_by_source: dict = field(default_factory=lambda: defaultdict(Counter))
    dropped_nodes: Counter = field(default_factory=Counter)
    unresolved_nodes_by_prefix: Counter = field(default_factory=Counter)
    flags: Counter = field(default_factory=Counter)
    flags_by_source: dict = field(default_factory=lambda: defaultdict(Counter))
    edges_out_by_source: Counter = field(default_factory=Counter)
    examples: dict = field(default_factory=lambda: defaultdict(list))

    def example(self, reason: str, record: dict) -> None:
        if len(self.examples[reason]) < EXAMPLES_PER_REASON:
            self.examples[reason].append(record)

    def to_dict(self) -> dict:
        return {
            "settings": self.settings,
            "nodes": {"in": self.nodes_in, "out": self.nodes_out},
            "edges": {"in": self.edges_in, "out": self.edges_out},
            "dropped_edges": dict(self.dropped_edges.most_common()),
            "dropped_edges_by_source": {
                source: dict(counts.most_common())
                for source, counts in sorted(self.dropped_edges_by_source.items())
            },
            "dropped_nodes": dict(self.dropped_nodes.most_common()),
            "unresolved_nodes_by_prefix": dict(self.unresolved_nodes_by_prefix.most_common()),
            "flags": dict(self.flags.most_common()),
            "flags_by_source": {
                source: dict(counts.most_common())
                for source, counts in sorted(self.flags_by_source.items())
            },
            "edges_out_by_source": dict(self.edges_out_by_source.most_common()),
            "examples": dict(self.examples),
        }


def edge_drop_reason(
    edge: dict,
    known: set[str],
    unresolved: set[str],
    keep_unresolved: bool,
    drop_lists: dict[str, set[EdgeKey]],
) -> str | None:
    """Why this edge is dropped, or None to keep it."""
    subject, predicate, obj = edge.get("subject"), edge.get("predicate"), edge.get("object")
    if not (subject and predicate and obj):
        return "malformed"
    if not edge.get("primary_knowledge_source"):
        return "no_primary_knowledge_source"
    if subject not in known or obj not in known:
        return "dangling"
    if not keep_unresolved and (subject in unresolved or obj in unresolved):
        return "unresolved_node"
    if drop_lists:
        key = edge_key(edge)
        for name, keys in drop_lists.items():
            if key in keys:
                return f"drop_list:{name}"
    return None


def edge_flags(edge: dict) -> Iterable[str]:
    """Things worth a look on an edge that is kept."""
    if edge["subject"] == edge["object"]:
        yield "self_loop"
    if edge.get("knowledge_level", "not_provided") == "not_provided":
        yield "knowledge_level_not_provided"
    if edge.get("agent_type", "not_provided") == "not_provided":
        yield "agent_type_not_provided"


def collect(
    input_dir: Path,
    output_dir: Path,
    *,
    keep_unresolved: bool = False,
    drop_list_paths: Iterable[Path] = (),
) -> Report:
    """Clean input_dir's nodes.jsonl and edges.jsonl into output_dir, and return the report.

    Three streaming passes, so only node ids are held in memory: nodes (which ids exist and
    which are unresolved), edges (filter, and note which nodes kept edges touch), nodes again
    (write the ones still in use).
    """
    nodes_path, edges_path = input_dir / "nodes.jsonl", input_dir / "edges.jsonl"
    for path in (nodes_path, edges_path):
        if not path.exists():
            raise FileNotFoundError(f"no {path.name} in {input_dir}")
    drop_list_paths = list(drop_list_paths)
    drop_lists = {path.stem: load_drop_list(path) for path in drop_list_paths}
    report = Report(
        settings={
            "input": str(input_dir),
            "keep_unresolved": keep_unresolved,
            "drop_lists": {str(path): len(drop_lists[path.stem]) for path in drop_list_paths},
        }
    )

    known: set[str] = set()
    unresolved: set[str] = set()
    for node in read_jsonl(nodes_path):
        report.nodes_in += 1
        node_id = node.get("id")
        if not node_id:
            continue
        known.add(node_id)
        if not is_resolved(node):
            unresolved.add(node_id)
            report.unresolved_nodes_by_prefix[prefix(node_id)] += 1

    output_dir.mkdir(parents=True, exist_ok=True)
    used: set[str] = set()
    with open(output_dir / "edges.jsonl", "w") as out:
        for edge in read_jsonl(edges_path):
            report.edges_in += 1
            source = edge.get("primary_knowledge_source") or "(none)"
            reason = edge_drop_reason(edge, known, unresolved, keep_unresolved, drop_lists)
            if reason:
                report.dropped_edges[reason] += 1
                report.dropped_edges_by_source[source][reason] += 1
                report.example(reason, edge)
                continue
            for flag in edge_flags(edge):
                report.flags[flag] += 1
                report.flags_by_source[source][flag] += 1
            used.add(edge["subject"])
            used.add(edge["object"])
            report.edges_out += 1
            report.edges_out_by_source[source] += 1
            out.write(json.dumps(edge) + "\n")

    with open(output_dir / "nodes.jsonl", "w") as out:
        for node in read_jsonl(nodes_path):
            node_id = node.get("id")
            if not node_id:
                reason = "malformed"
            elif not keep_unresolved and node_id in unresolved:
                reason = "unresolved"
            elif node_id not in used:
                reason = "orphan"
            else:
                reason = None
            if reason:
                report.dropped_nodes[reason] += 1
                report.example(f"node:{reason}", node)
                continue
            report.nodes_out += 1
            out.write(json.dumps(node) + "\n")

    (output_dir / "cleaning_report.json").write_text(json.dumps(report.to_dict(), indent=2) + "\n")
    return report


def summary(report: Report) -> str:
    """A few lines for the terminal."""
    lines = [
        f"Nodes: {report.nodes_in:,} in, {report.nodes_out:,} out",
        f"Edges: {report.edges_in:,} in, {report.edges_out:,} out",
    ]
    for title, counts in (
        ("Dropped edges", report.dropped_edges),
        ("Dropped nodes", report.dropped_nodes),
        ("Flagged edges (kept)", report.flags),
    ):
        if counts:
            lines.append(f"{title}:")
            lines += [f"  {reason}: {n:,}" for reason, n in counts.most_common()]
    if report.unresolved_nodes_by_prefix:
        top = ", ".join(f"{p} {n:,}" for p, n in report.unresolved_nodes_by_prefix.most_common(5))
        lines.append(f"Unresolved nodes by prefix (top 5): {top}")
    return "\n".join(lines)
