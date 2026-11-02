"""Generic semantic-mapping reader used by generated native ORION loaders.

ORION owns KGX writing, normalization, merging, and graph construction. This module only
extracts explicitly mapped source records and supplies TransformReport lineage.
"""

from __future__ import annotations

import argparse
import ast
import bz2
import csv
import gzip
import hashlib
import itertools
import json
import lzma
import math
import os
import shutil
import tarfile
import tempfile
import urllib.request
import zipfile
from contextlib import contextmanager
from pathlib import Path

from orion.kgx_file_writer import KGXFileWriter
from orion.loader_interface import SourceDataLoader
from orion.merging import DiskGraphMerger
from pydantic import ValidationError

if __package__:
    from .schema import ColumnRef, Edge, Mapping, Node, Skipped, TransformReport
else:
    from shared.schema import ColumnRef, Edge, Mapping, Node, Skipped, TransformReport

MANY = {"category", "equivalent_identifiers", "xref", "publications", "aggregator_knowledge_source"}
ENUMS = {"knowledge_level", "agent_type"}
IDENTITY = {"id", "subject", "object", "predicate", "category"}


class MergingNodeWriter(KGXFileWriter):
    """Use ORION's merger instead of its first-occurrence node deduplication."""

    def __init__(self, nodes_output_file_path=None, edges_output_file_path=None):
        super().__init__(nodes_output_file_path, edges_output_file_path)
        self._merge_directory = None
        self._node_merger = None
        self.merge_warnings = {}
        if nodes_output_file_path:
            self._merge_directory = tempfile.TemporaryDirectory(
                prefix="node-merge-", dir=Path(nodes_output_file_path).parent
            )
            self._node_merger = DiskGraphMerger(
                temp_directory=self._merge_directory.name, chunk_size=100_000
            )

    def write_node(
        self, node_id, node_name="", node_types=None, node_properties=None, uniquify=True
    ):
        node = {"id": node_id, "name": node_name, "category": node_types or []}
        node.update(node_properties or {})
        self.write_normalized_node(node)

    def write_normalized_node(self, node_json, uniquify=True):
        self._node_merger.merge_node(node_json)
        if node_json["id"] in self.written_nodes:
            self.repeat_node_count += 1
        else:
            self.written_nodes.add(node_json["id"])
            # SourceDataLoader reads counts before it closes its writer.
            self.nodes_written += 1

    def close(self):
        try:
            if self._node_merger is not None:
                merger, self._node_merger = self._node_merger, None
                self.nodes_written = 0
                for line in merger.get_merged_nodes_jsonl():
                    super().write_normalized_node(json.loads(line), uniquify=False)
                self.merge_warnings = {
                    kind: dict(counts) for kind, counts in merger.merge_warnings.items()
                }
        finally:
            try:
                super().close()
            finally:
                if self._merge_directory is not None:
                    self._merge_directory.cleanup()
                    self._merge_directory = None


@contextmanager
def merging_node_writers():
    """Scope writer registration to this single-source ORION invocation."""
    from orion import kgx_file_normalizer, loader_interface

    original_loader = loader_interface.KGXFileWriter
    original_normalizer = kgx_file_normalizer.KGXFileWriter
    loader_interface.KGXFileWriter = MergingNodeWriter
    kgx_file_normalizer.KGXFileWriter = MergingNodeWriter
    try:
        yield
    finally:
        loader_interface.KGXFileWriter = original_loader
        kgx_file_normalizer.KGXFileWriter = original_normalizer


def query(document, tokens):
    values = [document]
    for token in tokens:
        next_values = []
        for value in values:
            if token == "*" and isinstance(value, list):
                next_values.extend(value)
            elif isinstance(token, int) and isinstance(value, list) and token < len(value):
                next_values.append(value[token])
            elif isinstance(value, dict) and token in value:
                next_values.append(value[token])
        values = next_values
    return values


def raw_values(field, row):
    if "value" in field:
        value = field["value"]
    elif "tokens" in field:
        return query(row, field["tokens"])
    else:
        column = field["extract"].get("column")
        if column not in row:
            return []
        value = row[column]
    return value if isinstance(value, list) else [value]


def values(field, row, prefixes):
    output = []
    for raw in raw_values(field, row):
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, str) and field.get("list_format"):
            parsed = json.loads(raw) if field["list_format"] == "json" else ast.literal_eval(raw)
            if not isinstance(parsed, (list, tuple)):
                raise ValueError("Expected a list literal")
            items = list(parsed)
        elif isinstance(raw, str) and field.get("list_separator"):
            items = raw.split(field["list_separator"])
        else:
            items = [raw]
        for value in items:
            value = value.strip() if isinstance(value, str) else value
            if value is None or value in field.get("null_values", []):
                continue
            if "true_values" in field:
                value = value in field["true_values"]
            elif value == "":
                continue
            if field.get("item_pattern"):
                import re

                match = re.fullmatch(field["item_pattern"], str(value))
                if not match:
                    raise ValueError("Compound item does not match item_pattern")
                value = match.group(1)
            if "codes" in field:
                if str(value) not in field["codes"]:
                    raise ValueError(f"Unmapped code {value!r}")
                value = field["codes"][str(value)]
            prefix = field.get("id_prefix")
            if field.get("prefix_source"):
                candidates = values(field["prefix_source"], row, prefixes)
                if len(candidates) != 1:
                    raise ValueError("No unique prefix for this record")
                prefix = candidates[0]
            form = field.get("id_form")
            if prefix:
                value = str(value)
                if form != "native" and any(mark in value for mark in (",", ";", "|", "\t", "\n")):
                    raise ValueError("Compound identifier needs an explicit item or list mapping")
                if form == "mixed" and ":" in value:
                    pass
                elif not value.startswith(str(prefix) + ":"):
                    value = str(prefix) + ":" + value
            elif form == "obo":
                prefix, sep, local = str(value).partition("_")
                if not sep or prefix not in prefixes:
                    raise ValueError("OBO identifier has no declared prefix")
                value = prefix + ":" + local
            elif form == "url":
                matches = sorted(
                    ((uri, p) for p, uri in prefixes.items() if str(value).startswith(uri)),
                    key=lambda item: len(item[0]),
                    reverse=True,
                )
                if matches:
                    uri, prefix = matches[0]
                    value = prefix + ":" + str(value)[len(uri) :]
            elif field["slot"] == "id" and ":" not in str(value) and form != "native":
                raise ValueError("Bare identifier has no documented namespace")
            elif not prefix and not form and field["slot"] not in IDENTITY and "codes" not in field:
                types = field.get("types", [])
                if "sc:Integer" in types:
                    value = int(value)
                elif "sc:Float" in types or "sc:Number" in types:
                    value = float(value)
                elif "sc:Boolean" in types and "true_values" not in field:
                    if value in (True, "true", "True", "1"):
                        value = True
                    elif value in (False, "false", "False", "0"):
                        value = False
                    else:
                        raise ValueError("Unmapped boolean value")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("Non-finite numeric property")
            if isinstance(value, (dict, list)):
                raise ValueError("Nested property values need a reader adapter")
            output.append(value)
    return output


def open_text(path):
    opener = {".gz": gzip.open, ".bz2": bz2.open, ".xz": lzma.open}.get(path.suffix, open)
    return opener(path, "rt", encoding="utf-8", newline="")


class SemanticSourceLoader(SourceDataLoader):
    mapping_file: Path
    preserve_unconnected_nodes = True

    def __init__(self, test_mode=False, source_data_dir=None):
        super().__init__(test_mode=test_mode, source_data_dir=source_data_dir)
        self.mapping = json.loads(self.mapping_file.read_text())
        self.provenance_id = self.provenance_id or self.mapping.get("infores") or ""
        self._bindings = {}
        binding_path = os.environ.get("KGB_CIA_SOURCE_FILES")
        if binding_path:
            path = Path(binding_path).resolve()
            for file_id, locations in json.loads(path.read_text()).items():
                locations = locations if isinstance(locations, list) else [locations]
                self._bindings[file_id] = [
                    (path.parent / location).resolve() for location in locations
                ]
        self.data_files = [
            f["local_name"] for key, f in self.mapping["files"].items() if key not in self._bindings
        ]
        self.skips = list(self.mapping["skipped"])
        self._skip_keys = {(s["ref"], s["reason"]) for s in self.skips}
        self.mappings = {}
        self.diagnostic_count = 0

    def get_latest_source_version(self):
        return self.mapping["source_version"]

    def load(self, nodes_output_file_path, edges_output_file_path):
        with merging_node_writers():
            result = super().load(nodes_output_file_path, edges_output_file_path)
        result["node_merge_warnings"] = self.output_file_writer.merge_warnings
        return result

    def paths(self, file_id):
        if file_id in self._bindings:
            paths = self._bindings[file_id]
            if len({p.name for p in paths}) != len(paths):
                raise ValueError("FileSet members need distinct names for unambiguous lineage")
            return paths
        dist = self.mapping["files"][file_id]
        if "FileSet" in str(dist.get("@type")):
            raise ValueError(f"FileSet {file_id} requires explicit local member bindings")
        return [Path(self.data_path) / dist["local_name"]]

    def needs_data_download(self):
        return any(
            not path.is_file() for file_id in self.mapping["files"] for path in self.paths(file_id)
        )

    def _fetch(self, file_id, destination):
        dist = self.mapping["distributions"][file_id]
        url = str(dist.get("contentUrl") or "")
        if url.startswith(("https://", "http://", "ftp://")):
            temporary = destination.with_suffix(destination.suffix + ".part")
            urllib.request.urlretrieve(url, temporary)
            temporary.replace(destination)
        else:
            parent = dist.get("containedIn")
            parent = parent[0] if isinstance(parent, list) and len(parent) == 1 else parent
            parent_id = parent.get("@id") if isinstance(parent, dict) else parent
            if parent_id not in self.mapping["distributions"]:
                raise ValueError(f"No downloadable URL or archive parent for {file_id}: {url}")
            archive = Path(self.data_path) / (
                hashlib.sha256(str(parent_id).encode()).hexdigest()[:16] + ".archive"
            )
            if not archive.exists():
                self._fetch(parent_id, archive)
            member = dist.get("name")
            if zipfile.is_zipfile(archive):
                with (
                    zipfile.ZipFile(archive) as packed,
                    packed.open(member) as stream,
                    destination.open("wb") as target,
                ):
                    shutil.copyfileobj(stream, target)
            elif tarfile.is_tarfile(archive):
                with tarfile.open(archive) as packed:
                    stream = packed.extractfile(member)
                    if stream is None:
                        raise ValueError(f"Archive member is not a file: {member}")
                    with stream, destination.open("wb") as target:
                        shutil.copyfileobj(stream, target)
            else:
                raise ValueError(f"Unsupported archive parent for {file_id}")
        expected = dist.get("sha256")
        if expected:
            with destination.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != expected:
                raise ValueError(f"Checksum mismatch for {file_id}; refresh source metadata")

    def get_data(self):
        if not self.mapping["record_sets"]:
            raise ValueError("No compilable record sets; inspect generation_report.json")
        Path(self.data_path).mkdir(parents=True, exist_ok=True)
        for file_id in self.mapping["files"]:
            for path in self.paths(file_id):
                if path.exists():
                    expected = self.mapping["files"][file_id].get("sha256")
                    if expected and "FileSet" not in str(
                        self.mapping["files"][file_id].get("@type")
                    ):
                        with path.open("rb") as stream:
                            actual = hashlib.file_digest(stream, "sha256").hexdigest()
                        if actual != expected:
                            raise ValueError(
                                f"Checksum mismatch for {file_id}; refresh source metadata"
                            )
                    continue
                if file_id in self._bindings:
                    raise FileNotFoundError(path)
                self._fetch(file_id, path)
        return True

    def diagnostic(self, ref, reason, *, record=None):
        self.diagnostic_count += 1
        self._diagnostics.write(json.dumps({"ref": ref, "record": record, "reason": reason}) + "\n")
        key = (ref, reason)
        if key not in self._skip_keys:
            self.skips.append({"ref": ref, "name": None, "reason": reason})
            self._skip_keys.add(key)

    def rows(self, rs, path):
        if rs["reader"] in {"json", "jsonl"}:
            source_fields = {
                f["id"]: f
                for f in rs["row_fields"] + [f for e in rs["entities"] for f in e["fields"]]
                if f.get("file")
            }
            observed = set()
            with open_text(path) as stream:
                for line_number, document in self.json_documents(stream, rs):
                    for index, row in enumerate(query(document, rs["root"]), 1):
                        number = (
                            index
                            if line_number is None
                            else line_number
                            if not rs["root"]
                            else f"{line_number}:{index}"
                        )
                        if not isinstance(row, dict):
                            self.diagnostic(rs["id"], "JSON record is not an object", record=number)
                            continue
                        observed.update(
                            key for key, f in source_fields.items() if raw_values(f, row)
                        )
                        yield number, row
            for missing in sorted(source_fields.keys() - observed):
                self.diagnostic(
                    missing, "Declared JSON path absent from actual records", record="document"
                )
            return
        dist = self.mapping["files"][rs["file"]]
        dialect = dist.get("csvw:dialect", {})
        media = str(dist.get("encodingFormat", ""))
        delimiter = dialect.get("csvw:delimiter", "," if "text/csv" in media else "\t")
        if len(delimiter) != 1:
            raise ValueError("CSV delimiter must be a single character")
        comment = dialect.get("csvw:commentPrefix")
        header = dist.get("rcv:columnOrder", "header") == "header" and dialect.get(
            "csvw:header", True
        )
        names = None if header else dist.get("rcv:columnNames")
        if names is not None and not names:
            raise ValueError("Headerless source has no declared column order")
        all_fields = rs["row_fields"] + [f for e in rs["entities"] for f in e["fields"]]
        needed = {f["extract"]["column"]: f["id"] for f in all_fields if "column" in f["extract"]}
        checked = False
        with open_text(path) as stream:
            skip = dialect.get("csvw:skipRows", 0)
            lines = itertools.islice(stream, skip, None)
            quote = dialect.get("csvw:quoteChar", '"')
            reader = csv.reader(
                lines,
                delimiter=delimiter,
                quotechar=quote or None,
                quoting=csv.QUOTE_MINIMAL if quote else csv.QUOTE_NONE,
                doublequote=dialect.get("csvw:doubleQuote", True),
            )
            for cells in reader:
                number = reader.line_num + skip
                if not cells:
                    continue
                is_comment = bool(comment and cells[0].startswith(comment))
                if is_comment:
                    if header and dist.get("rcv:headerInComments"):
                        candidate = [cells[0].removeprefix(comment).strip(), *cells[1:]]
                        if set(needed) & set(candidate):
                            names = candidate
                    continue
                if names is None:
                    names = cells
                    continue
                if not checked:
                    for missing in sorted(set(needed) - set(names)):
                        self.diagnostic(
                            needed[missing],
                            "Declared column absent from actual header",
                            record="header",
                        )
                    checked = True
                if len(cells) != len(names):
                    self.diagnostic(
                        rs["id"], "Source row width differs from declared columns", record=number
                    )
                    continue
                yield number, dict(zip(names, cells, strict=True))

    def json_documents(self, stream, rs):
        if rs["reader"] == "json":
            yield None, json.load(stream)
            return
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                document = json.loads(line)
            except json.JSONDecodeError:
                self.diagnostic(rs["id"], "Malformed JSONL record", record=number)
                continue
            yield number, document

    def properties(self, fields, row, number):
        props = {}
        for field in fields:
            try:
                found = values(field, row, self.mapping["prefixes"])
            except (ValueError, TypeError, SyntaxError) as error:
                self.diagnostic(field["id"], str(error), record=number)
                continue
            if not found:
                continue
            slot = field["slot"]
            if slot in ENUMS:
                found = [str(v).removeprefix("biolink:") for v in found]
            multivalued = (
                slot in MANY
                or field.get("list_separator")
                or field.get("list_format")
                or field.get("is_array")
            )
            value = found if multivalued or len(found) > 1 else found[0]
            if slot in props and props[slot] != value:
                old = props[slot] if isinstance(props[slot], list) else [props[slot]]
                new = value if isinstance(value, list) else [value]
                value = list(dict.fromkeys(old + new))
            props[slot] = value
        return props

    def mapping_record(self, rs, name, produces, fields, **attributes):
        name_hash = hashlib.sha256(name.encode()).hexdigest()[:12]
        mapping_id = rs["mapping_prefix"] + "." + produces + "." + name_hash
        if mapping_id not in self.mappings:
            columns, constants = {}, {}
            for field in fields:
                if field.get("file"):
                    columns.setdefault(field["slot"], []).append(
                        ColumnRef(
                            field=field["id"],
                            name=str(field["extract"].get("column", field["name"])),
                        )
                    )
                    prefix_field = field.get("prefix_source")
                    if prefix_field and prefix_field.get("file"):
                        columns[field["slot"]].append(
                            ColumnRef(
                                field=prefix_field["id"],
                                name=str(
                                    prefix_field["extract"].get("column", prefix_field["name"])
                                ),
                            )
                        )
                elif isinstance(field.get("value"), (str, int, float, bool)):
                    constants[field["slot"]] = field["value"]
            self.mappings[mapping_id] = Mapping(
                id=mapping_id,
                file=rs["file"],
                record_set=rs["id"],
                plan_layout=rs.get("layout"),
                produces=produces,
                columns=columns,
                constants=constants,
                code=str(self.mapping_file.with_name("parser.py")),
                rows_read=0,
                records_written=0,
                conditions=[
                    "Records missing mapped IDs, categories, or required provenance are skipped"
                ],
                **attributes,
            )
        return self.mappings[mapping_id]

    def emit_nodes(self, rs, entity, row, number):
        mapping = self.mapping_record(
            rs,
            entity["name"],
            "node",
            entity["fields"],
            category=next(iter(entity["category"]), None),
        )
        mapping.rows_read += 1
        props = self.properties(entity["fields"], row, number)
        identifiers = props.pop("id", [])
        identifiers = identifiers if isinstance(identifiers, list) else [identifiers]
        categories = props.pop("category", entity["category"])
        categories = categories if isinstance(categories, list) else [categories]
        if not identifiers:
            self.diagnostic(rs["id"], f"Entity {entity['name']} has no identifier", record=number)
        result = []
        for identifier in identifiers:
            try:
                node = Node(id=str(identifier), category=categories, **props)
            except ValidationError as error:
                self.diagnostic(rs["id"], str(error), record=number)
                continue
            data = node.model_dump(exclude_none=True)
            node_id, node_types, name = data.pop("id"), data.pop("category"), data.pop("name", "")
            self.output_file_writer.write_node(
                node_id, node_name=name, node_types=node_types, node_properties=data, uniquify=False
            )
            result.append(node_id)
            mapping.records_written += 1
        return result

    def emit_edge(
        self,
        rs,
        name,
        subject_entity,
        object_entity,
        subjects,
        objects,
        props,
        predicate,
        key,
        number,
    ):
        fields = [
            {**f, "slot": role}
            for entity, role in ((subject_entity, "subject"), (object_entity, "object"))
            for f in entity["fields"]
            if f["slot"] == "id"
        ] + rs["row_fields"]
        mapping = self.mapping_record(
            rs,
            name,
            "edge",
            fields,
            predicate=predicate if isinstance(predicate, str) else None,
            subject_category=next(iter(subject_entity["category"]), None),
            object_category=next(iter(object_entity["category"]), None),
        )
        mapping.rows_read += 1
        if not subjects or not objects:
            self.diagnostic(rs["id"], "Relationship has a missing endpoint", record=number)
            return
        if (
            any(f["slot"] == "primary_knowledge_source" for f in rs["row_fields"])
            and "primary_knowledge_source" not in props
        ):
            self.diagnostic(
                rs["id"], "Mapped primary knowledge source is missing or unmapped", record=number
            )
            return
        if len(subjects) > 1 and len(objects) > 1:
            self.diagnostic(
                rs["id"], "Both endpoints are lists; pairing is not specified", record=number
            )
            return
        predicates = predicate if isinstance(predicate, list) else [predicate]
        for subject, obj, pred in itertools.product(subjects, objects, predicates):
            edge_props = dict(props)
            for slot in ("id", "name", "description", "category", "subject", "object", "predicate"):
                edge_props.pop(slot, None)
            edge_props.setdefault("primary_knowledge_source", self.provenance_id)
            edge_props.setdefault("knowledge_level", "not_provided")
            edge_props.setdefault("agent_type", "not_provided")
            edge_props["kgbcia_source_records"] = [mapping.id + "#" + key]
            try:
                edge = Edge(subject=subject, object=obj, predicate=pred, **edge_props)
            except ValidationError as error:
                self.diagnostic(rs["id"], str(error), record=number)
                continue
            data = edge.model_dump(exclude_none=True)
            subject_id, object_id, pred = (
                data.pop("subject"),
                data.pop("object"),
                data.pop("predicate"),
            )
            self.output_file_writer.write_edge(
                subject_id, object_id, predicate=pred, edge_properties=data
            )
            mapping.records_written += 1

    def parse_data(self):
        if not self.mapping["record_sets"]:
            raise ValueError(
                "This source has no compilable record sets; see generation_report.json"
            )
        if not self.provenance_id.startswith("infores:"):
            raise ValueError(
                "Supply an explicit source infores identity; the metadata does not identify it"
            )
        destination = Path(self.data_path).parent
        destination.mkdir(parents=True, exist_ok=True)
        self._diagnostics = (destination / "diagnostics.jsonl").open("w", encoding="utf-8")
        try:
            for rs in self.mapping["record_sets"]:
                for path in self.paths(rs["file"]):
                    for number, row in self.rows(rs, path):
                        node_ids = [
                            self.emit_nodes(rs, entity, row, number) for entity in rs["entities"]
                        ]
                        key_parts = [raw_values(f, row) for f in rs["key_fields"]]
                        key = (
                            json.dumps([part[0] for part in key_parts], separators=(",", ":"))
                            if key_parts
                            and all(len(p) == 1 and p[0] not in (None, "") for p in key_parts)
                            else str(number)
                        )
                        if len(self.paths(rs["file"])) > 1:
                            key = path.name + ":" + key
                        subjects = [
                            i
                            for i, e in enumerate(rs["entities"])
                            if e["role"] == "biolink:subject"
                        ]
                        objects = [
                            i for i, e in enumerate(rs["entities"]) if e["role"] == "biolink:object"
                        ]
                        if len(subjects) == len(objects) == 1:
                            s, o = subjects[0], objects[0]
                            props = self.properties(rs["row_fields"], row, number)
                            self.emit_edge(
                                rs,
                                "association",
                                rs["entities"][s],
                                rs["entities"][o],
                                node_ids[s],
                                node_ids[o],
                                props,
                                props.get("predicate"),
                                key,
                                number,
                            )
                        row_index = next(
                            (i for i, e in enumerate(rs["entities"]) if e["name"] == "row"), None
                        )
                        if row_index is not None:
                            provenance_fields = [
                                f
                                for f in rs["row_fields"]
                                if f["slot"]
                                in {
                                    "primary_knowledge_source",
                                    "aggregator_knowledge_source",
                                    "knowledge_level",
                                    "agent_type",
                                }
                            ]
                            props = self.properties(provenance_fields, row, number)
                            for i, entity in enumerate(rs["entities"]):
                                role = entity["role"] or ""
                                if i == row_index or role in {"biolink:subject", "biolink:object"}:
                                    continue
                                if not role.startswith("biolink:"):
                                    self.diagnostic(
                                        rs["id"],
                                        f"Unsupported relationship role {role}",
                                        record=number,
                                    )
                                    continue
                                self.emit_edge(
                                    rs,
                                    "to_" + entity["name"],
                                    rs["entities"][row_index],
                                    entity,
                                    node_ids[row_index],
                                    node_ids[i],
                                    props,
                                    role,
                                    key,
                                    number,
                                )
        finally:
            self._diagnostics.close()
            report = TransformReport(
                source=self.provenance_id,
                source_version=self.mapping["source_version"],
                croissant=self.mapping["croissant"],
                semantic_croissant=self.mapping["semantic_croissant"],
                mappings=list(self.mappings.values()),
                skipped=[Skipped(**item) for item in self.skips],
            )
            (destination / "transform_report.json").write_text(
                report.model_dump_json(indent=2), encoding="utf-8"
            )
        return {
            "diagnostic_count": self.diagnostic_count,
            "transform_report": str(destination / "transform_report.json"),
        }


def parse_main(loader_class, argv=None):
    parser = argparse.ArgumentParser(
        description="Run the generated native ORION loader (parsing only)"
    )
    parser.add_argument(
        "output", type=Path, help="Parsing workspace; no normalization is performed"
    )
    parser.add_argument(
        "--source-files", type=Path, help="JSON map of metadata file IDs to local paths"
    )
    parser.add_argument("--infores", help="Source identity when not present in the metadata")
    args = parser.parse_args(argv)
    if args.source_files:
        os.environ["KGB_CIA_SOURCE_FILES"] = str(args.source_files.resolve())
    args.output.mkdir(parents=True, exist_ok=True)
    loader = loader_class(source_data_dir=str(args.output.resolve()))
    if args.infores:
        loader.provenance_id = args.infores
    try:
        if not loader.provenance_id.startswith("infores:"):
            raise ValueError("Supply --infores before downloading source data")
        loader.get_data()
        result = loader.load(str(args.output / "nodes.jsonl"), str(args.output / "edges.jsonl"))
    except Exception as error:
        print(f"ORION parsing failed: {error}")
        return 1
    print(json.dumps(result, indent=2))
    return 0
