"""Run versioned source parsers and assemble shared-schema graphs from a JSON manifest."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import json
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

# Support direct execution and importing the script from tests without installing the skill.
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[4]
for directory in (REPO_ROOT, SCRIPT_DIR):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from parser_contract import PARSER_API_VERSION, Diagnostic, Envelope  # noqa: E402

from shared import nodenorm  # noqa: E402
from shared.schema import Edge, Graph, Node  # noqa: E402


class BuildError(Exception):
    """A structural failure, as opposed to a rejected source record."""


def read_document(path: Path) -> Any:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        text = stream.read()
    return json.loads(text) if path.name.removesuffix(".gz").endswith(".json") else text


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    """Replace complete artifacts atomically, rather than leave truncated JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


class Vocabulary:
    """A pinned Biolink vocabulary export with direct class parents and predicates."""

    def __init__(self, document: dict):
        if not isinstance(document, dict):
            raise BuildError("Biolink export must be a JSON object")
        self.version = document.get("version")
        self.classes = document.get("classes")
        predicates = document.get("predicates")
        if (
            not isinstance(self.version, str)
            or not self.version
            or not isinstance(self.classes, dict)
            or not self.classes
            or not isinstance(predicates, list)
            or not predicates
        ):
            raise BuildError("Biolink export requires version, classes, and predicates")
        self.predicates = set()
        for predicate in predicates:
            if not isinstance(predicate, str) or not predicate.startswith("biolink:"):
                raise BuildError("Invalid Biolink predicate export")
            self.predicates.add(predicate)
        for category, parents in self.classes.items():
            if (
                not isinstance(category, str)
                or not category.startswith("biolink:")
                or not isinstance(parents, list)
                or any(
                    not isinstance(parent, str) or parent not in self.classes for parent in parents
                )
            ):
                raise BuildError("Biolink classes must list known direct parent CURIEs")
        self.ancestors: dict[str, set[str]] = {}
        for category in self.classes:
            self._ancestors(category, set())

    def _ancestors(self, category: str, visiting: set[str]) -> set[str]:
        if category in visiting:
            raise BuildError("Cycle in Biolink class hierarchy")
        if category not in self.ancestors:
            parents = self.classes[category]
            ancestors = set(parents)
            for parent in parents:
                ancestors.update(self._ancestors(parent, visiting | {category}))
            self.ancestors[category] = ancestors
        return self.ancestors[category]

    def category(self, mapped: str, returned: list[str]) -> str:
        types = set(returned) & self.classes.keys()
        if mapped in types:
            return mapped
        leaves = types - {ancestor for item in types for ancestor in self.ancestors[item]}
        if not leaves:
            raise ValueError("Resolved node has no returned category in the pinned Biolink export")
        return sorted(leaves)[0]


def load_parser(path: Path):
    name = "generated_parser_" + hashlib.sha256(str(path.resolve()).encode()).hexdigest()
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise BuildError(f"Cannot load parser: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    version = getattr(module, "PARSER_API_VERSION", None)
    if type(version) is not int or version != PARSER_API_VERSION:
        raise BuildError(f"Parser {path} must declare PARSER_API_VERSION = {PARSER_API_VERSION}")
    if not callable(getattr(module, "parse", None)):
        raise BuildError(f"Parser {path} must expose parse(source_files, metadata)")
    return module


def normalized_response(response: Any, identifiers: list[str]) -> dict:
    if not isinstance(response, dict):
        raise BuildError("NodeNorm response must be an identifier mapping")
    result = {}
    for identifier in identifiers:
        entry = response.get(identifier)
        if entry:
            if not isinstance(entry, dict):
                raise BuildError("Invalid NodeNorm entry")
            preferred = entry.get("id")
            types = entry.get("type", [])
            if (
                not isinstance(preferred, dict)
                or not isinstance(preferred.get("identifier"), str)
                or not preferred["identifier"]
                or not isinstance(types, list)
                or any(not isinstance(item, str) for item in types)
                or (preferred.get("label") is not None and not isinstance(preferred["label"], str))
            ):
                raise BuildError("Invalid NodeNorm identifier, label, or types")
        elif entry not in (None, {}):
            raise BuildError("Invalid unresolved NodeNorm entry")
        result[identifier] = entry or None
    return result


def build_graph(
    parser_path: Path,
    source_files: Mapping[str, Path],
    metadata: dict,
    vocabulary: Vocabulary,
    source: str,
    policy: str = "lenient",
    batch_size: int = 500,
    url: str = nodenorm.DEFAULT_URL,
    replay: dict | None = None,
    normalizer: Callable | None = None,
) -> tuple[Graph, dict, dict]:
    """Build one source graph. Return the graph, diagnostic report, and raw response snapshot."""
    if policy not in {"strict", "lenient"}:
        raise BuildError("Normalization policy must be strict or lenient")
    if type(batch_size) is not int or batch_size < 1:
        raise BuildError("batch_size must be a positive integer")
    for path in source_files.values():
        if not Path(path).is_file():
            raise BuildError(f"Source file not available: {path}")

    diagnostics = []
    candidates: list[tuple[Node, dict]] = []
    edges: list[tuple[Edge, dict]] = []
    counts = {"node_records": 0, "edge_records": 0, "duplicate_edges": 0, "unresolved_ids": 0}

    def diagnostic(code, message, location=None, *, skipped=True, severity="warning"):
        diagnostics.append(
            {
                "code": code,
                "message": message,
                "location": location,
                "severity": severity,
                "skipped": skipped,
            }
        )

    try:
        parser = load_parser(parser_path)
        for position, raw in enumerate(parser.parse(dict(source_files), metadata), start=1):
            if not isinstance(raw, dict) or raw.get("kind") not in {"node", "edge", "diagnostic"}:
                raise BuildError(f"Parser yielded an invalid envelope at position {position}")
            kind = raw["kind"]
            if kind != "diagnostic":
                counts[f"{kind}_records"] += 1
            location = {"parser_record": position}
            try:
                envelope = Envelope.model_validate(raw)
                location = envelope.location.model_dump()
                if kind == "diagnostic":
                    item = Diagnostic.model_validate(envelope.data)
                    diagnostics.append({**item.model_dump(), "location": location})
                    continue
                model = Node if kind == "node" else Edge
                extra = envelope.data.keys() - model.model_fields.keys()
                if extra:
                    raise ValueError(f"Unexpected {kind} fields: {sorted(extra)}")
                record = model.model_validate(envelope.data, strict=True)
                # Reject non-finite numbers before serialization can turn them into null.
                json.dumps(record.model_dump(), allow_nan=False)
                if kind == "node":
                    if not record.id.strip() or record.category not in vocabulary.classes:
                        raise ValueError("Node requires an ID and a category in the Biolink export")
                    candidates.append((record, location))
                else:
                    if record.predicate not in vocabulary.predicates:
                        raise ValueError("Edge predicate is not in the Biolink export")
                    provenance = record.provenance
                    if (
                        provenance.source != source
                        or not provenance.file.strip()
                        or not provenance.columns
                        or any(not column.strip() for column in provenance.columns)
                    ):
                        raise ValueError(
                            "Edge provenance requires matching source, file, and columns"
                        )
                    if provenance.file != envelope.location.file:
                        raise ValueError("Edge provenance file must match its source location")
                    if not record.subject.strip() or not record.object.strip():
                        raise ValueError("Edge endpoints must not be empty")
                    edges.append((record, location))
            except (ValidationError, ValueError, TypeError) as error:
                diagnostic("invalid_record", str(error), location)
    except BuildError:
        raise
    except Exception as error:
        raise BuildError(f"Parser failed for {source}: {error}") from error

    identifiers = sorted({node.id for node, _ in candidates})
    responses = {}
    try:
        for offset in range(0, len(identifiers), batch_size):
            batch = identifiers[offset : offset + batch_size]
            if replay is not None:
                if not isinstance(replay, dict) or any(item not in replay for item in batch):
                    raise BuildError("Normalization replay is missing requested identifiers")
                response = replay
            else:
                response = (normalizer or nodenorm.normalize)(batch, url=url)
            responses.update(normalized_response(response, batch))
    except BuildError:
        raise
    except Exception as error:
        raise BuildError(f"Normalization service failed for {source}: {error}") from error

    nodes: dict[str, Node] = {}
    id_map = {}
    counts["unresolved_ids"] = sum(responses[item] is None for item in identifiers)
    for node, location in candidates:
        canonical = nodenorm.preferred_id(node.id, responses)
        if canonical is None:
            diagnostic(
                "unresolved_id", node.id, location, skipped=policy == "strict", severity="info"
            )
            if policy == "strict":
                continue
            canonical, category, name = node.id, node.category, node.name
        else:
            entry = responses[node.id]
            try:
                category = vocabulary.category(node.category, entry.get("type", []))
            except ValueError as error:
                diagnostic("invalid_category", str(error), location)
                continue
            name = entry["id"].get("label") or node.name
        original_ids = sorted(set([node.id, *node.original_ids]))
        candidate = Node(
            id=canonical,
            category=category,
            name=name,
            original_ids=original_ids,
            properties=node.properties,
        )
        existing = nodes.get(canonical)
        if existing:
            conflict = existing.category != category or any(
                key in existing.properties and existing.properties[key] != value
                for key, value in candidate.properties.items()
            )
            if conflict:
                diagnostic("node_conflict", f"Conflicting candidate for {canonical}", location)
                continue
            existing.original_ids = sorted(set(existing.original_ids) | set(original_ids))
            existing.properties.update(candidate.properties)
            if existing.name is None:
                existing.name = name
        else:
            nodes[canonical] = candidate
        id_map[node.id] = canonical

    final_edges: dict[str, Edge] = {}
    for edge, location in edges:
        if edge.subject not in id_map or edge.object not in id_map:
            diagnostic("missing_endpoint", f"{edge.subject} -> {edge.object}", location)
            continue
        edge = edge.model_copy(
            update={"subject": id_map[edge.subject], "object": id_map[edge.object]}
        )
        # Row/path lineage distinguishes assertions even if all other edge fields match.
        provenance = edge.provenance.model_copy()
        lineage = json.dumps(location, sort_keys=True)
        provenance.notes = (
            f"{provenance.notes}\nSource location: {lineage}"
            if provenance.notes
            else (f"Source location: {lineage}")
        )
        edge.provenance = provenance
        key = json.dumps(edge.model_dump(), sort_keys=True)
        if key in final_edges:
            counts["duplicate_edges"] += 1
        final_edges[key] = edge

    graph = Graph(
        nodes=[nodes[key] for key in sorted(nodes)],
        edges=[final_edges[key] for key in sorted(final_edges)],
    )
    graph = Graph.model_validate_json(graph.model_dump_json())
    counts.update(
        emitted_nodes=len(graph.nodes),
        emitted_edges=len(graph.edges),
        skipped_records=sum(item["skipped"] for item in diagnostics),
    )
    report = {
        "source": source,
        "status": "partial" if counts["skipped_records"] else "complete",
        "normalization_policy": policy,
        "biolink_version": vocabulary.version,
        "counts": counts,
        "diagnostics": diagnostics,
    }
    return graph, report, responses


def run_manifest(path: Path) -> int:
    """Paths in the manifest are relative to the manifest file, never to the shell cwd."""
    path = path.resolve()
    manifest = read_document(path)
    if not isinstance(manifest, dict):
        raise BuildError("Build manifest must be a JSON object")
    base = path.parent

    def resolve(value):
        if not isinstance(value, str) or not value:
            raise BuildError("Manifest file paths must be nonempty strings")
        return (base / value).resolve()

    output = resolve(manifest.get("output_dir", "graphs"))
    vocabulary_path = resolve(manifest.get("biolink"))
    vocabulary = Vocabulary(read_document(vocabulary_path))
    sources = manifest.get("sources")
    if not isinstance(sources, list) or not sources:
        raise BuildError("Build manifest requires a nonempty sources list")
    names = []
    for source in sources:
        name = source.get("name") if isinstance(source, dict) else None
        if (
            not isinstance(name, str)
            or not name
            or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in name)
            or name in names
        ):
            raise BuildError("Source names must be unique lowercase slugs")
        names.append(name)
    results = []
    for spec in sources:
        name = spec["name"]
        destination = output / name
        try:
            parser_path = resolve(spec.get("parser"))
            metadata_paths = {key: resolve(value) for key, value in spec["metadata"].items()}
            source_files = {key: resolve(value) for key, value in spec["source_files"].items()}
            metadata = {key: read_document(value) for key, value in metadata_paths.items()}
            replay_path = (
                resolve(spec["normalization_responses"])
                if spec.get("normalization_responses")
                else None
            )
            graph, report, responses = build_graph(
                parser_path,
                source_files,
                metadata,
                vocabulary,
                name,
                policy=spec.get("normalization_policy", "lenient"),
                batch_size=manifest.get("batch_size", 500),
                url=manifest.get("nodenorm_url", nodenorm.DEFAULT_URL),
                replay=read_document(replay_path) if replay_path else None,
            )
            report["manifest"] = {
                "parser_api_version": PARSER_API_VERSION,
                "parser_sha256": checksum(parser_path),
                "metadata_sha256": {key: checksum(value) for key, value in metadata_paths.items()},
                "source_sha256": {key: checksum(value) for key, value in source_files.items()},
                "source_version": spec.get("source_version"),
                "generation_model_id": spec.get("generation_model_id"),
                "biolink_sha256": checksum(vocabulary_path),
                "nodenorm_url": manifest.get("nodenorm_url", nodenorm.DEFAULT_URL),
                "conflate": True,
                "batch_size": manifest.get("batch_size", 500),
                "normalization_mode": "replay" if replay_path else "live",
            }
            write_json(destination / "normalization.json", responses)
            write_json(destination / "graph.json", graph.model_dump())
            write_json(destination / "report.json", report)
            results.append(
                {
                    "source": name,
                    "status": report["status"],
                    "graph": str(destination / "graph.json"),
                }
            )
        except Exception as error:
            report = {"source": name, "status": "failed", "error": str(error)}
            write_json(destination / "report.json", report)
            results.append({"source": name, "status": "failed", "graph": None})
    write_json(output / "build_report.json", {"sources": results})
    return int(any(result["status"] == "failed" for result in results))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", nargs="?", default="data/transformation/build.json")
    args = parser.parse_args(argv)
    try:
        return run_manifest(Path(args.manifest))
    except Exception as error:
        print(f"Graph build failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
