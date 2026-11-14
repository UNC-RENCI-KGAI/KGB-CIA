#!/usr/bin/env python3
"""Create synthetic lineage artifacts for the local LitCoin development sample.

The development graph predates KGB-CIA lineage fields.  This script is intentionally
separate from transformation output: it rewrites a *copy* of its edges and emits
synthetic TransformReports solely for exercising dump preparation and MCP lineage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

SYNTHETIC_VERSION = "synthetic"
_DEFAULT_SOURCE = "infores:unknown"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read JSON-object records from a KGX JSONL file."""

    records: list[dict[str, Any]] = []
    with path.open() as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected a JSON object")
            records.append(value)
    return records


def synthetic_edge_id(edge: dict[str, Any], ordinal: int) -> str:
    """Return an existing ID or a deterministic synthetic ID for an edge."""

    if isinstance(edge.get("id"), str) and edge["id"]:
        return edge["id"]
    material = "\x1f".join(
        [
            str(edge.get("subject", "")),
            str(edge.get("predicate", "")),
            str(edge.get("object", "")),
            str(ordinal),
        ]
    )
    return f"synthetic-{hashlib.sha256(material.encode()).hexdigest()[:16]}"


def _mapping_id(source: str, ordinal: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", source.lower()).strip("_") or "unknown"
    return f"synthetic.{slug}.{ordinal}"


def create_dev_artifacts(input_dir: Path, output_dir: Path) -> dict[str, int]:
    """Write rewritten edges and synthetic reports; return output counts."""

    nodes = read_jsonl(input_dir / "nodes.jsonl")
    edges = read_jsonl(input_dir / "edges.jsonl")
    categories = {
        node_id: category[0]
        for node in nodes
        if isinstance((node_id := node.get("id")), str)
        and isinstance((category := node.get("category")), list)
        and category
        and isinstance(category[0], str)
    }
    if len(categories) != len(nodes):
        raise ValueError("every sample node must have an id and a non-empty category list")

    combinations: dict[str, dict[tuple[str, str, str], str]] = defaultdict(dict)
    counts: Counter[str] = Counter()
    for edge in edges:
        source = edge.get("primary_knowledge_source", _DEFAULT_SOURCE)
        if not isinstance(source, str) or not source:
            source = _DEFAULT_SOURCE
        subject = edge.get("subject")
        object_ = edge.get("object")
        predicate = edge.get("predicate")
        if not all(isinstance(value, str) and value for value in (subject, object_, predicate)):
            raise ValueError("every sample edge must have subject, predicate, and object")
        try:
            combo = (predicate, categories[subject], categories[object_])
        except KeyError as exc:
            raise ValueError(f"edge endpoint {exc.args[0]!r} is absent from nodes.jsonl") from exc
        if combo not in combinations[source]:
            combinations[source][combo] = _mapping_id(source, len(combinations[source]) + 1)
        counts[source] += 1

    output_dir.mkdir(parents=True, exist_ok=True)
    reports_dir = output_dir / "reports"
    reports_dir.mkdir(exist_ok=True)
    for source, source_combinations in sorted(combinations.items()):
        mappings = [
            {
                "id": mapping_id,
                "file": "edges.jsonl",
                "record_set": "synthetic_edges",
                "produces": "edge",
                "subject_category": subject_category,
                "predicate": predicate,
                "object_category": object_category,
                "columns": {
                    "subject": [{"field": "subject", "name": "subject"}],
                    "predicate": [{"field": "predicate", "name": "predicate"}],
                    "object": [{"field": "object", "name": "object"}],
                },
                "constants": {"lineage": "synthetic"},
                "rows_read": counts[source],
                "records_written": counts[source],
            }
            for (predicate, subject_category, object_category), mapping_id in sorted(
                source_combinations.items()
            )
        ]
        report = {
            "source": source,
            "source_version": SYNTHETIC_VERSION,
            "croissant": "synthetic://litcoin-development-sample",
            "mappings": mappings,
        }
        report_name = re.sub(r"[^A-Za-z0-9._-]+", "_", source)
        (reports_dir / report_name).mkdir(exist_ok=True)
        (reports_dir / report_name / "transform_report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n"
        )

    rewritten: list[dict[str, Any]] = []
    for ordinal, edge in enumerate(edges, 1):
        copy = dict(edge)
        source = copy.get("primary_knowledge_source", _DEFAULT_SOURCE)
        if not isinstance(source, str) or not source:
            source = _DEFAULT_SOURCE
            copy["primary_knowledge_source"] = source
        combo = (copy["predicate"], categories[copy["subject"]], categories[copy["object"]])
        mapping_id = combinations[source][combo]
        copy["id"] = synthetic_edge_id(copy, ordinal)
        references = [f"{mapping_id}#{ordinal}"]
        # Deliberately model a merged edge for lineage tests without inventing a second mapping.
        if ordinal % 25 == 0:
            references.append(f"{mapping_id}#synthetic-merged-{ordinal}")
        copy["kgbcia_source_records"] = references
        if ordinal == 1:
            copy["kgbcia_flags"] = ["wrong_mechanism"]
            copy["kgbcia_flag_reasons"] = ["PMID:20371707 reports a different action"]
        elif ordinal == 2:
            copy["kgbcia_flags"] = ["wrong_mechanism", "type_violation"]
            copy["kgbcia_flag_reasons"] = [
                "PMID:20371707 reports a different action",
                "Synthetic Biolink range mismatch for development testing",
            ]
        elif ordinal == 3:
            copy["kgbcia_flags"] = ["wrong_mechanism", "rare_category_combination"]
            copy["kgbcia_flag_reasons"] = ["Deliberately mismatched reason list"]
        rewritten.append(copy)
    (output_dir / "edges.jsonl").write_text(
        "".join(json.dumps(edge, sort_keys=True) + "\n" for edge in rewritten)
    )
    (output_dir / "nodes.jsonl").write_text(
        "".join(json.dumps(node, sort_keys=True) + "\n" for node in nodes)
    )
    return {"nodes": len(nodes), "edges": len(rewritten), "reports": len(combinations)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-dir", type=Path, required=True, help="directory with KGX JSONL files"
    )
    parser.add_argument(
        "--output-dir", type=Path, required=True, help="new directory for synthetic graph"
    )
    args = parser.parse_args(argv)
    counts = create_dev_artifacts(args.input_dir, args.output_dir)
    summary = ", ".join(f"{key}={value}" for key, value in counts.items())
    print(f"wrote synthetic development artifacts: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
