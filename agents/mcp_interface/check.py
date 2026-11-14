"""Offline, streaming checks before building the Neo4j dump."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from shared.schema import TransformReport

DEFAULT_INPUTS = (Path("data/cleaning"), Path("data/transformation/merged"))
DEFAULT_REPORT_ROOT = Path("data/transformation")
_MAX_ERROR_SAMPLES = 20
# Edges the augmentation stage proposes from literature carry no transformation record; they
# cite PubMed instead (``publications``) and name the originals they refine
# (``original_edge_ids``). They are accepted without ``kgbcia_source_records``.
AUGMENTOR_SOURCES = frozenset({"infores:kgbcia-augmentor"})


@dataclass(frozen=True)
class CheckResult:
    input_dir: Path
    node_count: int = 0
    edge_count: int = 0
    report_count: int = 0
    mapping_count: int = 0
    error_counts: dict[str, int] = field(default_factory=dict)
    missing_source_records_by_primary_source: dict[str, int] = field(default_factory=dict)
    augmentor_edges_without_records: int = 0
    unknown_mapping_ids: dict[str, int] = field(default_factory=dict)
    error_samples: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not any(self.error_counts.values())


def resolve_default_input(repo_root: Path | str = Path(".")) -> Path | None:
    """Return the first existing cleaning or merged-transformation output."""

    root = Path(repo_root)
    for relative in DEFAULT_INPUTS:
        candidate = root / relative
        if candidate.exists():
            return candidate
    return None


def discover_reports(repo_root: Path | str = Path(".")) -> list[Path]:
    """Find per-source transformation reports in deterministic order."""

    return sorted((Path(repo_root) / DEFAULT_REPORT_ROOT).glob("**/transform_report.json"))


def check_graph(
    input_dir: Path | str,
    report_paths: Iterable[Path | str] | None = None,
    repo_root: Path | str = Path("."),
) -> CheckResult:
    """Stream a KGX graph and report whether it is ready for ``orion-neo4j-dump``."""

    graph_dir = Path(input_dir)
    paths = discover_reports(repo_root) if report_paths is None else [Path(p) for p in report_paths]
    errors: Counter[str] = Counter()
    missing_by_source: Counter[str] = Counter()
    unknown_mapping_ids: Counter[str] = Counter()
    augmentor_counter: Counter[str] = Counter()
    samples: list[str] = []
    mapping_ids: set[str] = set()
    report_count = 0
    mapping_count = 0

    for path in paths:
        try:
            report = TransformReport.model_validate_json(path.read_text())
        except (OSError, ValidationError, ValueError) as exc:
            errors["invalid_report"] += 1
            _sample(samples, f"{path}: {exc}")
            continue
        report_count += 1
        mapping_count += len(report.mappings)
        mapping_ids.update(mapping.id for mapping in report.mappings)

    node_count = _stream_nodes(graph_dir / "nodes.jsonl", errors, samples)
    edge_count = _stream_edges(
        graph_dir / "edges.jsonl",
        mapping_ids,
        errors,
        missing_by_source,
        unknown_mapping_ids,
        augmentor_counter,
        samples,
    )
    return CheckResult(
        input_dir=graph_dir,
        node_count=node_count,
        edge_count=edge_count,
        report_count=report_count,
        mapping_count=mapping_count,
        error_counts=dict(sorted(errors.items())),
        missing_source_records_by_primary_source=dict(sorted(missing_by_source.items())),
        augmentor_edges_without_records=augmentor_counter["edges"],
        unknown_mapping_ids=dict(sorted(unknown_mapping_ids.items())),
        error_samples=tuple(samples),
    )


def format_check_result(result: CheckResult) -> str:
    """Format counts, bounded diagnostics and the exact next commands."""

    lines = [
        f"input: {result.input_dir}",
        f"nodes: {result.node_count}",
        f"edges: {result.edge_count}",
        f"reports: {result.report_count}",
        f"mappings: {result.mapping_count}",
    ]
    for source, count in result.missing_source_records_by_primary_source.items():
        lines.append(f"missing kgbcia_source_records [{source}]: {count}")
    if result.augmentor_edges_without_records:
        lines.append(
            "augmentor edges without kgbcia_source_records (cite publications instead): "
            f"{result.augmentor_edges_without_records}"
        )
    for name, count in result.error_counts.items():
        lines.append(f"error {name}: {count}")
    lines.extend(f"  {sample}" for sample in result.error_samples)
    if result.ok:
        lines.extend(
            [
                "ready for Neo4j dump preparation",
                "next: agents/mcp_interface/deploy/prepare_dump.sh <arguments>",
            ]
        )
    else:
        lines.append("not ready for Neo4j dump preparation")
    return "\n".join(lines)


def _stream_nodes(path: Path, errors: Counter[str], samples: list[str]) -> int:
    if not path.is_file():
        errors["missing_nodes_file"] += 1
        _sample(samples, f"missing {path}")
        return 0
    count = 0
    with path.open() as stream:
        for line_number, line in enumerate(stream, 1):
            count += 1
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("node must be a JSON object")
            except (json.JSONDecodeError, ValueError) as exc:
                errors["invalid_node"] += 1
                _sample(samples, f"{path}:{line_number}: {exc}")
    return count


def _stream_edges(
    path: Path,
    mapping_ids: set[str],
    errors: Counter[str],
    missing_by_source: Counter[str],
    unknown_mapping_ids: Counter[str],
    augmentor_counter: Counter[str],
    samples: list[str],
) -> int:
    if not path.is_file():
        errors["missing_edges_file"] += 1
        _sample(samples, f"missing {path}")
        return 0
    count = 0
    with path.open() as stream:
        for line_number, line in enumerate(stream, 1):
            count += 1
            location = f"{path}:{line_number}"
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, ValueError) as exc:
                errors["invalid_edge"] += 1
                _sample(samples, f"{location}: {exc}")
                continue
            if not isinstance(value, dict):
                errors["invalid_edge"] += 1
                _sample(samples, f"{location}: edge must be a JSON object")
                continue
            if not value.get("id"):
                errors["missing_edge_id"] += 1
                _sample(samples, f"{location}: missing edge id")
            source = value.get("primary_knowledge_source")
            source_name = source if isinstance(source, str) and source else "<unknown>"
            references = value.get("kgbcia_source_records")
            if (
                references is None
                and source in AUGMENTOR_SOURCES
                and value.get("publications")
                and value.get("original_edge_ids")
            ):
                augmentor_counter["edges"] += 1
                continue
            if references is None:
                errors["missing_source_records"] += 1
                missing_by_source[source_name] += 1
                _sample(samples, f"{location}: missing kgbcia_source_records")
                continue
            if not isinstance(references, list):
                errors["non_list_source_records"] += 1
                _sample(samples, f"{location}: kgbcia_source_records must be a list")
                continue
            if not references:
                errors["empty_source_records"] += 1
                _sample(samples, f"{location}: kgbcia_source_records must not be empty")
            for reference in references:
                if not isinstance(reference, str) or "#" not in reference:
                    errors["malformed_source_record"] += 1
                    _sample(samples, f"{location}: malformed source record {reference!r}")
                    continue
                mapping_id, record_key = reference.split("#", 1)
                if not mapping_id or not record_key:
                    errors["malformed_source_record"] += 1
                    _sample(samples, f"{location}: malformed source record {reference!r}")
                elif mapping_id not in mapping_ids:
                    errors["unknown_mapping_id"] += 1
                    unknown_mapping_ids[mapping_id] += 1
                    _sample(samples, f"{location}: unknown mapping id {mapping_id!r}")
    return count


def _sample(samples: list[str], message: str) -> None:
    if len(samples) < _MAX_ERROR_SAMPLES:
        normalized = " ".join(message.split())
        samples.append(normalized[:500])
