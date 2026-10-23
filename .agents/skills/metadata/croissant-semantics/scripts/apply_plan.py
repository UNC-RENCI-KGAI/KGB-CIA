#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Write a semantics plan into a croissant, producing a new croissant.

    apply_plan.py CROISSANT PLAN OUT        # OUT ending in .gz is gzipped

The input croissant is not modified. See reference/plan-format.md for the plan format and
reference/croissant-constructs.md for what each plan entry turns into.
"""

import argparse
import copy
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import dialects  # noqa: E402
from common import (
    as_list,
    column_names,
    distribution_index,
    field_source,
    layout_id,
    load_croissant,
    record_set_files,
    save_croissant,
    text,
)

# Vocabularies the conventions rely on. Plan prefixes are added alongside these.
VOCAB = {
    "biolink": "https://w3id.org/biolink/vocab/",
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "sh": "http://www.w3.org/ns/shacl#",
    "qudt": "http://qudt.org/schema/qudt/",
    "unit": "http://qudt.org/vocab/unit/",
    "rcv": "https://roboboulangerie.github.io/vocab/",
    "csvw": "http://www.w3.org/ns/csvw#",
}
# Croissant 1.1 terms our older croissants' @context lacks. mlcroissant reads `value` as
# schema.org/value.
CONTEXT_TERMS = {"annotation": "cr:annotation", "value": "sc:value"}
ATOMIC = {
    "sc:Text",
    "sc:Integer",
    "sc:Float",
    "sc:Boolean",
    "sc:Date",
    "sc:DateTime",
    "sc:Time",
    "sc:URL",
    "sc:Number",
}
ASSERTION_KEYS = {"basis", "source", "confidence"}


class PlanError(Exception):
    pass


WARNINGS: set[str] = set()
PLAN_PREFIXES: dict[str, str] = {}  # prefix -> expansion, for the plan being applied
KNOWN = {
    "plan": {
        "kb",
        "croissant",
        "biolink_version",
        "prefixes",
        "local_terms",
        "column_library",
        "layouts",
        "skipped",
        "notes",
        "explained",
    },
    "layout": {
        "id",
        "match",
        "summary",
        "dialect",
        "row",
        "entities",
        "columns",
        "constants",
        "description",
    },
    "entity": {
        "name",
        "type",
        "role",
        "description",
        "columns",
        "constants",
        "basis",
        "source",
        "confidence",
        "optional",
        "no_id",
    },
    "column": {
        "type",
        "property",
        "id_prefix",
        "id_form",
        "prefix_column",
        "list_separator",
        "list_format",
        "unit",
        "codes",
        "null_values",
        "true_values",
        "atomic",
        "pattern_note",
        "description",
        "basis",
        "source",
        "confidence",
        "use",
        "optional",
        "derived",
        "prefix_codes",
    },
}
RENAMED = {"skipped_layouts": "skipped (a list of {what, reason})"}


def check_keys(plan: dict) -> None:
    def look(obj, kind, where):
        for k in obj:
            if k in RENAMED:
                WARNINGS.add(f"{where}: {k!r} was renamed to {RENAMED[k]}")
            elif k not in KNOWN[kind]:
                WARNINGS.add(f"{where}: unknown key {k!r} (ignored)")

    look(plan, "plan", "plan")
    for lay in plan.get("layouts") or []:
        look(lay, "layout", f"layout {lay.get('id')}")
        for ent in lay.get("entities") or []:
            look(ent, "entity", f"layout {lay.get('id')} entity {ent.get('name')}")
            for c, sp in (ent.get("columns") or {}).items():
                look(sp, "column", f"layout {lay.get('id')} {ent.get('name')}.{c}")
        for c, sp in (lay.get("columns") or {}).items():
            look(sp, "column", f"layout {lay.get('id')} column {c}")


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", s).strip("_") or "x"


def annotation(field_id: str, name: str, prop: str, value) -> dict:
    return {
        "@type": "cr:Field",
        "@id": f"{field_id}/{name}",
        "name": name,
        "dataType": "sc:Text",
        "equivalentProperty": prop,
        "value": value,
    }


def merge_types(existing, added) -> list:
    types = [t for t in as_list(existing)]
    if not any(t in ATOMIC for t in types):
        types.insert(0, "sc:Text")  # Croissant: a field with several types needs an atomic one
    for t in as_list(added):
        if t not in types:
            types.append(t)
    return types


def apply_column(
    field: dict, spec: dict, enums: dict, layout_key: str, column: str, by_name: dict
) -> None:
    fid = field["@id"]
    if spec.get("atomic"):
        if spec["atomic"] not in ATOMIC:
            raise PlanError(
                f"column {column}: atomic {spec['atomic']!r} isn't one of {sorted(ATOMIC)}"
            )
        field["dataType"] = [spec["atomic"]] + [
            t for t in as_list(field.get("dataType")) if t not in ATOMIC
        ]
    if spec.get("type"):
        field["dataType"] = merge_types(field.get("dataType"), spec["type"])
    if spec.get("property"):
        field["equivalentProperty"] = spec["property"]
    if spec.get(
        "description"
    ):  # the plan's note on the column (how a list is joined, ...) adds to the pipeline's
        existing = text(field.get("description")).rstrip()
        field["description"] = (existing.rstrip(".") + ". " if existing else "") + spec[
            "description"
        ]
    notes = []
    form = spec.get("id_form", "bare" if spec.get("id_prefix") else None)
    if spec.get("id_prefix") and form == "bare":
        notes.append(annotation(fid, "id_prefix", "sh:prefix", spec["id_prefix"]))
    if spec.get("prefix_column"):
        src = by_name.get(spec["prefix_column"])
        if src is None or "source" not in src:
            raise PlanError(
                f"column {column}: prefix_column {spec['prefix_column']!r} isn't a column of the "
                "record set"
            )
        # the prefix differs by row: an annotation that reads it from the other column
        note = {
            "@type": "cr:Field",
            "@id": f"{fid}/id_prefix",
            "name": "id_prefix",
            "dataType": "sc:Text",
            "equivalentProperty": "sh:prefix",
            "source": copy.deepcopy(src["source"]),
        }
        if spec.get("prefix_codes"):
            # the other column holds namespace keys ("protein_xref_omim"), not prefixes: a code
            # table maps each key
            # to its namespace (the prefix's expansion)
            missing = [v for v in spec["prefix_codes"].values() if v not in PLAN_PREFIXES]
            if missing:
                raise PlanError(
                    f"column {column}: prefix_codes uses prefixes not in the plan's prefixes: "
                    f"{missing}"
                )
            mapping = {k: PLAN_PREFIXES[v] for k, v in spec["prefix_codes"].items()}
            note["references"] = {
                "field": {
                    "@id": register_codes(enums, mapping, spec["prefix_column"], layout_key)
                    + "/name"
                }
            }
        notes.append(note)
    if form in ("url", "mixed", "native", "obo"):
        # full IRIs, a mix of CURIEs and bare ids, the source's own tagged ids (MITAB "db:id",
        # HGVS), or OBO short
        # forms (MONDO_0004989: prefix, underscore, local id)
        notes.append(annotation(fid, "id_form", "rcv:idForm", form))
    if spec.get("unit"):
        notes.append(annotation(fid, "unit", "qudt:hasUnit", spec["unit"]))
    for i, null in enumerate(as_list(spec.get("null_values"))):
        notes.append(annotation(fid, f"null_value_{i}", "rcv:nullValue", null))
    for i, val in enumerate(as_list(spec.get("true_values"))):
        notes.append(annotation(fid, f"true_value_{i}", "rcv:trueValue", val))
    if spec.get("list_separator"):
        notes.append(annotation(fid, "list_separator", "rcv:listSeparator", spec["list_separator"]))
    if spec.get("list_format"):
        if spec["list_format"] not in ("json", "python"):
            raise PlanError(f"list_format is json or python, not {spec['list_format']!r}")
        notes.append(annotation(fid, "list_format", "rcv:listFormat", spec["list_format"]))
    if notes:
        field["annotation"] = [
            a for a in as_list(field.get("annotation")) if a["@id"] not in {n["@id"] for n in notes}
        ] + notes
    if spec.get("codes"):
        field["references"] = {
            "field": {"@id": f"{register_codes(enums, spec['codes'], column, layout_key)}/name"}
        }


def register_codes(enums: dict, codes: dict, column: str, layout_key: str) -> str:
    """The code table for this mapping: one per distinct mapping, shared by every column in any
    layout using it."""
    digest = hashlib.sha1(json.dumps(codes, sort_keys=True).encode()).hexdigest()[:8]
    enum_id = f"codes_{digest}"
    info = enums.setdefault(enum_id, {"columns": [], "codes": codes, "layouts": []})
    if column not in info["columns"]:
        info["columns"].append(column)
    if layout_key not in info["layouts"]:
        info["layouts"].append(layout_key)
    return enum_id


def enumeration_record_set(enum_id: str, info: dict) -> dict:
    return {
        "@type": "cr:RecordSet",
        "@id": enum_id,
        "name": f"Codes in {', '.join(info['columns'])}",
        "description": (
            f"What each value of {', '.join(info['columns'])} means (plan layouts "
            f"{', '.join(info['layouts'])})."
        ),
        "dataType": "sc:Enumeration",
        "key": {"@id": f"{enum_id}/name"},
        "field": [
            {"@type": "cr:Field", "@id": f"{enum_id}/name", "name": "name", "dataType": "sc:Text"},
            {"@type": "cr:Field", "@id": f"{enum_id}/url", "name": "url", "dataType": "sc:URL"},
        ],
        "data": [
            {f"{enum_id}/name": code, f"{enum_id}/url": term}
            for code, term in info["codes"].items()
        ],
    }


def local_terms_record_set(terms: list[dict]) -> dict:
    rid = "rc_local_terms"
    cols = [
        ("id", "sc:URL", None),
        ("label", "sc:Text", "sc:name"),
        ("definition", "sc:Text", "sc:description"),
        ("exact_match", "sc:Text", "skos:exactMatch"),
        ("close_match", "sc:Text", "skos:closeMatch"),
        ("broad_match", "sc:Text", "skos:broadMatch"),
        ("narrow_match", "sc:Text", "skos:narrowMatch"),
        ("see_also", "sc:URL", "rdfs:seeAlso"),
    ]
    fields = []
    for name, dt, prop in cols:
        f = {"@type": "cr:Field", "@id": f"{rid}/{name}", "name": name, "dataType": dt}
        if prop:
            f["equivalentProperty"] = prop
        fields.append(f)
    rows = []
    for t in terms:
        row = {}
        for name, _, _ in cols:
            v = t.get(name if name != "see_also" else "see")
            # every row needs every column; several terms are space-separated CURIEs
            row[f"{rid}/{name}"] = " ".join(as_list(v)) if v not in (None, "", []) else ""
        rows.append(row)
    return {
        "@type": "cr:RecordSet",
        "@id": rid,
        "name": "Terms defined for this knowledge base",
        "description": (
            "Terms with no equivalent in an established vocabulary, linked to the nearest ones "
            "with SKOS."
        ),
        "dataType": "sc:DefinedTerm",
        "key": {"@id": f"{rid}/id"},
        "field": fields,
        "data": rows,
    }


_WARNED_UNUSED: set[str] = set()


def match_record_sets(croissant: dict, plan: dict) -> dict[str, dict]:
    """record set @id -> the plan layout that applies to it (most specific match wins)."""
    matches: dict[str, list[tuple[tuple[int, int], dict]]] = {}
    for lay in plan.get("layouts") or []:
        m = lay.get("match") or {}
        if not m:
            raise PlanError(f"layout {lay.get('id')}: needs a match")
        if not (
            m.get("layout") or m.get("layouts") or m.get("has_columns") or m.get("record_sets")
        ):
            raise PlanError(
                f"layout {lay.get('id')}: match needs layout, layouts, has_columns or record_sets"
            )
        hits = 0
        for rs in croissant.get("recordSet") or []:
            if m.get("layout") and layout_id(column_names(rs)) != m["layout"]:
                continue
            if m.get("layouts") and layout_id(column_names(rs)) not in as_list(m["layouts"]):
                continue
            if m.get("has_columns") and not set(m["has_columns"]) <= set(column_names(rs)):
                continue
            if (
                m.get("record_sets")
                and rs["@id"] not in m["record_sets"]
                and text(rs.get("name")) not in m["record_sets"]
            ):
                continue
            if m.get("record_set_name") and not re.search(
                m["record_set_name"], text(rs.get("name"))
            ):
                continue
            # then, among has_columns matches, the one requiring more columns (layered has_columns
            # layouts)
            specificity = (
                bool(m.get("record_sets")) * 4
                + bool(m.get("record_set_name")) * 2
                + bool(m.get("layout") or m.get("layouts")),
                len(as_list(m.get("has_columns"))),
            )
            matches.setdefault(rs["@id"], []).append((specificity, lay))
            hits += 1
        if not hits:
            raise PlanError(f"layout {lay.get('id')}: matches no record set ({json.dumps(m)})")
    # decided over all matches, so a tie between two layouts that a third outranks isn't an error
    chosen = {}
    for rs_id, cands in matches.items():
        best = max(sp for sp, _ in cands)
        top = [lay for sp, lay in cands if sp == best]
        if len(top) > 1:
            raise PlanError(
                f"record set {rs_id} matches layouts {', '.join(lay['id'] for lay in top)} equally"
            )
        chosen[rs_id] = top[0]
    used = {lay["id"] for lay in chosen.values()}
    for lay in plan.get("layouts") or []:
        if lay["id"] not in used and lay["id"] not in _WARNED_UNUSED:
            _WARNED_UNUSED.add(lay["id"])
            print(
                f"[apply_plan] warning: layout {lay['id']} applies to no record set: every record "
                "set it matches is matched more specifically by another layout",
                file=sys.stderr,
            )
    return chosen


def resolve_constant(const: dict, rs: dict, layout_key: str) -> dict:
    """A constant whose value comes from the record set's name: name_pattern with a value template
    ({1}) or a value_map from the first group ({"human": "NCBITaxon:9606"}). Other constants are
    returned as they are."""
    if not const.get("name_pattern"):
        return const
    m = re.search(const["name_pattern"], text(rs.get("name")))
    if not m:
        raise PlanError(
            f"layout {layout_key}: constant {const['name']}: name_pattern "
            f"{const['name_pattern']!r} "
            f"doesn't match record set name {text(rs.get('name'))!r}"
        )
    if const.get("value_map"):
        key = m.group(1) if m.groups() else m.group(0)
        if key not in const["value_map"]:
            raise PlanError(
                f"layout {layout_key}: constant {const['name']}: value_map has no entry for "
                f"{key!r} "
                f"(record set {text(rs.get('name'))!r})"
            )
        return {**const, "value": const["value_map"][key]}
    return {**const, "value": const["value"].format(m.group(0), *m.groups())}


def apply_layout(rs: dict, lay: dict, enums: dict) -> None:
    by_name = {text(f.get("name")): f for f in rs.get("field") or []}
    # columns marked optional are used where the record set has them and skipped where it doesn't
    lay = copy.deepcopy(lay)
    lay["columns"] = {
        c: sp
        for c, sp in (lay.get("columns") or {}).items()
        if c in by_name or not sp.get("optional")
    }
    for ent in lay.get("entities") or []:  # an optional entity's columns are all optional
        ent["columns"] = {
            c: sp
            for c, sp in (ent.get("columns") or {}).items()
            if c in by_name or not (sp.get("optional") or ent.get("optional"))
        }
    lay["entities"] = [
        e for e in lay.get("entities") or [] if e["columns"] or not e.get("optional")
    ]
    wanted = list(lay.get("columns") or {}) + [
        c for e in lay.get("entities") or [] for c in e.get("columns") or {}
    ]
    missing = [c for c in wanted if c not in by_name]
    if missing:
        raise PlanError(f"layout {lay['id']}: record set {rs['@id']} has no column(s) {missing}")
    dup = {c for c in wanted if wanted.count(c) > 1}
    if dup:
        raise PlanError(f"layout {lay['id']}: column(s) {sorted(dup)} used more than once")

    row = lay.get("row") or {}
    if row.get("type"):
        rs["dataType"] = list(dict.fromkeys(as_list(rs.get("dataType")) + as_list(row["type"])))
    constants = list(lay.get("constants") or [])
    if row.get("predicate"):
        constants.insert(
            0, {"name": "predicate", "property": "biolink:predicate", "value": row["predicate"]}
        )
    for const in constants:
        const = resolve_constant(const, rs, lay["id"])
        cid = f"{rs['@id']}/{slug(const['name'])}"
        rs["field"] = [f for f in rs["field"] if f["@id"] != cid] + [
            {
                "@type": "cr:Field",
                "@id": cid,
                "name": const["name"],
                "dataType": as_list(const.get("type")) or ["sc:Text"],
                "equivalentProperty": const["property"],
                "value": const["value"],
            }
        ]
    for column, spec in (lay.get("columns") or {}).items():
        apply_column(by_name[column], spec, enums, lay["id"], column, by_name)

    fields = rs["field"]
    for ent in lay.get("entities") or []:
        members = [by_name[c] for c in ent.get("columns") or {}]
        if not members:
            raise PlanError(f"layout {lay['id']}: entity {ent.get('name')} has no columns")
        if any("jsonPath" in ((m.get("source") or {}).get("extract") or {}) for m in members):
            # mlcroissant 1.1.1 can't read sub-fields with JSON-path sources: type the columns one
            # by one
            WARNINGS.add(
                f"layout {lay['id']}: entity {ent['name']} reads JSON paths, which mlcroissant "
                "can't "
                "group; its columns were typed individually instead"
            )  # a set: once per layout and entity
            # flattened onto the row, the entity's own properties would describe the row (a taxon's
            # id would become
            # the protein's id): its id column takes the entity's role, and its other properties are
            # dropped
            for column, spec in ent["columns"].items():
                spec = dict(spec)
                if spec.get("property") in ("biolink:id", "sc:identifier"):
                    if ent.get("type") and "type" not in spec:
                        spec["type"] = ent["type"]
                    spec["property"] = ent.get("role")
                elif spec.get("property"):
                    WARNINGS.add(
                        f"layout {lay['id']}: entity {ent['name']}: {column}'s property "
                        f"{spec['property']} "
                        "was dropped, because without grouping it would describe the row"
                    )
                    spec["property"] = None
                apply_column(
                    by_name[column],
                    {k: v for k, v in spec.items() if v is not None},
                    enums,
                    lay["id"],
                    column,
                    by_name,
                )
            continue
        for column, spec in ent["columns"].items():
            spec = dict(spec)
            if (
                spec.get("property") in ("biolink:id", "sc:identifier")
                and ent.get("type")
                and "type" not in spec
            ):
                spec["type"] = ent["type"]  # the id column holds identifiers of the entity's type
            apply_column(by_name[column], spec, enums, lay["id"], column, by_name)
        gid = f"{rs['@id']}/{slug(ent['name'])}"
        group = {
            "@type": "cr:Field",
            "@id": gid,
            "name": ent["name"],
            "description": ent.get("description") or f"{ent['name']}: {', '.join(ent['columns'])}",
            "dataType": as_list(ent.get("type")) or ["sc:Thing"],
            "subField": list(members),
        }
        if ent.get("role"):
            group["equivalentProperty"] = ent["role"]
        for const in ent.get("constants") or []:
            const = resolve_constant(const, rs, lay["id"])
            group["subField"].append(
                {
                    "@type": "cr:Field",
                    "@id": f"{gid}/{slug(const['name'])}",
                    "name": const["name"],
                    "dataType": as_list(const.get("type")) or ["sc:Text"],
                    "equivalentProperty": const["property"],
                    "value": const["value"],
                }
            )
        first = min(fields.index(m) for m in members)
        member_ids = {m["@id"] for m in members}
        fields[:] = [f for f in fields if f["@id"] not in member_ids]
        fields.insert(min(first, len(fields)), group)

    # derived fields: a second reading of a column, with its own property (a negation flag from a
    # predicate column)
    specs = list((lay.get("columns") or {}).items()) + [
        (c, sp) for e in lay.get("entities") or [] for c, sp in (e.get("columns") or {}).items()
    ]
    for column, spec in specs:
        for d in spec.get("derived") or []:
            if not d.get("name") or not d.get("property"):
                raise PlanError(
                    f"layout {lay['id']}: column {column}: a derived field needs a name and a "
                    "property"
                )
            did = f"{rs['@id']}/{slug(d['name'])}"
            if any(f["@id"] == did for f in fields) or d["name"] in by_name:
                raise PlanError(
                    f"layout {lay['id']}: derived field {d['name']!r} clashes with an existing "
                    "field"
                )
            new = {
                "@type": "cr:Field",
                "@id": did,
                "name": d["name"],
                "description": d.get("description") or f"Derived from {column}.",
                "dataType": [d.get("atomic") or "sc:Text"],
                "source": copy.deepcopy(by_name[column]["source"]),
            }
            fields.append(new)
            apply_column(
                new,
                {k: v for k, v in d.items() if k not in ("name", "atomic", "description")},
                enums,
                lay["id"],
                d["name"],
                by_name,
            )


def apply_dialects(croissant: dict, assignments: dict[str, dict]) -> int:
    """Describe how to read each file used by a layout with a dialect: csvw:dialect,
    rcv:columnOrder, rcv:dialect, the column names in file order for files without a header row, and
    an encodingFormat list (content type, compression, spec). Must run before columns are grouped.
    Returns the number of files."""
    dists = distribution_index(croissant)
    given: dict[
        str, tuple[str, dict, list[str]]
    ] = {}  # file id -> (layout id, dialect, columns in file order)
    for rs in croissant.get("recordSet") or []:
        lay = assignments.get(rs["@id"])
        if not lay or not lay.get("dialect"):
            continue
        for file_id in record_set_files(rs):
            try:
                dialect = dialects.for_file(lay["dialect"], dists[file_id])
            except KeyError as e:
                raise PlanError(f"layout {lay['id']}: {e.args[0]}") from e
            if dialect is None:
                raise PlanError(
                    f"layout {lay['id']}: dialect.by_suffix has no entry for "
                    f"{dialects.file_name(dists[file_id])}"
                )
            columns = [
                text(ex.get("column")) or text(f.get("name"))
                for f in rs.get("field") or []
                for _, d, ex in [field_source(f)]
                if d == file_id
            ]
            prev = given.get(file_id)
            if prev and prev[1] != dialect:
                raise PlanError(
                    f"file {file_id} is read by layouts {prev[0]} and {lay['id']} with different "
                    "dialects"
                )
            if prev and dialect["columns"] == "field_order" and prev[2] != columns:
                raise PlanError(
                    f"file {file_id} has no header row, and record sets give its columns in "
                    "different orders"
                )
            given[file_id] = (lay["id"], dialect, columns)
    read_by_lines = {
        d
        for rs in croissant.get("recordSet") or []
        for f in rs.get("field") or []
        for _, d, ex in [field_source(f)]
        if ex.get("fileProperty") == "lines"
    }
    for file_id, (lay_id, dialect, columns) in given.items():
        dist = dists[file_id]
        if file_id in read_by_lines:
            # fields that read raw lines need the file's own format; a table type would make
            # mlcroissant parse it as a table and the line fields would find nothing
            print(
                f"[apply_plan] warning: layout {lay_id}: {dist.get('name') or file_id} is read as "
                "raw lines (fileProperty: lines); its encodingFormat is left as it is, and the "
                "dialect only describes the file",
                file=sys.stderr,
            )
        else:
            dist["encodingFormat"] = dialects.encoding_formats(dist, dialect)
        dist.update(dialects.to_croissant(dialect, columns))
    return len(given)


# Croissant terms whose @context definition matters to mlcroissant: inline record-set data must be
# JSON literals,
# or the rows of code tables and local terms are read as graph nodes and the validator rejects them
# `value` too: constants are written with it, and mlcroissant reads sc:value; a croissant mapping
# it to cr:value (FlyBase, Dark Kinase Knowledgebase) made every constant load as None
CONTEXT_REQUIRED = {"data": {"@id": "cr:data", "@type": "@json"}, "value": "sc:value"}


def update_context(croissant: dict, plan: dict) -> None:
    ctx = croissant.setdefault("@context", {})
    for key, definition in CONTEXT_REQUIRED.items():
        if key in ctx and ctx[key] != definition:
            print(
                f"[apply_plan] warning: @context defined {key!r} as {ctx[key]!r}; replaced with "
                f"{definition!r}, which the semantics need (a generator problem to report)",
                file=sys.stderr,
            )
        ctx[key] = definition
    wanted = {**CONTEXT_TERMS, **VOCAB, **(plan.get("prefixes") or {})}
    if "rdfs" not in ctx:
        wanted["rdfs"] = "http://www.w3.org/2000/01/rdf-schema#"
    for key, iri in wanted.items():
        if key in ctx and ctx[key] != iri:
            if key in CONTEXT_TERMS:
                continue  # an existing definition of a Croissant term wins
            if str(ctx[key]).startswith(("cr:", "sc:", "dct:")) or isinstance(ctx[key], dict):
                raise PlanError(
                    f"prefix {key}: the data's ids use {key}: as a prefix, but {key} is a "
                    f"Croissant term in @context ({ctx[key]!r}), so it can't also be declared as "
                    f"one. Leave it out of prefixes, mark the id columns id_form native, and say "
                    f"in pattern_note what {key}: ids are"
                )
            raise PlanError(f"prefix {key} is already in @context as {ctx[key]!r}, not {iri!r}")
        ctx[key] = iri


def duplicate_ids(croissant: dict) -> set[str]:
    """@ids used more than once (record sets, fields, sub-fields, annotations, distributions)."""
    seen: dict[str, int] = {}

    def walk(fields):
        for fl in fields:
            seen[fl["@id"]] = seen.get(fl["@id"], 0) + 1
            walk(fl.get("subField") or [])
            walk(fl.get("annotation") or [])

    for rs in croissant.get("recordSet") or []:
        seen[rs["@id"]] = seen.get(rs["@id"], 0) + 1
        walk(rs.get("field") or [])
    for d in croissant.get("distribution") or []:
        if "@id" in d:
            seen[d["@id"]] = seen.get(d["@id"], 0) + 1
    return {i for i, n in seen.items() if n > 1}


def check_unique_ids(out: dict, before: set[str]) -> None:
    """An @id clash the plan made (an entity named like an existing column) breaks mlcroissant with
    an unrelated-looking error, so it's refused; clashes already in the input are reported."""
    dups = duplicate_ids(out)
    new = sorted(dups - before)
    if new:
        raise PlanError(
            f"{len(new)} @id(s) are used more than once, e.g. {new[:5]}. An entity, constant or "
            "derived field probably has the same name as an existing column; rename it in the plan."
        )
    if before:
        WARNINGS.add(
            f"the input croissant already uses {len(before)} @id(s) more than once, e.g. "
            f"{sorted(before)[:3]} "
            "(a pipeline problem)"
        )


def ensure_field_ids(croissant: dict) -> int:
    """Give every field (and sub-field) that lacks an @id one built from its record set and name,
    cleaned so the validator accepts it (names like ' #skip' have spaces and marks). Returns how
    many were added."""
    added = 0
    for rs in croissant.get("recordSet") or []:
        if "@id" not in rs:
            raise PlanError(f"record set {text(rs.get('name'))!r} has no @id")
        taken = {f["@id"] for f in rs.get("field") or [] if "@id" in f}

        def fill(fields, parent, taken=taken):
            nonlocal added
            for fl in fields:
                if "@id" not in fl:
                    base = f"{parent}/{slug(text(fl.get('name'))) or 'field'}"
                    fid, n = base, 2
                    while fid in taken:
                        fid, n = f"{base}_{n}", n + 1
                    fl["@id"] = fid
                    taken.add(fid)
                    added += 1
                fill(fl.get("subField") or [], fl["@id"], taken)

        fill(rs.get("field") or [], rs["@id"])
    return added


ANSWERS_FILE = "semantics_answers.json"  # default location: next to the input croissant


def answers_path(croissant_path: str, given: str | None) -> Path | None:
    """The maintainer answers file: --answers, else semantics_answers.json next to the input
    croissant if present."""
    if given:
        return Path(given)
    default = Path(croissant_path).parent / ANSWERS_FILE
    return default if default.exists() else None


def merge_answers(plan: dict, croissant: dict, answers: list[dict]) -> tuple[dict, list[str]]:
    """Fold maintainer answers into the plan; they override the agent's choices. Each answer
    applying to a column ({"column": NAME, optionally "layout": PLAN_LAYOUT_ID}) merges its "spec"
    into that column's spec wherever the plan types the column (adding it, marked optional, to
    layouts whose record sets have it), or with "skip" removes the column and lists it in skipped.
    Returns the new plan and a line per answer saying what happened."""
    plan = copy.deepcopy(plan)
    report = []
    matched = match_record_sets(croissant, plan) if plan.get("layouts") else {}
    has_column: dict[str, set[str]] = {}
    for rs in croissant.get("recordSet") or []:
        lay = matched.get(rs["@id"])
        if lay:
            has_column.setdefault(lay["id"], set()).update(column_names(rs))
    for n, a in enumerate(answers, 1):
        who, when, question = a.get("by", "?"), a.get("date", "?"), str(a.get("question", ""))[:80]
        label = f"answer {n} ({who}, {when}): {question}"
        target = a.get("applies_to") or {}
        col = target.get("column")
        if not col:
            report.append(
                f"{label}: "
                + ("prefixes added" if a.get("prefixes") else "recorded (no column to apply it to)")
            )
            continue
        who, when = a.get("by", "maintainer"), a.get("date", "undated")
        provenance = f"Maintainer: {a.get('answer', '')} ({who}, {when})"
        if a.get(
            "prefixes"
        ):  # prefixes the answer establishes (what an unknown CURIE prefix means)
            plan.setdefault("prefixes", {}).update(a["prefixes"])
        if a.get(
            "local_term"
        ):  # a term the answer defines (or redefines), e.g. a local property it names
            terms = plan.setdefault("local_terms", [])
            existing = next((t for t in terms if t.get("id") == a["local_term"].get("id")), None)
            if existing:
                existing.update(a["local_term"])
            else:
                terms.append(dict(a["local_term"]))
        hits = 0
        for lay in plan.get("layouts") or []:
            if target.get("layout") and lay.get("id") != target["layout"]:
                continue
            places = [lay.setdefault("columns", {})] + [
                e.get("columns") or {} for e in lay.get("entities") or []
            ]
            spec_home = next((cols for cols in places if col in cols), None)
            if spec_home is None and col not in has_column.get(lay.get("id"), set()):
                continue
            if a.get("skip"):
                if spec_home is not None:
                    del spec_home[col]
                plan.setdefault("skipped", []).append(
                    {
                        "what": f"{lay.get('id')} column {col}",
                        "reason": f"{a['skip']} ({provenance})",
                    }
                )
                hits += 1
                continue
            if spec_home is None:
                spec_home = lay["columns"]
                spec_home[col] = {"optional": True}
            spec = spec_home[col]
            spec.update(a.get("spec") or {})
            spec["source"], spec["confidence"] = "maintainer", "high"
            spec["basis"] = provenance
            hits += 1
        report.append(
            f"{label}: applied to {hits} layout(s)"
            if hits
            else f"{label}: matched no layout with column {col}"
        )
    return plan, report


INHERITED = ("list_separator", "list_format", "null_values")  # derived fields take these


def resolve_plan(plan: dict) -> dict:
    """Expand references to the plan's column_library: a column spec {"use": NAME, ...overrides}."""
    library = plan.get("column_library") or {}

    def resolve(spec):
        if not isinstance(spec, dict) or "use" not in spec:
            return spec
        if spec["use"] not in library:
            raise PlanError(f"column spec uses {spec['use']!r}, which isn't in column_library")
        merged = {**library[spec["use"]], **{k: v for k, v in spec.items() if k != "use"}}
        return {
            k: v for k, v in merged.items() if v is not None
        }  # an override of null removes a key

    def inherit(spec):
        """A derived field reads the same cells, so it splits and skips them the same way unless
        it says otherwise."""
        if isinstance(spec, dict) and spec.get("derived"):
            shared = {k: spec[k] for k in INHERITED if k in spec}
            spec["derived"] = [{**shared, **d} for d in spec["derived"]]
        return spec

    plan = copy.deepcopy(plan)
    for lay in plan.get("layouts") or []:
        lay["columns"] = {
            c: inherit(resolve(sp)) for c, sp in (lay.get("columns") or {}).items()
        }  # fmt: skip
        for ent in lay.get("entities") or []:
            ent["columns"] = {
                c: inherit(resolve(sp)) for c, sp in (ent.get("columns") or {}).items()
            }  # fmt: skip
    return plan


def apply_plan(croissant: dict, plan: dict) -> tuple[dict, dict]:
    WARNINGS.clear()
    PLAN_PREFIXES.clear()
    PLAN_PREFIXES.update({**VOCAB, **(plan.get("prefixes") or {})})
    check_keys(plan)
    plan = resolve_plan(plan)
    out = copy.deepcopy(croissant)
    ids_added = ensure_field_ids(out)
    dups_before = duplicate_ids(out)
    if ids_added:
        WARNINGS.add(
            f"{ids_added} field(s) in the input croissant had no @id; ids were generated from "
            "record set "
            "and field names (a pipeline problem worth reporting)"
        )
    update_context(out, plan)
    assignments = match_record_sets(out, plan)
    enums: dict = {}
    counts = {
        "record_sets": 0,
        "layouts": len(plan.get("layouts") or []),
        "codes_tables": 0,
        "local_terms": 0,
        "dialect_files": apply_dialects(out, assignments),
    }
    for rs in out.get("recordSet") or []:
        lay = assignments.get(rs["@id"])
        if lay:
            apply_layout(rs, lay, enums)
            counts["record_sets"] += 1
    for enum_id, info in enums.items():
        out["recordSet"].append(enumeration_record_set(enum_id, info))
    counts["codes_tables"] = len(enums)
    if plan.get("local_terms"):
        out["recordSet"].append(local_terms_record_set(plan["local_terms"]))
        counts["local_terms"] = len(plan["local_terms"])
    check_unique_ids(out, dups_before)
    return out, counts


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("croissant")
    ap.add_argument("plan")
    ap.add_argument("out")
    ap.add_argument(
        "--answers",
        help=f"maintainer answers (default: {ANSWERS_FILE} next to the croissant, if any)",
    )
    args = ap.parse_args()
    plan = json.loads(Path(args.plan).read_text())
    croissant = load_croissant(args.croissant)
    try:
        if (path := answers_path(args.croissant, args.answers)) is not None:
            plan, lines = merge_answers(
                resolve_plan(plan), croissant, json.loads(path.read_text()).get("answers") or []
            )
            print(f"Maintainer answers from {path}:", file=sys.stderr)
            for ln in lines:
                print(f"  {ln}", file=sys.stderr)
        out, counts = apply_plan(croissant, plan)
    except PlanError as e:
        sys.exit(f"Plan error: {e}")
    for w in sorted(WARNINGS):
        print(f"Warning: {w}", file=sys.stderr)
    save_croissant(out, args.out)
    print(
        f"Wrote {args.out}: {counts['layouts']} plan layouts applied to {counts['record_sets']} "
        "record sets, "
        f"{counts['codes_tables']} code tables, {counts['local_terms']} local terms, "
        f"dialects on {counts['dialect_files']} files."
    )


if __name__ == "__main__":
    main()
