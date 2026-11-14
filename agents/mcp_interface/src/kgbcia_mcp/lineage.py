"""Resolve edge source-record pointers against transformation reports."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass
from typing import Any

from .report_models import Mapping, TransformReport

_PROVENANCE_FIELDS = (
    "primary_knowledge_source",
    "aggregator_knowledge_source",
    "knowledge_level",
    "agent_type",
    "publications",
    "original_subject",
    "original_object",
    "original_predicate",
)


@dataclass(frozen=True)
class _IndexedMapping:
    report: TransformReport
    mapping: Mapping


class LineageIndex:
    """Small startup index over reports; graph records remain in Neo4j."""

    def __init__(self, reports: Iterable[TransformReport] = ()) -> None:
        self._reports = tuple(reports)
        by_id: dict[str, list[_IndexedMapping]] = defaultdict(list)
        for report in self._reports:
            for mapping in report.mappings:
                by_id[mapping.id].append(_IndexedMapping(report, mapping))
        self._by_id = dict(by_id)

    @classmethod
    def from_reports(cls, reports: Iterable[TransformReport]) -> LineageIndex:
        return cls(reports)

    def resolve_source_records(
        self,
        edge: MappingABC[str, Any],
        subject_categories: Iterable[str] = (),
        object_categories: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Return Biolink provenance and exact or explicitly candidate file lineage."""

        result: dict[str, Any] = {
            "edge": dict(edge),
            "provenance": {
                key: edge[key] for key in _PROVENANCE_FIELDS if edge.get(key) is not None
            },
            "lineage": [],
        }
        references = edge.get("kgbcia_source_records")
        edge_sources = _edge_sources(edge)
        unresolved: list[str] = []
        grouped: dict[tuple[str, int], list[str]] = defaultdict(list)

        if isinstance(references, list):
            for reference in references:
                if not isinstance(reference, str) or "#" not in reference:
                    unresolved.append(str(reference))
                    continue
                mapping_id, record_key = reference.split("#", 1)
                matches = self._by_id.get(mapping_id, ())
                source_matches = [
                    (position, match)
                    for position, match in enumerate(matches)
                    if match.report.source in edge_sources
                ]
                if not mapping_id or not record_key or len(source_matches) != 1:
                    unresolved.append(reference)
                    continue
                position, _match = source_matches[0]
                grouped[(mapping_id, position)].append(record_key)
        elif references is not None:
            unresolved.append(str(references))

        exact = []
        for (mapping_id, position), record_keys in grouped.items():
            indexed = self._by_id[mapping_id][position]
            exact.append(_lineage_entry(indexed, "exact", sorted(record_keys), len(record_keys)))
        exact.sort(key=lambda item: (item["mapping_id"], item["source"]))
        result["lineage"].extend(exact)

        needs_fallback = not isinstance(references, list) or not references or bool(unresolved)
        if needs_fallback:
            candidates = self._candidate_matches(
                edge, set(subject_categories), set(object_categories)
            )
            result["lineage"].extend(candidates)
        if unresolved:
            result["unresolved_source_records"] = unresolved

        if not self._reports:
            result["hint"] = (
                "Transformation reports are not configured; file and column lineage is "
                "unavailable. Biolink provenance is still shown verbatim."
            )
        elif not result["lineage"]:
            result["hint"] = (
                "No transformation mapping matched this edge; cite the Biolink provenance and "
                "treat file and column lineage as unavailable."
            )
        elif needs_fallback:
            result["hint"] = "Candidate lineage is not an exact source-record match."
        return result

    def _candidate_matches(
        self,
        edge: MappingABC[str, Any],
        subject_categories: set[str],
        object_categories: set[str],
    ) -> list[dict[str, Any]]:
        sources = _edge_sources(edge)
        predicate = edge.get("predicate")
        candidates = []
        for report in self._reports:
            if report.source not in sources:
                continue
            for mapping in report.mappings:
                if mapping.produces != "edge" or mapping.predicate != predicate:
                    continue
                if subject_categories and mapping.subject_category not in subject_categories:
                    continue
                if object_categories and mapping.object_category not in object_categories:
                    continue
                candidates.append(_lineage_entry(_IndexedMapping(report, mapping), "candidate"))
        return sorted(candidates, key=lambda item: (item["mapping_id"], item["source"]))


def _edge_sources(edge: MappingABC[str, Any]) -> set[str]:
    sources = {edge.get("primary_knowledge_source")}
    aggregators = edge.get("aggregator_knowledge_source", [])
    if isinstance(aggregators, list):
        sources.update(aggregators)
    return {source for source in sources if isinstance(source, str)}


def _lineage_entry(
    indexed: _IndexedMapping,
    match: str,
    record_keys: list[str] | None = None,
    record_count: int | None = None,
) -> dict[str, Any]:
    report = indexed.report
    mapping = indexed.mapping
    entry: dict[str, Any] = {
        "match": match,
        "mapping_id": mapping.id,
        "source": report.source,
        "source_version": report.source_version,
        "croissant": report.croissant,
        "file": mapping.file,
        "record_set": mapping.record_set,
        "columns": {
            slot: [column.model_dump() for column in columns]
            for slot, columns in mapping.columns.items()
        },
        "constants": mapping.constants,
    }
    if record_keys is not None:
        entry["record_keys"] = record_keys
        entry["record_count"] = record_count
    return entry
