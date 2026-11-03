"""Compile semantic Croissant bundles into native ORION SourceDataLoader packages."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import shutil
from pathlib import Path

ORION_VERSION = "2.0.13"
ORION_REVISION = "df1e985afea18c45c5cc64b58fcfdce2ae1894e5"
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[4]


def read_json(path: Path) -> dict:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        result = json.load(stream)
    if isinstance(result, list) and len(result) == 1:
        result = result[0]
    if not isinstance(result, dict):
        raise ValueError(f"Expected an object in {path}")
    return result


def dump(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def as_list(value):
    return value if isinstance(value, list) else ([] if value is None else [value])


def text(value):
    return (
        str(value.get("en", next(iter(value.values()), "")))
        if isinstance(value, dict)
        else str(value or "")
    )


def slug(value):
    return re.sub(r"[^a-zA-Z0-9_]+", "_", value).strip("_").lower()


def leaves(fields):
    for field in fields:
        if field.get("subField"):
            yield from leaves(field["subField"])
        else:
            yield field


def field_source(field):
    source = field.get("source") or {}
    for kind in ("fileObject", "fileSet"):
        ref = source.get(kind)
        if isinstance(ref, dict) and ref.get("@id"):
            return ref["@id"], source.get("extract") or {}
    return None, source.get("extract") or {}


def json_tokens(path):
    """The supported, unambiguous JSONPath subset; reject expressions we cannot evaluate."""
    if not isinstance(path, str) or not path.startswith("$"):
        raise ValueError(f"Invalid JSONPath: {path!r}")
    tokens, offset = [], 1
    pattern = re.compile(r"\.([^.[\]]+)|\[(\*|\d+)\]|\['([^']+)'\]|\[\"([^\"]+)\"\]")
    while offset < len(path):
        match = pattern.match(path, offset)
        if not match:
            raise ValueError(f"Unsupported JSONPath expression: {path}")
        if match[2] is not None:
            tokens.append("*" if match[2] == "*" else int(match[2]))
        else:
            tokens.append(match[1] or match[3] or match[4])
        offset = match.end()
    return tokens


def compile_bundle(path: Path, *, infores: str | None = None) -> dict:
    document = read_json(path)
    plan_path = path.parent / "semantics_plan.json"
    plan = read_json(plan_path) if plan_path.exists() else {}
    # Several bundles share the same plan.kb (for example the DrugCentral exports).
    # Their bundle directories are distinct sources and must not overwrite one another.
    source = slug(path.parent.name)
    if not source:
        raise ValueError("Cannot determine source package name")
    context = document.get("@context") or {}
    prefixes = dict(plan.get("prefixes") or {})
    if isinstance(context, dict):
        prefixes.update(
            {k: v for k, v in context.items() if isinstance(v, str) and v.startswith("http")}
        )
    expansions = {}
    for prefix, uri in prefixes.items():
        expansions.setdefault(uri, []).append(prefix)
    record_sets = document.get("recordSet") or []
    enumerations = {}
    for rs in record_sets:
        if "sc:Enumeration" in as_list(rs.get("dataType")):
            fields = rs.get("field") or []
            if len(fields) == 2:
                key, value = fields[0]["@id"], fields[1]["@id"]
                enumerations[key] = {
                    str(row[key]): row[value]
                    for row in rs.get("data", [])
                    if key in row and value in row
                }
    distributions = {item["@id"]: item for item in document.get("distribution", [])}
    skips = []

    def skip(ref, name, reason):
        skips.append({"ref": str(ref), "name": text(name), "reason": reason})

    def codes(field):
        reference = ((field.get("references") or {}).get("field") or {}).get("@id")
        if reference and reference not in enumerations:
            raise ValueError(f"Missing enumeration {reference}")
        return enumerations.get(reference)

    def column(field):
        file_id, extract = field_source(field)
        spec = {
            "id": field["@id"],
            "name": text(field.get("name")),
            "slot": str(field.get("equivalentProperty") or "").removeprefix("biolink:"),
            "types": as_list(field.get("dataType")),
            "file": file_id,
            "extract": extract,
            "is_array": bool(field.get("isArray")),
        }
        if "value" in field:
            spec["value"] = field["value"]
        if file_id:
            dialect = distributions.get(file_id, {}).get("csvw:dialect") or {}
            if "csvw:null" in dialect:
                spec["null_values"] = as_list(dialect["csvw:null"])
        if field.get("source", {}).get("transform"):
            raise ValueError("Croissant source transforms need an explicit reader adapter")
        if file_id and not extract:
            raise ValueError("Field has no extraction path")
        if extract and set(extract) - {"column", "jsonPath"}:
            raise ValueError(f"Unsupported extraction: {extract}")
        if "jsonPath" in extract:
            spec["tokens"] = json_tokens(extract["jsonPath"])
        if not file_id and "value" not in field:
            raise ValueError("Field has neither a source nor a constant")
        if codes(field) is not None:
            spec["codes"] = codes(field)
        for annotation in field.get("annotation", []):
            name = annotation.get("name", "")
            if "value" in annotation:
                if name.startswith("null_value_"):
                    spec.setdefault("null_values", []).append(annotation["value"])
                elif name.startswith("true_value_"):
                    spec.setdefault("true_values", []).append(annotation["value"])
                else:
                    spec[name] = annotation["value"]
            elif name == "id_prefix":
                spec["prefix_source"] = column(annotation)
                if spec["prefix_source"].get("codes"):
                    mapped = {}
                    for key, uri in spec["prefix_source"]["codes"].items():
                        candidates = expansions.get(uri, [])
                        if len(candidates) != 1:
                            raise ValueError(f"Namespace {uri} has no unique declared CURIE prefix")
                        mapped[key] = candidates[0]
                    spec["prefix_source"]["codes"] = mapped
        return spec

    def fields(specs):
        result = []
        for field in specs:
            prop = field.get("equivalentProperty", "")
            if not prop:
                if field_source(field)[0]:
                    skip(field["@id"], field.get("name"), "No semantic property mapping")
                continue
            if not prop.startswith("biolink:"):
                skip(field["@id"], field.get("name"), f"Non-Biolink property {prop}")
                continue
            try:
                result.append(column(field))
            except ValueError as error:
                skip(field["@id"], field.get("name"), str(error))
        return result

    def entity(name, types, columns, role=None):
        categories = [t for t in as_list(types) if str(t).startswith("biolink:")]
        ids = [c for c in columns if c["slot"] == "id"]
        if len(ids) != 1 or (not categories and not any(c["slot"] == "category" for c in columns)):
            return None
        return {"name": name, "category": categories, "fields": columns, "role": role}

    compiled = []
    for rs in record_sets:
        if "sc:Enumeration" in as_list(rs.get("dataType")) or "data" in rs:
            continue
        rs_id, rs_name = rs["@id"], text(rs.get("name"))
        rs_fields = rs.get("field") or []
        file_ids = sorted({field_source(f)[0] for f in leaves(rs_fields) if field_source(f)[0]})
        if len(file_ids) != 1 or file_ids[0] not in distributions:
            skip(
                rs_id,
                rs_name,
                "Requires one explicit source file; joins and missing files are unsupported",
            )
            continue
        file_id, dist = file_ids[0], distributions[file_ids[0]]
        raw_fields = list(leaves(rs_fields))
        extracts = [field_source(f)[1] for f in raw_fields if field_source(f)[0]]
        media = " ".join(as_list(dist.get("encodingFormat"))).lower()
        has_json = extracts and all("jsonPath" in ex for ex in extracts)
        if has_json or "application/json" in media or "ndjson" in media:
            reader = "jsonl" if "ndjson" in media else "json"
        elif (dist.get("csvw:dialect") or "text/csv" in media or "tab-separated" in media) and all(
            "column" in ex for ex in extracts
        ):
            reader = "table"
        else:
            skip(rs_id, rs_name, "No supported table/JSON reader or missing extraction paths")
            continue
        dialect_name = dist.get("rcv:dialect", "")
        if dialect_name.startswith(("vcf", "gmt", "obo", "gaf", "gpad", "gpi", "mitab")):
            skip(rs_id, rs_name, f"Specialized dialect {dialect_name} needs a reader adapter")
            continue
        if (
            reader == "table"
            and dist.get("rcv:columnOrder") == "field_order"
            and not dist.get("rcv:columnNames")
        ):
            skip(rs_id, rs_name, "Headerless file lacks explicit column order")
            continue
        row_fields, entities = [], []
        for f in rs_fields:
            if f.get("subField"):
                ent = entity(
                    text(f.get("name")),
                    f.get("dataType"),
                    fields(f["subField"]),
                    f.get("equivalentProperty"),
                )
                if ent:
                    entities.append(ent)
                else:
                    skip(
                        f["@id"],
                        f.get("name"),
                        "Entity lacks exactly one mapped ID or a Biolink category",
                    )
            else:
                specs = fields([f])
                if specs:
                    spec = specs[0]
                    types = [t for t in spec["types"] if str(t).startswith("biolink:")]
                    role = f.get("equivalentProperty")
                    if types and spec["slot"] not in {
                        "id",
                        "category",
                        "name",
                        "description",
                        "symbol",
                        "xref",
                        "publications",
                        "primary_knowledge_source",
                        "aggregator_knowledge_source",
                        "supporting_data_source",
                        "provided_by",
                        "knowledge_level",
                        "agent_type",
                    }:
                        spec["slot"] = "id"
                        entities.append(entity(spec["name"], types, [spec], role))
                    else:
                        row_fields.append(spec)
        row = entity("row", rs.get("dataType"), row_fields)
        if row:
            entities.insert(0, row)
        if not entities:
            skip(rs_id, rs_name, "No identified, typed entities can be emitted")
            continue
        if row is None and not (
            {"biolink:subject", "biolink:object"} <= {e["role"] for e in entities}
        ):
            # Standalone groups may still produce nodes; unsupported relationships stay reported.
            skip(
                rs_id,
                rs_name,
                "No complete subject/object association; emit supported entities only",
            )
        all_columns = row_fields + [c for e in entities for c in e["fields"]]
        paths = [c["tokens"] for c in all_columns if "tokens" in c]
        common = list(paths[0]) if paths else []
        for tokens in paths[1:]:
            length = next(
                (
                    i
                    for i, pair in enumerate(zip(common, tokens, strict=False))
                    if pair[0] != pair[1]
                ),
                min(len(common), len(tokens)),
            )
            common = common[:length]
        root = common[: len(common) - common[::-1].index("*")] if "*" in common else []
        if reader in {"json", "jsonl"} and any("*" in p for p in paths) and not root:
            skip(
                rs_id,
                rs_name,
                "JSON paths have no shared record-array root; alignment is ambiguous",
            )
            continue
        key_ids = [k.get("@id") if isinstance(k, dict) else k for k in as_list(rs.get("key"))]
        key_fields = []
        for f in raw_fields:
            if f.get("@id") in key_ids and field_source(f)[1]:
                try:
                    key_fields.append(column(f))
                except ValueError as error:
                    skip(f["@id"], f.get("name"), str(error))
        seen = set()

        def relative(c, seen=seen, root=root):
            if id(c) in seen:
                return
            seen.add(id(c))
            if "tokens" in c:
                c["tokens"] = c["tokens"][len(root) :]
            if c.get("prefix_source"):
                relative(c["prefix_source"])

        for c in all_columns + key_fields:
            relative(c)
        digest = hashlib.sha256(rs_id.encode()).hexdigest()[:12]
        compiled.append(
            {
                "id": rs_id,
                "name": rs_name,
                "layout": rs.get("rcv:planLayout"),
                "file": file_id,
                "reader": reader,
                "root": root,
                "row_fields": row_fields,
                "entities": entities,
                "key_fields": key_fields,
                "mapping_prefix": f"{source}.{digest}",
            }
        )
    if infores is not None and not re.fullmatch(r"infores:[^\s]+", infores):
        raise ValueError("Source identity must be a valid infores CURIE")
    if infores is None:
        for slot in ("provided_by", "aggregator_knowledge_source", "primary_knowledge_source"):
            values = {
                c["value"]
                for rs in compiled
                for c in rs["row_fields"]
                if c["slot"] == slot
                and isinstance(c.get("value"), str)
                and c["value"].startswith("infores:")
            }
            if len(values) == 1:
                infores = values.pop()
                break
    used_files = {rs["file"] for rs in compiled}
    files = {}
    for file_id in sorted(used_files):
        item = dict(distributions[file_id])
        filename = Path(text(item.get("name")) or file_id).name
        item["local_name"] = hashlib.sha256(file_id.encode()).hexdigest()[:12] + "_" + filename
        files[file_id] = item
    seen_record_sets = {r["@id"] for r in record_sets if "data" not in r}
    return {
        "format_version": 2,
        "source": source,
        "source_id": "Kgbcia_" + source,
        "infores": infores,
        "source_version": str(
            document.get("version") or hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        ),
        "name": text(document.get("name")),
        "description": text(document.get("description")),
        "url": document.get("url"),
        "license": document.get("license"),
        "semantic_croissant": str(path.resolve()),
        "croissant": plan.get("croissant", str(path.resolve())),
        "biolink_version": plan.get("biolink_version"),
        "prefixes": prefixes,
        "record_sets": compiled,
        "files": files,
        "distributions": distributions,
        "skipped": skips,
        "record_sets_seen": len(seen_record_sets),
        "record_sets_unsupported": len(seen_record_sets - {r["id"] for r in compiled}),
        "metadata_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "schema_sha256": hashlib.sha256((REPO_ROOT / "shared/schema.py").read_bytes()).hexdigest(),
        "runtime_sha256": hashlib.sha256(
            (SCRIPT_DIR / "orion_runtime.py").read_bytes()
        ).hexdigest(),
        "requires_infores": infores is None,
        "orion_version": ORION_VERSION,
        "orion_revision": ORION_REVISION,
    }


def generate(path: Path, output: Path, *, infores=None) -> dict:
    config = compile_bundle(path, infores=infores)
    root = output.resolve()
    target = root / "parsers" / config["source_id"] / "src"
    target.mkdir(parents=True, exist_ok=True)
    for directory in (root / "parsers", target.parent, target):
        (directory / "__init__.py").touch()
    support_hash = hashlib.sha256(
        (config["runtime_sha256"] + config["schema_sha256"]).encode()
    ).hexdigest()[:16]
    support_name = "_kgbcia_support_" + support_hash
    support = root / support_name
    support.mkdir(exist_ok=True)
    (support / "__init__.py").touch()
    shutil.copyfile(SCRIPT_DIR / "orion_runtime.py", support / "orion_runtime.py")
    shutil.copyfile(REPO_ROOT / "shared/schema.py", support / "schema.py")
    shutil.copyfile(SCRIPT_DIR / "run_orion.py", root / "run_orion.py")
    dump(target / "semantic_mapping.json", config)
    version = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]
    source = f'''# /// script
# requires-python = ">=3.12"
# dependencies = ["robokop-orion=={ORION_VERSION}"]
# ///
"""Generated ORION loader. Mapping metadata SHA256: {config["metadata_sha256"]}."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from {support_name}.orion_runtime import SemanticSourceLoader, merging_node_writers


class GeneratedLoader(SemanticSourceLoader):
    source_id = {config["source_id"]!r}
    provenance_id = {config["infores"] or ""!r}
    parsing_version = {version!r}
    mapping_file = Path(__file__).with_name("semantic_mapping.json")
    preserve_unconnected_nodes = True


if __name__ == "__main__":
    from {support_name}.orion_runtime import parse_main
    raise SystemExit(parse_main(GeneratedLoader))
'''
    (target / "parser.py").write_text(source, encoding="utf-8")
    dump(
        target / f"{config['source_id']}.source.json",
        {
            "@context": "https://schema.org",
            "@type": "Dataset",
            "name": config["name"],
            "identifier": config["infores"],
            "description": config["description"],
            "url": config["url"],
            "license": config["license"],
        },
    )
    result = {
        "source": config["source"],
        "source_id": config["source_id"],
        "parser": str((target / "parser.py").relative_to(root)),
        "class": "GeneratedLoader",
        "record_sets_compiled": len(config["record_sets"]),
        "record_sets_unsupported": config["record_sets_unsupported"],
        "record_sets_seen": config["record_sets_seen"],
        "status": "unsupported"
        if not config["record_sets"]
        else "needs_infores"
        if config["requires_infores"]
        else "partial"
        if config["skipped"]
        else "ready",
        "requires_infores": config["requires_infores"],
        "infores": config["infores"],
        "skipped": config["skipped"],
    }
    dump(target / "generation_report.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input", type=Path, help="Semantic Croissant file, source bundle, or bundle collection"
    )
    parser.add_argument("output", type=Path, help="Generated ORION package directory under data/")
    parser.add_argument("--infores", help="Explicit source identity for a single bundle")
    args = parser.parse_args(argv)
    if args.input.is_file():
        inputs = [args.input]
    else:
        inputs = sorted(args.input.rglob("croissant.semantic.json*"))
    if not inputs or (args.infores and len(inputs) != 1):
        parser.error("Supply at least one semantic Croissant; --infores requires one bundle")
    results, errors, seen = [], [], set()
    for path in inputs:
        try:
            config = compile_bundle(path, infores=args.infores)
            if config["source_id"] in seen:
                raise ValueError(f"Duplicate source id {config['source_id']}")
            seen.add(config["source_id"])
            results.append(generate(path, args.output, infores=args.infores))
        except (ValueError, OSError, KeyError, TypeError) as error:
            errors.append({"input": str(path), "error": str(error)})
    registry_path = args.output / "registry.json"
    registry = read_json(registry_path) if registry_path.exists() else {}
    registry.update({r["source_id"]: {"parser": r["parser"], "class": r["class"]} for r in results})
    dump(registry_path, registry)
    dump(args.output / "generation_report.json", {"sources": results, "errors": errors})
    print(
        f"Generated {len(results)} ORION loaders; {len(errors)} source errors. "
        "See generation_report.json."
    )
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
