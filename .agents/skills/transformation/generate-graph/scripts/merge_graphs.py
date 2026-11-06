#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["robokop-orion==2.0.13"]
# ///
"""Merge per-source ORION graphs into one KGX graph, using ORION's own merge rules.

    merge_graphs.py GRAPHS OUTPUT [--sources ID ...]

GRAPHS holds one ORION workspace per source (kgb-transform's OUTPUT/graphs). Each source's stable
normalized files are merged with KGXFileMerger's disk merger, because the memory merger behind
ORION's merge_kgx_files can't hold a graph this size. Writes nodes.jsonl, edges.jsonl and
merge-metadata.json to OUTPUT, which must not already hold a merged graph.

ORION merges nodes on id, and edges on subject, predicate, object, primary knowledge source and
qualifiers. List properties such as publications and kgbcia_source_records are combined. Scalars
that conflict keep the first value, and merge-metadata.json counts those it dropped.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def stable_normalization(workspace: Path) -> tuple[Path, str] | None:
    """The newest stable normalized output in a source's workspace, by ORION's own metadata."""
    found = []
    for meta_path in workspace.glob("*/data/*/*.meta.json"):
        meta = json.loads(meta_path.read_text())
        if meta.get("fetch_status") != "stable":
            continue
        for parsing_id, parsing in (meta.get("parsings") or {}).items():
            if parsing.get("parsing_status") != "stable":
                continue
            for norm_id, norm in (parsing.get("normalizations") or {}).items():
                if norm.get("normalization_status") != "stable":
                    continue
                path = meta_path.parent / f"parsed_{parsing_id}" / f"normalized_{norm_id}"
                if (path / "normalized_nodes.jsonl").exists():
                    found.append((norm.get("normalization_time", ""), path))
    if not found:
        return None
    return max(found, key=lambda item: item[0])[1]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("graphs", type=Path, help="Directory of per-source ORION workspaces")
    parser.add_argument("output", type=Path, help="Directory for the merged graph")
    parser.add_argument("--sources", nargs="*", help="Only these source ids (default: all)")
    args = parser.parse_args(argv)

    workspaces = sorted(path for path in args.graphs.iterdir() if path.is_dir())
    if args.sources:
        workspaces = [path for path in workspaces if path.name in args.sources]
    from orion.kgx_file_merger import KGXFileMerger
    from orion.kgxmodel import GraphFileSource, GraphSpec

    class Source(GraphFileSource):
        # ORION looks for "node" and "edge" anywhere in a file's path, and "knowledge" has "edge"
        def get_node_file_paths(self):
            return [f for f in self.file_paths if Path(f).name == "normalized_nodes.jsonl"]

        def get_edge_file_paths(self):
            return [f for f in self.file_paths if Path(f).name == "normalized_edges.jsonl"]

    sources, unbuilt = [], []
    for workspace in workspaces:
        normalized = stable_normalization(workspace)
        if normalized is None:
            unbuilt.append(workspace.name)
            continue
        files = [normalized / "normalized_nodes.jsonl", normalized / "normalized_edges.jsonl"]
        sources.append(Source(id=workspace.name, file_paths=[str(f) for f in files]))
    if not sources:
        print(f"No stable graphs to merge under {args.graphs}", file=sys.stderr)
        return 1

    args.output.mkdir(parents=True, exist_ok=True)
    spec = GraphSpec(
        graph_id="kgbcia",
        graph_name="KGB-CIA merged graph",
        graph_description="Per-source ORION graphs merged with ORION's merge rules",
        graph_url="",
        add_edge_id=True,
        edge_id_type="uuid",
        graph_output_format="jsonl",
        sources=[],
        resolved_sources=sources,
    )
    merger = KGXFileMerger(
        graph_spec=spec,
        output_directory=str(args.output),
        nodes_output_filename="nodes.jsonl",
        edges_output_filename="edges.jsonl",
        save_memory=True,
    )
    merger.merge()
    merger.get_merge_metadata()["sources_not_merged"] = unbuilt
    merger.write_merge_metadata()
    metadata = merger.get_merge_metadata()
    if "merge_error" in metadata:
        print(f"ORION merge failed: {metadata['merge_error']}", file=sys.stderr)
        return 1
    print(
        f"Merged {len(sources)} sources: {metadata['final_node_count']} nodes, "
        f"{metadata['final_edge_count']} edges ({metadata['edges_diff']} duplicate edges merged)."
    )
    if unbuilt:
        print(f"Not merged, no stable graph: {', '.join(unbuilt)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
