#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["robokop-orion==2.0.13"]
# ///
"""Merge per-source ORION graphs into one KGX graph, using ORION's own merge rules.

    merge_graphs.py GRAPHS OUTPUT [--sources ID ...]

GRAPHS holds one ORION workspace per source (kgb-transform's OUTPUT/graphs). Each source's stable
normalized files are merged with KGXFileMerger's disk merger, because the memory merger behind
ORION's merge_kgx_files can't hold a graph this size. Writes nodes.jsonl, edges.jsonl,
merge-metadata.json and merge_inputs.json to OUTPUT.

A source's normalized files are its last successful ORION run, named by current_build.json in its
workspace. If merge_inputs.json shows OUTPUT already holds a merge of exactly those builds, with
this script, nothing runs. Otherwise OUTPUT's earlier merged graph is replaced.

ORION merges nodes on id, and edges on subject, predicate, object, primary knowledge source and
qualifiers. List properties such as publications and kgbcia_source_records are combined. Scalars
that conflict keep the first value, and merge-metadata.json counts those it dropped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

INPUTS = "merge_inputs.json"
MERGED = ("nodes.jsonl", "edges.jsonl", "merge-metadata.json")


def current_graph(workspace: Path) -> Path | None:
    """The normalized output of a source's last successful ORION run, if it has one."""
    build = workspace / "current_build.json"
    if not build.exists():
        return None
    path = workspace / json.loads(build.read_text())["normalized"]
    return path if (path / "normalized_nodes.jsonl").exists() else None


def merged_already(output: Path, inputs: dict) -> bool:
    record = output / INPUTS
    if not record.exists() or not all((output / name).exists() for name in MERGED):
        return False
    return json.loads(record.read_text()) == inputs


def clear_merge(output: Path) -> None:
    """Remove an earlier merge, and what an interrupted one left; ORION won't overwrite them."""
    for path in [*(output / name for name in (INPUTS, *MERGED)), *output.glob("*.temp")]:
        path.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("graphs", type=Path, help="Directory of per-source ORION workspaces")
    parser.add_argument("output", type=Path, help="Directory for the merged graph")
    parser.add_argument("--sources", nargs="*", help="Only these source ids (default: all)")
    args = parser.parse_args(argv)

    workspaces = sorted(path for path in args.graphs.iterdir() if path.is_dir())
    unknown = set()
    if args.sources:
        workspaces = [path for path in workspaces if path.name in args.sources]
        unknown = set(args.sources) - {path.name for path in workspaces}
    from orion.kgx_file_merger import KGXFileMerger
    from orion.kgxmodel import GraphFileSource, GraphSpec

    class Source(GraphFileSource):
        # ORION looks for "node" and "edge" anywhere in a file's path, and "knowledge" has "edge"
        def get_node_file_paths(self):
            return [f for f in self.file_paths if Path(f).name == "normalized_nodes.jsonl"]

        def get_edge_file_paths(self):
            return [f for f in self.file_paths if Path(f).name == "normalized_edges.jsonl"]

    sources, unbuilt, builds = [], sorted(unknown), {}
    for workspace in workspaces:
        normalized = current_graph(workspace)
        if normalized is None:
            unbuilt.append(workspace.name)
            continue
        files = [normalized / "normalized_nodes.jsonl", normalized / "normalized_edges.jsonl"]
        sources.append(Source(id=workspace.name, file_paths=[str(f) for f in files]))
        builds[workspace.name] = str(normalized.relative_to(workspace))
    if not sources:
        print(f"No stable graphs to merge under {args.graphs}", file=sys.stderr)
        return 1

    inputs = {
        "builds": builds,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    if merged_already(args.output, inputs):
        print(f"Merged graph unchanged: the same {len(sources)} builds are in {args.output}.")
        return 0
    args.output.mkdir(parents=True, exist_ok=True)
    clear_merge(args.output)
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
    (args.output / INPUTS).write_text(json.dumps(inputs, indent=2, sort_keys=True) + "\n")
    print(
        f"Merged {len(sources)} sources: {metadata['final_node_count']} nodes, "
        f"{metadata['final_edge_count']} edges ({metadata['edges_diff']} duplicate edges merged)."
    )
    if unbuilt:
        print(f"Not merged, no stable graph: {', '.join(unbuilt)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
