#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "pyyaml", "bioregistry", "jsonpath-ng", "openpyxl", "truststore"]
# ///
"""Check a semantics plan and the croissant it produced, and report coverage.

    check.py ORIGINAL PLAN OUT [--report report.md] [--plan-only] [--no-data] [--no-validate]
             [--load-records]

Checks, in order:
  1. Plan: every term has a declared prefix; Biolink terms exist (classes for types, slots for
     properties); association types are associations; id prefixes suit the entity type; every
     assertion says its source and confidence; named dialects exist; QUDT units exist.
     --plan-only stops here (seconds).
  2. Data (unless --no-data): reads a sample of each layout (with the layout's dialect, if any)
     and checks that the dialect reads the files into the croissant's columns, id columns
     against their declared prefix (Bioregistry pattern for bare ids, the prefix itself for
     CURIEs) and that code columns only hold codes the plan maps.
  3. Croissant: mlcroissant validate on OUT (unless --no-validate). With --load-records, reads a
     few records of one record set per layout through mlcroissant. It doesn't download whole
     files: it takes the first complete lines (about 2 MB) of each file and points a small copy
     of the croissant at them. JSON and archived files must be read whole, so only those under
     20 MB are used. If OUT fails, ORIGINAL is tried, so failures that already existed (formats
     mlcroissant can't read, like VCF or files with commented headers) aren't blamed on the plan.
  4. Coverage: share of record sets and fields with semantics, by kind of vocabulary.

Exit status is 1 if there are errors (warnings don't fail)."""

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import dialects  # noqa: E402
from apply_plan import (
    ATOMIC,
    CONTEXT_TERMS,
    VOCAB,
    PlanError,
    answers_path,
    match_record_sets,
    merge_answers,
    resolve_plan,
)
from common import (
    CURIE,
    as_list,
    column_names,
    content_size,
    distribution_index,
    field_source,
    layout_id,
    load_croissant,
    record_set_files,
    text,
)

# Biolink slots whose definition asks for text although their range is a class (see
# reference/vocabulary.md)
TEXT_DESPITE_RANGE = {"authors"}
ID_FORMS = ("bare", "curie", "obo", "mixed", "url", "native")
# OBO short form: MONDO_0004989, NCBITaxon_9606, OBA_VT0000047, PR_P12345 (not UniProt entry names
# like BTK_HUMAN)
OBO_ID = re.compile(
    r"^(?!(?:NC|NG|NM|NP|NR|NT|NW|NZ|XM|XP|XR|WP|AC|AP|YP)_)([A-Za-z][A-Za-z.]*)_([A-Z]{0,3}\d[\w.-]*)$"
)
# Biolink slots whose values are text, where 'tRNA:Pro-AGG-1-1' is a symbol, not a CURIE
# predicates whose Biolink domain contradicts their definition (see reference/vocabulary.md): not
# checked
DOMAIN_QUIRKS = {"participates_in"}
TEXT_SLOTS = {"symbol", "name", "synonym", "description", "full_name", "has_biological_sequence"}
BUILTIN = {"sc", "cr", "dct", "rai", "rdfs", "wd", "wdt"} | set(VOCAB)
CONFIDENCE = {"high", "medium", "low"}
SOURCES = {"agent", "data_dictionary", "documentation", "maintainer"}


class Findings:
    def __init__(self, explained: list[dict] | None = None):
        self.items: list[tuple[str, str, str]] = []
        # the plan's "explained": warnings checked by hand, given as {"where": ..., "message": ...,
        # "reason": ...}
        # (substrings); a matching warning becomes a note carrying the reason
        self.explained = explained or []
        self.used: set[int] = set()

    def error(self, where, msg):
        self.items.append(("error", where, msg))

    def warn(self, where, msg):
        for i, e in enumerate(self.explained):
            if (
                str(e.get("where", "")) in str(where)
                and str(e.get("message", "")) in str(msg)
                and e.get("reason")
            ):
                self.used.add(i)
                self.items.append(("note", where, f"{msg} (explained in the plan: {e['reason']})"))
                return
        self.items.append(("warning", where, msg))

    def note(self, where, msg):
        self.items.append(("note", where, msg))

    def count(self, level):
        return sum(1 for i in self.items if i[0] == level)


def iter_assertions(lay: dict):
    """(where, spec, kind) for every assertion in a plan layout."""
    if lay.get("row"):
        yield f"{lay['id']} row", lay["row"], "row"
    for c in lay.get("constants") or []:
        yield f"{lay['id']} constant {c.get('name')}", c, "constant"
    for ent in lay.get("entities") or []:
        yield f"{lay['id']} entity {ent.get('name')}", ent, "entity"
        for col, spec in (ent.get("columns") or {}).items():
            yield f"{lay['id']} {ent.get('name')}.{col}", spec, "member"
            for d in spec.get("derived") or []:
                yield f"{lay['id']} {col} -> {d.get('name')}", d, "column"
    for col, spec in (lay.get("columns") or {}).items():
        yield f"{lay['id']} column {col}", spec, "column"
        for d in spec.get("derived") or []:
            yield f"{lay['id']} {col} -> {d.get('name')}", d, "column"


def terms_in(spec: dict, kind: str) -> list[tuple[str, str]]:
    """(term, role) pairs: role is 'class' or 'slot' or 'value'."""
    out = [(t, "class") for t in as_list(spec.get("type"))]
    if spec.get("predicate"):
        out.append((spec["predicate"], "slot"))
    for key in ("property", "role"):
        if spec.get(key):
            out.append((spec[key], "slot"))
    for c in spec.get("constants") or []:
        out.append((c["property"], "slot"))
        if isinstance(c.get("value"), str) and CURIE.match(c["value"]):
            out.append((c["value"], "value"))
    if kind == "constant" and isinstance(spec.get("value"), str) and CURIE.match(spec["value"]):
        out.append((spec["value"], "value"))
    for v in (spec.get("codes") or {}).values():
        out.append((v, "value"))
    return out


def check_plan(plan: dict, f: Findings, model: dict | None) -> None:
    declared = BUILTIN | set(plan.get("prefixes") or {})
    local_ids = {t.get("id") for t in plan.get("local_terms") or []}
    classes = {
        re.sub(r"\s", "", k).lower(): (k, v) for k, v in (model or {}).get("classes", {}).items()
    }
    slots = {k.replace(" ", "_"): v for k, v in (model or {}).get("slots", {}).items()}

    def biolink_ancestors(key):
        out = []
        while key:
            out.append(key)
            key = (model["classes"].get(key) or {}).get("is_a")
        return out

    for prefix, iri in (plan.get("prefixes") or {}).items():
        if not str(iri).endswith(("/", "#", "_", ":", "=")):
            f.error(
                f"prefix {prefix}",
                f"expansion {iri!r} doesn't end in / # _ : or =, so CURIEs would expand to "
                "malformed IRIs; add the delimiter",
            )
    unlinked = [
        t.get("id")
        for t in plan.get("local_terms") or []
        if not any(t.get(k) for k in ("exact_match", "close_match", "broad_match", "narrow_match"))
        and not t.get("no_match_reason")
    ]
    if unlinked:
        f.warn(
            "local terms",
            f"{len(unlinked)} link to no established term and give no no_match_reason: "
            f"{', '.join(map(str, unlinked[:12]))}",
        )
    for t in plan.get("local_terms") or []:
        if not t.get("definition"):
            f.warn(f"local term {t.get('id')}", "has no definition")
    ids = [lay.get("id") for lay in plan.get("layouts") or []]
    for dup in {i for i in ids if ids.count(i) > 1}:
        f.error("plan", f"layout id {dup} is used more than once")
    for lay in plan.get("layouts") or []:
        if lay.get("dialect"):
            try:
                dialects.check_spec(lay["dialect"])
            except KeyError as e:
                f.error(f"layout {lay.get('id')}", f"dialect: {e.args[0]}")
        for where, spec, kind in iter_assertions(lay):
            if kind in ("row", "entity", "column"):
                if spec.get("source") not in SOURCES:
                    f.warn(where, f"source should be one of {sorted(SOURCES)}")
                if spec.get("confidence") not in CONFIDENCE:
                    f.warn(where, f"confidence should be one of {sorted(CONFIDENCE)}")
            for term, role in terms_in(spec, kind):
                m = CURIE.match(str(term))
                if str(term).startswith(("http://", "https://")):
                    continue
                if not m:
                    f.error(where, f"{term!r} is not a CURIE or URL")
                    continue
                prefix, local = m.groups()
                if prefix not in declared:
                    f.error(
                        where, f"prefix {prefix} (in {term}) is not declared in the plan's prefixes"
                    )
                if prefix == "rcv" and term not in local_ids and role != "value":
                    f.warn(where, f"{term} is a local term with no entry in local_terms")
                if prefix == "biolink" and model:
                    if role == "class":
                        key = local.lower()
                        if key not in classes:
                            f.error(where, f"{term} is not a Biolink class")
                        elif kind == "entity" and "association" in biolink_ancestors(
                            classes[key][0]
                        ):
                            f.error(where, f"{term} is an association; an entity should be a thing")
                    elif role == "slot" and local not in slots:
                        f.error(where, f"{term} is not a Biolink slot")
            if spec.get("id_prefix") and spec["id_prefix"] not in declared:
                f.error(
                    where, f"id prefix {spec['id_prefix']} is not declared in the plan's prefixes"
                )
            undeclared = [v for v in (spec.get("prefix_codes") or {}).values() if v not in declared]
            if undeclared:
                f.error(
                    where,
                    f"prefix_codes uses prefixes not declared in the plan's prefixes: {undeclared}",
                )
            if spec.get("prefix_codes") and not spec.get("prefix_column"):
                f.error(where, "prefix_codes needs prefix_column (the column holding the keys)")
            if spec.get("id_form", "bare") not in ID_FORMS:
                f.error(where, f"id_form must be one of {', '.join(ID_FORMS)}")
        # rows that are things: their own id column, checked like an entity's
        row_thing = {
            "name": "row",
            "type": (lay.get("row") or {}).get("type"),
            "columns": {
                c: sp
                for c, sp in (lay.get("columns") or {}).items()
                if sp.get("property") == "biolink:id"
            },
        }
        for ent in (lay.get("entities") or []) + ([row_thing] if row_thing["columns"] else []):
            etype = next((t for t in as_list(ent.get("type")) if t.startswith("biolink:")), None)
            if not (model and etype and etype[8:].lower() in classes):
                continue
            if ent is row_thing and "association" in biolink_ancestors(
                classes[etype[8:].lower()][0]
            ):
                continue
            allowed = []
            for key in biolink_ancestors(classes[etype[8:].lower()][0]):
                allowed += model["classes"].get(key, {}).get("id_prefixes") or []
            for col, spec in (ent.get("columns") or {}).items():
                p = spec.get("id_prefix")
                if spec.get("property") not in ("biolink:id", "sc:identifier"):
                    continue  # xrefs etc. may use any prefix
                if p and allowed and p not in allowed:
                    f.warn(
                        f"{lay['id']} {ent['name']}.{col}",
                        f"Biolink doesn't list {p} as an id prefix for {etype} (it lists "
                        f"{', '.join(allowed[:8])})",
                    )
            if ent.get("no_id"):
                # the plan says the entity has no id column (genes known only by symbol), and why
                continue
            if not any(
                s.get("property") in ("biolink:id", "sc:identifier")
                for s in (ent.get("columns") or {}).values()
            ):
                f.warn(
                    f"{lay['id']} entity {ent['name']}",
                    "no column is marked as the entity's id (property biolink:id)",
                )


def class_lineage(model: dict, key: str) -> list[str]:
    """A Biolink class and everything it is: is_a ancestors, mixins and their parents (closest
    first)."""
    out, todo = [], [key]
    while todo:
        k = todo.pop(0)
        if k in out or k not in model["classes"]:
            continue
        out.append(k)
        c = model["classes"][k]
        todo += ([c["is_a"]] if c.get("is_a") else []) + list(c.get("mixins") or [])
    return out


def _class_key(model: dict, curie: str) -> str | None:
    want = curie.removeprefix("biolink:").lower()
    return next((k for k in model["classes"] if k.replace(" ", "").lower() == want), None)


def check_roles(plan: dict, f: "Findings", model: dict | None) -> None:
    """Entities in the subject/object role must be of the classes the association's slot_usage
    allows."""
    if not model:
        return
    import vocab

    for lay in plan.get("layouts") or []:
        row_type = next(
            (t for t in as_list((lay.get("row") or {}).get("type")) if t.startswith("biolink:")),
            None,
        )
        assoc = _class_key(model, row_type) if row_type else None
        if not assoc or "association" not in class_lineage(model, assoc):
            continue
        for ent in lay.get("entities") or []:
            role = str(ent.get("role") or "").removeprefix("biolink:")
            if role not in ("subject", "object"):
                continue
            rng = vocab._slot_range(model, assoc, role)
            etypes = [t for t in as_list(ent.get("type")) if t.startswith("biolink:")]
            for t in etypes:
                key = _class_key(model, t)
                if rng and key and rng not in class_lineage(model, key):
                    f.warn(
                        f"{lay['id']} entity {ent.get('name')}",
                        f"{row_type} wants a {rng} as {role} (its slot_usage), but the entity is "
                        f"{t}; pick an "
                        f"association whose {role} fits (vocab.py associations) or say why in "
                        "basis",
                    )
        check_predicate_fit(lay, f, model)


def _slot_constraint(model: dict, slot: str, which: str) -> str | None:
    """A Biolink slot's domain or range, inherited along is_a."""
    key = slot.removeprefix("biolink:").replace("_", " ")
    seen = set()
    while key and key not in seen:
        seen.add(key)
        s_ = model["slots"].get(key) or {}
        if s_.get(which):
            return s_[which]
        key = s_.get("is_a")
    return None


def check_predicate_fit(lay: dict, f: "Findings", model: dict) -> None:
    """A predicate's domain and range must fit the subject's and object's classes
    (ameliorates_condition is for chemicals and treatments, not genes)."""
    predicates = [str((lay.get("row") or {}).get("predicate") or "")]
    for _, spec, _ in iter_assertions(lay):
        if spec.get("property") == "biolink:predicate":
            predicates += [str(v) for v in (spec.get("codes") or {}).values()]
    import vocab

    row_type = next(
        (t for t in as_list((lay.get("row") or {}).get("type")) if t.startswith("biolink:")), None
    )
    assoc = _class_key(model, row_type) if row_type else None
    roles = {}
    for ent in lay.get("entities") or []:
        role = str(ent.get("role") or "").removeprefix("biolink:")
        if role in ("subject", "object"):
            declared = vocab._slot_range(model, assoc, role) if assoc else None
            # a class the association itself declares for the role (a mixin) is Biolink's own
            # pairing: not checked
            roles[role] = [
                k
                for t in as_list(ent.get("type"))
                if (k := _class_key(model, str(t))) and k != declared
            ]
    for pred in dict.fromkeys(p_ for p_ in predicates if p_.startswith("biolink:")):
        for role, which in (("subject", "domain"), ("object", "range")):
            if pred.removeprefix("biolink:") in DOMAIN_QUIRKS:
                continue
            want = _slot_constraint(model, pred, which)
            if (
                not want
                or want not in model["classes"]
                or want in ("named thing", "entity", "thing")
            ):
                # anything fits (mixins like 'gene or gene product' aren't named things by is_a)
                continue
            for key in roles.get(role, []):
                if want not in class_lineage(model, key):
                    f.warn(
                        f"{lay['id']} predicate {pred}",
                        f"its {which} is {want}, but the {role} is {key}; use a predicate whose "
                        f"{which} fits, "
                        "or say why in basis",
                    )


MARKERS = re.compile(
    r"\bnot\b|(^|_)not_|negat|\bvia\b|_via_|orthology|predict|inferred|\bIEA\b", re.I
)


def check_code_merges(plan: dict, f: "Findings") -> None:
    """Negated or inferred codes mapped to the same target as direct ones make them
    indistinguishable."""
    for lay in plan.get("layouts") or []:
        for where, spec, _ in iter_assertions(lay):
            if spec.get("property") != "biolink:predicate":
                # several codes sharing a knowledge level or category is fine; sharing a predicate
                # isn't
                continue
            codes = spec.get("codes") or {}
            flagged = {t for d in spec.get("derived") or [] for t in as_list(d.get("true_values"))}
            by_target: dict[str, list[str]] = {}
            for code, target in codes.items():
                by_target.setdefault(str(target), []).append(code)
            for target, keys in by_target.items():
                marked = [k for k in keys if MARKERS.search(k) and k not in flagged]
                plain = [k for k in keys if not MARKERS.search(k)]
                if marked and plain:
                    f.warn(
                        where,
                        f"{marked} and {plain} all map to {target}, so negated or inferred rows "
                        "look like "
                        "direct ones; mark them with a derived field (negated, knowledge_level) or "
                        "leave "
                        "them unmapped",
                    )


def check_prefix_twins(plan: dict, f: "Findings") -> None:
    """A prefix Biolink writes differently (pubmed vs PMID, taxid vs NCBITaxon) splits one namespace
    in two."""
    import bioregistry
    import vocab

    canon = {}
    for bp in vocab.biolink_prefixes():
        norm = bioregistry.normalize_prefix(bp)
        if norm:
            canon.setdefault(norm, bp)
    twins = []
    for p_ in plan.get("prefixes") or {}:
        norm = bioregistry.normalize_prefix(p_)
        if norm and norm in canon and canon[norm] != p_ and p_ not in vocab.biolink_prefixes():
            twins.append(f"{p_} (Biolink: {canon[norm]})")
    if twins:
        f.warn(
            "prefixes",
            f"{len(twins)} declared prefix(es) are written differently in Biolink: "
            f"{', '.join(twins)}. "
            "If the data's values are tagged strings that only look like CURIEs (MITAB "
            "'pubmed:123', 'taxid:9606(human)'), mark those columns id_form native and don't "
            "declare "
            "the tags. If they are real CURIEs, keep the data's spelling (identifiers aren't "
            "rewritten) "
            "and say in notes.md which Biolink prefix each one is",
        )


def check_local_term_links(plan: dict, f: "Findings") -> None:
    """Code values are concepts: linking one with SKOS to a property (a Biolink slot) says the wrong
    thing."""
    specs = [spec for lay in plan.get("layouts") or [] for _, spec, _ in iter_assertions(lay)]
    code_values = {str(v) for spec in specs for v in (spec.get("codes") or {}).values()}
    properties = {str(spec.get(k)) for spec in specs for k in ("property", "role") if spec.get(k)}
    for t in plan.get("local_terms") or []:
        if t.get("id") not in code_values:
            continue
        for key in ("exact_match", "close_match", "broad_match", "narrow_match"):
            for target in as_list(t.get(key)):
                local = str(target).removeprefix("biolink:")
                if (str(target).startswith("biolink:") and local[:1].islower()) or str(
                    target
                ) in properties:
                    f.warn(
                        f"local term {t['id']}",
                        f"is a code value but {key} links it to the property {target}; "
                        "link it to a class or concept, or give no_match_reason",
                    )


def identifier_valued(model: dict, slot: str) -> bool:
    """Does this Biolink slot take identifiers (a CURIE/IRI range or a class), rather than text or
    numbers?"""
    if slot == "id":
        return True
    if slot in TEXT_DESPITE_RANGE:
        return False
    rng = (model["slots"].get(slot.replace("_", " ")) or {}).get("range")
    return rng in ("uriorcurie", "iri type") or rng in model["classes"]


def progress(msg: str) -> None:
    print(f"[check] {msg}", file=sys.stderr, flush=True)


def _values(rows: list[dict], col: str, spec: dict) -> list[tuple[str, dict]]:
    """(value, row) pairs for a column: lists split, empties, nulls and true-values dropped."""
    skip = set(as_list(spec.get("null_values"))) | set(as_list(spec.get("true_values")))
    out = []
    for r in rows:
        v = r.get(col)
        for x in v if isinstance(v, list) else [v]:
            if x in (None, ""):
                continue
            parts = str(x).split(spec["list_separator"]) if spec.get("list_separator") else [str(x)]
            out += [(p.strip(), r) for p in parts if p.strip() and p.strip() not in skip]
    return out


def _strings(obj, path=""):
    """(path, string) for every string value in a nested plan structure."""
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _strings(v, f"{path}[{i}]")


def check_infores(plan: dict, f: "Findings", catalog: dict | None) -> None:
    """Every infores: id the plan uses (code mappings, constants) must be in the infores catalog."""
    used: dict[str, str] = {}
    for where, value in _strings(plan.get("layouts") or []):
        if value.startswith("infores:"):
            used.setdefault(value, where)
    if not used:
        return
    if catalog is None:
        f.warn("infores", f"couldn't load the infores catalog; {len(used)} infores ids unchecked")
        return
    for iid, where in used.items():
        entry = catalog.get(iid)
        if entry is None:
            f.error(
                f"layouts{where[where.find('[') :] if '[' in where else ''}",
                f"{iid} isn't in the infores catalog; find the right id with vocab.py infores "
                "NAME, "
                "or leave the value unmapped",
            )
        elif entry.get("status") != "released":
            f.warn(iid, f"infores status is {entry.get('status')!r}, not released")


def check_units(plan: dict, f: "Findings") -> None:
    """Every unit the plan uses must exist: QUDT units (unit:...) at qudt.org, UO terms in OLS.
    Units given as a column's `unit` and units that code values map to (a column of unit labels) are
    both checked."""
    import requests

    units: dict[str, list[str]] = {}
    for lay in plan.get("layouts") or []:
        for where, spec, kind in iter_assertions(lay):
            if kind in ("column", "member") and spec.get("unit"):
                units.setdefault(spec["unit"], []).append(where)
            for target in (spec.get("codes") or {}).values():
                if str(target).startswith(("unit:", VOCAB["unit"], "UO:")):
                    units.setdefault(str(target), []).append(f"{where} (codes)")
    for unit, wheres in units.items():
        if unit.startswith("UO:"):
            try:
                r = requests.get(
                    "https://www.ebi.ac.uk/ols4/api/terms", params={"obo_id": unit}, timeout=30
                )
                r.raise_for_status()
            except requests.RequestException as e:
                f.warn(wheres[0], f"couldn't check unit {unit}: {e}")
                continue
            if not (r.json().get("_embedded") or {}).get("terms"):
                f.error(", ".join(wheres[:3]), f"unit {unit} isn't a UO term in OLS")
            continue
        iri = VOCAB["unit"] + unit.split(":", 1)[1] if unit.startswith("unit:") else unit
        if not iri.startswith(VOCAB["unit"]):
            f.warn(
                wheres[0],
                f"unit {unit} is neither a QUDT unit (unit:...) nor a UO term, so it wasn't "
                "checked",
            )
            continue
        try:
            r = requests.head(
                iri.replace("http://", "https://"),
                headers={"Accept": "text/turtle"},
                timeout=30,
                allow_redirects=True,
            )
        except requests.RequestException as e:
            f.warn(wheres[0], f"couldn't check unit {unit}: {e}")
            continue
        if r.status_code == 404:
            f.error(
                ", ".join(wheres[:3]),
                f"unit {unit} doesn't exist in QUDT ({iri}); see https://qudt.org/vocab/unit/",
            )
        elif r.status_code != 200:
            f.warn(wheres[0], f"couldn't check unit {unit}: HTTP {r.status_code}")


TABLE_TYPES = {"text/csv", "text/tab-separated-values", "text/plain"}
TABLE_SUFFIXES = (
    ".csv",
    ".tsv",
    ".tab",
    ".txt",
    ".vcf",
    ".gaf",
    ".gpad",
    ".gpi",
    ".gmt",
    ".mitab",
    ".psi",
)


def _table_like(dist: dict) -> bool:
    name = str(dist.get("contentUrl") or dist.get("name") or "").lower().removesuffix(".gz")
    formats = set(map(str, as_list(dist.get("encodingFormat"))))
    return dist.get("@type") != "cr:FileSet" and (
        bool(formats & {"text/csv", "text/tab-separated-values"}) or name.endswith(TABLE_SUFFIXES)
    )


def check_formats(original: dict, plan: dict, f: "Findings") -> None:
    """A table file described only by its compression (encodingFormat application/gzip) can't be
    read by tools that pick a reader from the format: the layout should set a dialect, which states
    the inner format."""
    assignments = match_record_sets(original, plan)
    dists = distribution_index(original)
    bare: dict[str, list[str]] = {}
    for rs in original.get("recordSet") or []:
        lay = assignments.get(rs["@id"])
        if not lay or lay.get("dialect"):
            continue
        if any(
            "jsonPath" in ((fl.get("source") or {}).get("extract") or {})
            for fl in rs.get("field") or []
        ):
            continue  # JSON
        for fid in record_set_files(rs):
            formats = set(as_list(dists.get(fid, {}).get("encodingFormat")))
            if (
                formats
                and not formats & TABLE_TYPES
                and formats <= {"application/gzip", "application/x-gzip"}
            ):
                bare.setdefault(lay["id"], []).append(str(dists[fid].get("name") or fid))
    for lay_id, names in bare.items():
        f.warn(
            f"{lay_id} dialect",
            f"{len(names)} file(s) are described only as gzip (e.g. {names[0]}), so readers "
            "can't tell they're tables; set the layout's dialect (dialects.py detect)",
        )


def check_dialect_fit(lay: dict, spec, got: list, dists: dict, f: "Findings") -> None:
    """The layout's dialect must read its sampled files into rows that match the croissant's
    columns."""
    names = set()
    for rs, url, rows in got:
        dialect = dialects.for_file(spec, dists[record_set_files(rs)[0]])
        if dialect is None:
            continue  # apply_plan reports it
        name = dialect.get("name") or "inline dialect"
        names.add(name)
        score, why = dialects.fit(rows, column_names(rs), dialect)
        if score < 0.9:
            f.error(
                f"{lay['id']} dialect",
                f"{name} doesn't read {url} into the croissant's columns "
                f"(fit {score:.2f}: {why}); try dialects.py detect",
            )
            return
    if names:
        f.note(
            f"{lay['id']} dialect",
            f"{', '.join(sorted(names))} read(s) {len(got)} sampled file(s) into the "
            "croissant's columns",
        )
    else:
        f.note(f"{lay['id']} dialect", "no file could be sampled, so the dialect is unchecked")


_ONTOLOGY: dict[str, bool] = {}


def is_ontology(prefix: str) -> bool:
    """Is this an ontology prefix (OBO Foundry or OLS), whose ids have PREFIX_local short forms?"""
    if prefix not in _ONTOLOGY:
        import bioregistry

        _ONTOLOGY[prefix] = bool(
            bioregistry.get_obofoundry_prefix(prefix) or bioregistry.get_ols_prefix(prefix)
        )
    return _ONTOLOGY[prefix]


def check_data(
    original: dict, plan: dict, f: Findings, max_mb: float, model: dict | None = None
) -> None:
    import bioregistry
    import sample as smp

    assignments = match_record_sets(original, plan)
    by_layout: dict[str, list[dict]] = {}
    for rs in original.get("recordSet") or []:
        lay = assignments.get(rs["@id"])
        if lay:
            by_layout.setdefault(lay["id"], []).append(rs)
    declared = set(plan.get("prefixes") or {}) | BUILTIN
    layouts_ = plan.get("layouts") or []
    for n, lay in enumerate(layouts_, 1):
        specs = dict(lay.get("columns") or {})
        for ent in lay.get("entities") or []:
            specs.update(ent.get("columns") or {})
        dialect = lay.get("dialect")
        try:
            dialect and dialects.check_spec(dialect)
        except KeyError:
            dialect = None  # reported by check_plan
        if not specs and not dialect:
            continue
        progress(f"data {n}/{len(layouts_)}: {lay['id']}")
        got, errs = smp.sample_rows(
            original, by_layout.get(lay["id"], []), 300, int(max_mb * 1e6), k=3, dialect=dialect
        )
        for e in errs[:3]:
            f.note(lay["id"], f"couldn't sample: {e[:200]}")
        if dialect:
            check_dialect_fit(lay, dialect, got, distribution_index(original), f)
        for _, url, _rows in got:
            wide, narrow, total = smp.WIDTHS.get(str(url), (0, 0, 0))
            if total and (wide + narrow) > 0.01 * total:
                f.warn(
                    f"{lay['id']} rows",
                    f"{wide} of {total} sampled rows of {url} have more cells than the header and "
                    f"{narrow} fewer: a delimiter inside values (a list joined by the "
                    "delimiter), shifted rows, or the wrong dialect",
                )
        rows = [r for _, _, rs_rows in got for r in rs_rows]
        if not rows:
            f.note(lay["id"], "no data sampled; id and code checks skipped")
            continue
        passed = []
        # ids of the entities a row is about (subject/object); linked extras (a variant an allele
        # may have) may be empty
        entity_ids = {
            c: e.get("name")
            for e in lay.get("entities") or []
            for c, sp in (e.get("columns") or {}).items()
            if sp.get("property") in ("biolink:id", "sc:identifier")
            and e.get("role") in ("biolink:subject", "biolink:object")
        }
        # entities sharing a role are alternatives (an anatomy term or a cellular component): a row
        # lacks an id
        # for that role only if all of them are empty
        same_role = {}
        for e in lay.get("entities") or []:
            ids_ = [
                c
                for c, sp in (e.get("columns") or {}).items()
                if sp.get("property") in ("biolink:id", "sc:identifier")
            ]
            same_role.setdefault(e.get("role"), []).extend(ids_)
        role_of = {
            c: e.get("role") for e in lay.get("entities") or [] for c in (e.get("columns") or {})
        }
        row_type = next(
            (t for t in as_list((lay.get("row") or {}).get("type")) if t.startswith("biolink:")),
            None,
        )
        row_key = _class_key(model, row_type) if model and row_type else None
        rows_are_things = bool(row_key) and "association" not in class_lineage(model, row_key)
        row_ids = [
            c for c, sp in (lay.get("columns") or {}).items() if sp.get("property") == "biolink:id"
        ]
        checks = [(col, spec, f"{lay['id']} {col}") for col, spec in specs.items()]
        checks += [
            (col, d, f"{lay['id']} {col} -> {d.get('name')}")
            for col, spec in specs.items()
            for d in spec.get("derived") or []
        ]
        for col, spec, where in checks:
            pairs = _values(rows, col, spec)
            values = [v for v, _ in pairs]
            if col in entity_ids and spec in specs.values():
                nulls = set(as_list(spec.get("null_values")))
                alternatives = same_role.get(role_of.get(col), [col])
                empty = sum(
                    1
                    for r in rows
                    if all(str(r.get(c) or "").strip() in ("", *nulls) for c in alternatives)
                )
                if empty > 0.05 * len(rows):
                    f.warn(
                        where,
                        f"{100 * empty / len(rows):.0f}% of sampled rows have no {col}, so their "
                        f"{entity_ids[col]} has no id. If another column holds the id for those "
                        "rows, add "
                        "a second entity with the same role for it; otherwise say in notes.md what "
                        "those "
                        "rows mean",
                    )
            if rows_are_things and col in row_ids and spec in specs.values():
                # within each file: files of one layout can be snapshots of the same table
                for _, url, file_rows in got:
                    ids_ = [str(r.get(col)) for r in file_rows if r.get(col) not in (None, "")]
                    dups = len(ids_) - len(set(ids_))
                    if dups > 0.05 * max(len(ids_), 1):
                        f.warn(
                            where,
                            f"rows are typed {row_type} with {col} as their id, but {dups} of "
                            f"{len(ids_)} "
                            f"sampled ids repeat within {url}; a row with a repeated id is a "
                            "relationship, "
                            "not the thing itself",
                        )
                        break
                else:
                    n_ids = sum(
                        len([r for r in fr if r.get(col) not in (None, "")]) for _, _, fr in got
                    )
                    f.note(
                        where,
                        f"row ids are unique within each sampled file ({n_ids} ids in {len(got)} "
                        "file(s))",
                    )
            # short forms only of ontologies OBO Foundry or OLS knows (MONDO_, EFO_), not ids like
            # DDB_G0292440
            obo_like = sum(
                bool((m := OBO_ID.match(v)) and not CURIE.match(v) and is_ontology(m.group(1)))
                for v in values
            )
            if (
                values
                and spec.get("id_form") != "obo"
                and obo_like >= 0.9 * len(values)
                and (spec.get("property") in ("biolink:id", "sc:identifier") or spec.get("id_form"))
            ):
                f.warn(
                    where,
                    f"values are OBO short forms (e.g. {values[0]}); use id_form obo and declare "
                    "their "
                    "prefixes",
                )
            if not values:
                if spec.get("id_prefix") or spec.get("codes") or spec.get("id_form"):
                    f.note(
                        where,
                        f"empty in all {len(rows)} sampled rows (a sparse column may still have "
                        "values; "
                        "profile it with sample.py --profile to be sure)",
                    )
                continue
            # a pattern mismatch is an error, or a note when the plan's pattern_note explains it
            explained = spec.get("pattern_note")

            def report(w, m, explained=explained):
                if explained:
                    f.note(w, f"{m} (explained by pattern_note: {str(explained)[:120]})")
                else:
                    f.error(w, m)

            errors_before = f.count("error") + f.count("warning")
            form = spec.get(
                "id_form", "bare" if (spec.get("id_prefix") or spec.get("prefix_column")) else None
            )
            if spec.get("prefix_column"):
                bad = []
                prefix_codes = spec.get("prefix_codes") or {}
                for v, r in pairs:
                    pre = str(r.get(spec["prefix_column"]) or "").strip()
                    if prefix_codes:
                        if pre and pre not in prefix_codes:
                            bad.append(f"{pre} (no entry in prefix_codes)")
                            continue
                        pre = prefix_codes.get(pre, "")
                    local = v[len(pre) + 1 :] if v.startswith(pre + ":") else v
                    pattern = bioregistry.get_pattern(pre) if pre else None
                    if pre and pre not in declared:
                        bad.append(f"{pre} (undeclared prefix)")
                    elif pattern and not re.fullmatch(pattern, local):
                        bad.append(f"{pre}:{local}")
                if len(bad) > 0.05 * len(values):
                    report(
                        where,
                        f"{len(bad)}/{len(values)} values don't fit the prefix in "
                        f"{spec['prefix_column']} "
                        f"(e.g. {list(dict.fromkeys(bad))[:4]})",
                    )
            elif form == "bare" and spec.get("id_prefix"):
                pattern = bioregistry.get_pattern(spec["id_prefix"])
                curie_like = sum(bool(CURIE.match(v)) for v in values)
                if curie_like > len(values) / 2:
                    f.error(
                        where,
                        f"declared bare but {curie_like}/{len(values)} values are CURIEs (e.g. "
                        f"{values[0]}); use id_form curie",
                    )
                elif pattern:
                    bad = [v for v in values if not re.fullmatch(pattern, v)]
                    if len(bad) > 0.05 * len(values):
                        report(
                            where,
                            f"{len(bad)}/{len(values)} values don't fit {spec['id_prefix']}'s "
                            f"pattern {pattern} "
                            f"(e.g. {bad[:3]})"
                            + (
                                ""
                                if spec.get("pattern_note")
                                else "; if the registry's pattern is out of date, "
                                "say so in pattern_note"
                            ),
                        )
                else:
                    f.note(
                        where,
                        f"Bioregistry has no id pattern for {spec['id_prefix']}; values not "
                        "checked",
                    )
            elif form == "obo":
                prefixes = Counter(m.group(1) for v in values if (m := OBO_ID.match(v)))
                if sum(prefixes.values()) < 0.95 * len(values):
                    f.error(
                        where,
                        f"declared obo but only {sum(prefixes.values())}/{len(values)} values are "
                        f"PREFIX_local short forms (e.g. {values[0]})",
                    )
                undeclared = sorted(p for p in prefixes if p not in declared)
                if undeclared:
                    f.error(
                        where, f"short-form prefix(es) not in the plan's prefixes: {undeclared}"
                    )
            elif form == "curie":
                prefixes = Counter(m.group(1) for v in values if (m := CURIE.match(v)))
                if sum(prefixes.values()) < 0.95 * len(values):
                    f.error(
                        where,
                        f"declared curie but only {sum(prefixes.values())}/{len(values)} values "
                        f"are CURIEs (e.g. {values[0]})",
                    )
                if spec.get("id_prefix") and prefixes and spec["id_prefix"] not in prefixes:
                    f.warn(where, f"id_prefix {spec['id_prefix']} but values use {dict(prefixes)}")
            # CURIE prefixes in the data must be declared, whatever the column's id_form
            seen = Counter(
                m.group(1)
                for v in values
                if (m := CURIE.match(v)) and not v.startswith(("http:", "https:"))
            )
            missing = sorted(p for p in seen if p not in declared)
            prop = str(spec.get("property") or "")
            text_column = prop.removeprefix("biolink:") in TEXT_SLOTS or bool(spec.get("codes"))
            if (
                missing
                and sum(seen.values()) >= 0.5 * len(values)
                and form != "native"
                and not text_column
            ):
                (f.error if form == "curie" else f.warn)(
                    where, f"values use CURIE prefix(es) not in the plan's prefixes: {missing}"
                )
            if (
                model
                and prop.startswith("biolink:")
                and identifier_valued(model, prop[8:])
                and form not in ("obo", "native", "url", "mixed")
                and not (spec.get("id_prefix") or spec.get("prefix_column") or spec.get("codes"))
            ):
                idlike = sum(
                    bool(CURIE.match(v)) or v.startswith(("http://", "https://")) for v in values
                )
                if idlike < 0.5 * len(values):
                    f.warn(
                        where,
                        f"{prop} takes identifiers, but the values look like plain text (e.g. "
                        f"{list(dict.fromkeys(values))[:3]}); add id_prefix or codes, or use "
                        "another property",
                    )
            if spec.get("codes"):
                unmapped = Counter(v for v in values if v not in spec["codes"])
                if unmapped:
                    f.warn(
                        where,
                        f"values with no code mapping in the sample: "
                        f"{dict(unmapped.most_common(8))}",
                    )
            if f.count("error") + f.count("warning") == errors_before and (
                form or spec.get("codes")
            ):
                passed.append(where.removeprefix(f"{lay['id']} "))
        if passed:
            f.note(
                lay["id"],
                f"sampled {len(rows)} rows; ids, prefixes and codes fit for: {', '.join(passed)}",
            )


def plain_json(path: str, tmpdir: str) -> str:
    """mlcroissant can't read gzipped JSON-LD: give it a decompressed copy."""
    if not path.endswith(".gz"):
        return path
    import gzip
    import shutil

    target = Path(tmpdir) / Path(path).name.removesuffix(".gz")
    if target.exists():
        target = target.with_name("original_" + target.name)
    with gzip.open(path, "rb") as src, open(target, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return str(target)


def run_bounded(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    """Run cmd in its own process group and kill the whole group on timeout. subprocess.run's
    timeout kills only `uv`; the python it started keeps the output pipes open, so the call would
    wait for it indefinitely. The timeout is wall-clock time: a monotonic clock stops while the
    machine sleeps, and a 3-hour timeout then took 10 hours."""
    import os
    import signal
    import tempfile
    import time

    with tempfile.TemporaryFile("w+") as out, tempfile.TemporaryFile("w+") as err:
        proc = subprocess.Popen(cmd, stdout=out, stderr=err, text=True, start_new_session=True)
        deadline = time.time() + timeout
        try:
            while proc.poll() is None:
                if time.time() > deadline:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                    raise subprocess.TimeoutExpired(cmd, timeout)
                time.sleep(2)
        except KeyboardInterrupt:
            os.killpg(proc.pid, signal.SIGKILL)
            raise
        out.seek(0)
        err.seek(0)
        return subprocess.CompletedProcess(cmd, proc.returncode, out.read(), err.read())


VALIDATION = {"result": "not run"}


VALIDATE_CACHE = Path.home() / ".cache" / "croissant-semantics" / "validate"


def run_validate(path: str, f: Findings, timeout: int) -> None:
    """mlcroissant validate, or its cached result for a byte-identical croissant (validation can
    take an hour)."""
    import hashlib

    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    cached = VALIDATE_CACHE / f"{digest}.json"
    if cached.exists():
        hit = json.loads(cached.read_text())
        VALIDATION["result"] = (
            f"{hit['result']} (cached: this exact croissant was validated before)"
        )
        if hit["result"].startswith("failed"):
            f.error("croissant", hit["error"])
        return
    _run_validate(path, f, timeout)
    if VALIDATION["result"] in ("passed",) or VALIDATION["result"].startswith("failed"):
        VALIDATE_CACHE.mkdir(parents=True, exist_ok=True)
        err = next((m for lv, w, m in f.items if lv == "error" and w == "croissant"), "")
        cached.write_text(json.dumps({"result": VALIDATION["result"], "error": err}))


def _run_validate(path: str, f: Findings, timeout: int) -> None:
    cmd = [
        "uv",
        "run",
        "--quiet",
        "--with",
        "mlcroissant==1.1.1",
        "mlcroissant",
        "validate",
        "--jsonld",
        path,
    ]
    try:
        p = run_bounded(cmd, timeout)
    except subprocess.TimeoutExpired:
        f.warn(
            "croissant",
            f"mlcroissant validate timed out after {timeout}s, so the croissant's structure is "
            "unverified; re-run with a larger --validate-timeout on an idle machine",
        )
        VALIDATION["result"] = f"timed out after {timeout}s (unverified)"
        return
    out = p.stdout + p.stderr
    if p.returncode != 0 or re.search(r"Found the following \d+ error", out):
        f.error("croissant", "mlcroissant validate failed:\n" + "\n".join(out.splitlines()[-25:]))
        VALIDATION["result"] = "failed (see Errors)"
    else:
        VALIDATION["result"] = "passed"
        warnings = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("- ")]
        for w in warnings[:10]:
            f.note("croissant", f"mlcroissant: {w}")


LOAD_SNIPPET = """
import sys, itertools, mlcroissant as mlc
ds = mlc.Dataset(jsonld=sys.argv[1])
recs = list(itertools.islice(ds.records(sys.argv[2]), 20))
print(f"{len(recs)} records read" + (f", e.g. {repr(recs[0])[:500]}" if recs else ""))
"""


LOAD_LOG: list[str] = []
PREEXISTING: dict[str, list[str]] = {}


def _load(path: str, record_set_id: str, timeout: int) -> tuple[bool, str]:
    cmd = [
        "uv",
        "run",
        "--quiet",
        "--with",
        "mlcroissant==1.1.1",
        "python",
        "-c",
        LOAD_SNIPPET,
        path,
        record_set_id,
    ]
    try:
        p = run_bounded(cmd, timeout)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    if p.returncode == 0:
        return True, (p.stdout.strip().splitlines() or [""])[0]
    lines = [
        ln.strip()
        for ln in p.stderr.splitlines()
        if re.search(r"(Error|Exception)\b", ln) and "WarningException" not in ln
    ]
    LOAD_LOG.append(f"=== {path} {record_set_id}\n{p.stderr[-6000:]}")
    return False, " | ".join(dict.fromkeys(lines[-3:])) or (
        p.stderr.strip().splitlines()[-1:] or [""]
    )[0]


def _hint(msg: str) -> str:
    """What to try for a few common load failures."""
    if re.search(r"could not convert|invalid literal|Unable to parse string|cannot convert", msg):
        return (
            " — a column typed as a number or date holds text (a '-' placeholder, a list): set its "
            "atomic type "
            "with `atomic: sc:Text` (sample.py flags these columns), and report the type as a "
            "pipeline problem"
        )
    return ""


def _gist(msg: str) -> str:
    """An error message without temp paths and ids, to tell whether two failures are the same."""
    msg = re.sub(r"/\S*/(new|original)_\S*", "<file>", msg)
    msg = re.sub(r"\b(new|original)_", "", msg)
    # the same failure can name a different column or node first (grouping reorders fields): compare
    # the kind
    msg = re.sub(r'Column ".*?" does not exist', "Column <col> does not exist", msg, flags=re.S)
    msg = re.sub(r'Column "[^"]*"', "Column <col>", msg)
    msg = re.sub(r'node "[^"]*"', "node <node>", msg)
    msg = re.sub(r"Existing columns:.*", "", msg)
    return msg[:150]


def _referenced_ids(obj) -> set[str]:
    """@ids referenced anywhere inside obj (sources, references)."""
    out = set()
    if isinstance(obj, dict):
        if set(obj) == {"@id"}:
            out.add(obj["@id"])
        for v in obj.values():
            out |= _referenced_ids(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= _referenced_ids(v)
    return out


def _excerpt(
    dist: dict, dists: dict, needs_whole: bool, head_bytes: int, whole_limit: int
) -> bytes | None:
    """The file's first complete lines (re-gzipped if it was gzipped), or the whole file when it
    must be whole."""
    import gzip as gz

    import sample as smp

    url = dist.get("contentUrl")
    if needs_whole:
        size = content_size(dist)
        if size is not None and size > whole_limit:
            return None
        return smp.fetch_bytes(
            url, whole_limit, whole=True
        )  # no declared size: fails if it's over the limit
    raw = smp.fetch_bytes(url, head_bytes, whole=False)
    gzipped = str(url).endswith(".gz") or raw[:2] == b"\x1f\x8b"
    data = smp.decompress_prefix(raw, url) if gzipped else raw
    data = data[: data.rfind(b"\n") + 1] if b"\n" in data else data
    return gz.compress(data, mtime=0) if gzipped else data


def _member_excerpt(
    dist: dict, dists: dict, needs_whole: bool, head_bytes: int, archive_bytes: int
) -> bytes:
    """An archive member's first complete lines (or all of it), re-gzipped if it is gzipped."""
    import gzip as gz

    import sample as smp

    member = str(dist.get("contentUrl") or dist.get("name") or "").split("!/")[-1]
    data = smp.file_bytes(dist, dists, archive_bytes, whole=needs_whole)
    if not needs_whole:
        data = data[:head_bytes]
        data = data[: data.rfind(b"\n") + 1] if b"\n" in data else data
    return gz.compress(data, mtime=0) if member.endswith(".gz") else data


def _member_formats(member: str, set_formats: list) -> list[str]:
    """encodingFormat for one member of a FileSet, from its name: content type first, then
    compression."""
    name = member.lower()
    base = name.removesuffix(".gz")
    by_suffix = {
        ".jsonl": "application/jsonlines",
        ".ndjson": "application/jsonlines",
        ".json": "application/json",
        ".tsv": "text/tab-separated-values",
        ".csv": "text/csv",
        ".txt": "text/plain",
    }
    content = next((t for suf, t in by_suffix.items() if base.endswith(suf)), None)
    if content is None:
        return [f for f in set_formats if f != "application/octet-stream"]
    return [content] + (["application/gzip"] if name.endswith(".gz") else [])


def _read_as_file(rs: dict, dist_id: str) -> dict:
    """A copy of the record set whose fields read dist_id as a FileObject (extract.path dropped)."""
    import copy

    rs = copy.deepcopy(rs)

    def fix(fields):
        for fl in fields:
            src = fl.get("source") or {}
            if (src.get("fileSet") or {}).get("@id") == dist_id:
                src.pop("fileSet")
                src["fileObject"] = {"@id": dist_id}
                (src.get("extract") or {}).pop("path", None)
            fix(fl.get("subField") or [])

    fix(rs.get("field") or [])
    return rs


def _excerpt_croissant(
    croissant: dict, rs_id: str, files: dict[str, bytes], tmp: Path, label: str
) -> str:
    """A small croissant with just this record set (and what it references), reading local excerpt
    files."""
    import hashlib

    rs_by_id = {r["@id"]: r for r in croissant.get("recordSet") or []}
    keep_rs, todo = [], [rs_id]
    while todo:
        rid = todo.pop()
        if rid in keep_rs or rid not in rs_by_id:
            continue
        keep_rs.append(rid)
        todo += [
            i.split("/")[0] for i in _referenced_ids(rs_by_id[rid]) if i.split("/")[0] in rs_by_id
        ]
    dists = distribution_index(croissant)
    keep_d = set()
    for rid in keep_rs:
        for i in _referenced_ids(rs_by_id[rid]):
            if i in dists:
                keep_d.add(i)
                if i not in files:  # an extracted member no longer needs its archive
                    keep_d |= {
                        p["@id"]
                        for p in as_list(dists[i].get("containedIn"))
                        if isinstance(p, dict)
                    }
    small = {k: v for k, v in croissant.items() if k not in ("recordSet", "distribution")}
    small["recordSet"] = [rs_by_id[r] for r in keep_rs]
    small["distribution"] = []
    for d in croissant.get("distribution") or []:
        if d.get("@id") not in keep_d:
            continue
        d = dict(d)
        if d["@id"] in files and d.get("@type") == "cr:FileSet":
            d["@type"] = "cr:FileObject"
            d.pop("includes", None)
            member = next(
                (
                    field_source(fl)[2].get("path")
                    for r in small["recordSet"]
                    for fl in r.get("field") or []
                    if field_source(fl)[1] == d["@id"] and field_source(fl)[2].get("path")
                ),
                "member",
            )
            d["contentUrl"] = member  # renamed to the local excerpt below
            # the set lists the formats of all its members (JSON first, say); this member needs its
            # own, or
            # mlcroissant reads a JSON Lines file as one JSON document
            d["encodingFormat"] = _member_formats(member, as_list(d.get("encodingFormat")))
            small["recordSet"] = [_read_as_file(r, d["@id"]) for r in small["recordSet"]]
        if d["@id"] in files:
            d.pop("containedIn", None)
            base = Path(str(d.get("contentUrl") or d.get("name")).split("!/")[-1]).name
            # short, unique names: ids and member names can be long (ClinPGx guideline files passed
            # 255 bytes)
            digest = hashlib.sha1(d["@id"].encode()).hexdigest()[:12]
            name = f"{label}_{digest}_{re.sub(r'[^A-Za-z0-9._-]', '_', base)[-80:]}"
            (tmp / name).write_bytes(files[d["@id"]])
            d["contentUrl"] = name
            d["sha256"] = hashlib.sha256(files[d["@id"]]).hexdigest()
            d.pop("md5", None)
            d["contentSize"] = f"{len(files[d['@id']])} B"
        small["distribution"].append(d)
    path = tmp / f"{label}_{hashlib.sha1(rs_id.encode()).hexdigest()[:16]}.json"
    path.write_text(json.dumps(small))
    return str(path)


def load_records(
    original: dict,
    out: dict,
    plan: dict,
    f: Findings,
    max_layouts: int,
    tmp: str,
    head_mb: float = 2,
    whole_mb: float = 20,
    timeout: int = 300,
    archive_mb: float = 40,
) -> None:
    """Read a few records of one record set per layout through mlcroissant, using the first complete
    lines of each file (JSON files must be read whole, so only small ones are used; archive members
    are extracted from archives up to archive_mb)."""
    import sample as smp

    tmpdir = Path(tmp)
    dists = distribution_index(original)
    assignments = match_record_sets(original, plan)
    # one record set per original layout (files with the same columns), however many plan entries
    # cover it
    best: dict[str, tuple[float, dict]] = {}
    alternates: dict[str, list[dict]] = {}
    unreadable: dict[str, str] = {}
    for rs in original.get("recordSet") or []:
        lay = assignments.get(rs["@id"])
        if not lay:
            continue
        ids = record_set_files(rs)
        if not ids:
            continue
        src, _ = smp.source_file(rs, dists)
        kind = smp.file_kind(src or dists.get(ids[0], {}), rs.get("field") or [])
        if kind in ("xlsx", "sqlite"):
            unreadable.setdefault(kind, lay["id"])
            continue
        whole = (
            kind == "json"
        )  # JSON must be read whole; JSON Lines and tables are cut to their first lines
        # sizes of the files actually read: a directory FileSet's member, not the whole set
        sizes = (
            [content_size(src)]
            if src and src is not dists.get(ids[0])
            else [content_size(dists.get(i, {})) for i in ids]
        )
        if whole and any(s is not None and s > whole_mb * 1e6 for s in sizes):
            continue
        if any(
            dists.get(i, {}).get("@type") == "cr:FileSet" and dists[i].get("containedIn")
            for i in ids
        ):
            continue  # a set of archive members: left to mlcroissant, too slow to test here
        cost = sum(s or 0 for s in sizes) if whole else len(ids)
        key = layout_id(column_names(rs))
        alternates.setdefault(key, []).append(rs)
        if key not in best or cost < best[key][0]:
            best[key] = (cost, rs)
    for kind, lay_id in unreadable.items():
        f.note(
            "records",
            f"{kind} files aren't loaded: mlcroissant 1.1.1 has no reader for them (e.g. layout "
            f"{lay_id}); the sampled-data checks are the evidence",
        )

    def fmt(rs):
        d = dists.get((record_set_files(rs) or [""])[0], {})
        name = str(d.get("contentUrl") or d.get("name") or "")
        return (
            " ".join(map(str, as_list(d.get("encodingFormat")))),
            Path(name.removesuffix(".gz")).suffix,
            any(
                "jsonPath" in ((x.get("source") or {}).get("extract") or {})
                for x in rs.get("field") or []
            ),
        )

    # round-robin over file formats, so one format that mlcroissant can't read doesn't use up the
    # budget
    groups: dict = {}
    for lay_id, item in best.items():
        groups.setdefault(fmt(item[1]), []).append((lay_id, item))
    chosen = []
    max_layouts = max_layouts or len(best)
    while len(chosen) < max_layouts and any(groups.values()):
        for key in list(groups):
            if groups[key] and len(chosen) < max_layouts:
                chosen.append(groups[key].pop(0))
    for n, (key, (_, first)) in enumerate(chosen, 1):
        lay_id = assignments[first["@id"]]["id"]
        progress(f"loading records {n}/{len(chosen)}: {lay_id}")
        # if a record set fails from the original too, try one more from the same layout (another
        # file)
        others = [r for r in alternates.get(key, []) if r["@id"] != first["@id"]]
        tries = [first] + others[-1:]
        for attempt, rs in enumerate(tries):
            where = f"{lay_id} records ({text(rs.get('name'))})"
            src, _ = smp.source_file(rs, dists)
            # JSON must be read whole; JSON Lines and tables can be cut to their first lines
            whole = smp.file_kind(src or {}, rs.get("field") or []) == "json"
            files = {}
            try:
                for i in record_set_files(rs):
                    d = dists.get(i, {})
                    if d.get("@type") == "cr:FileSet" and not d.get("containedIn"):
                        # a directory FileSet: test with the member the fields name (extract.path),
                        # as one file
                        member, why = smp.source_file(rs, dists)
                        if member is None:
                            raise ValueError(f"can't load records: the record set {why}")
                        data = _excerpt(
                            member, dists, whole, int(head_mb * 1e6), int(whole_mb * 1e6)
                        )
                        if data is None:
                            raise ValueError(
                                f"{member.get('contentUrl')} must be read whole and is over "
                                f"{whole_mb} MB"
                            )
                        files[i] = data
                        continue
                    if d.get("containedIn"):
                        # extract the member ourselves: this tests the plan, not mlcroissant's
                        # archive handling
                        # (it can't open 'x.zip!/member' paths) or the archive's checksum
                        files[i] = _member_excerpt(
                            d, dists, whole, int(head_mb * 1e6), int(archive_mb * 1e6)
                        )
                        continue
                    data = _excerpt(d, dists, whole, int(head_mb * 1e6), int(whole_mb * 1e6))
                    if data is None:
                        raise ValueError(
                            f"{d.get('contentUrl')} must be read whole and is over {whole_mb} MB"
                        )
                    files[i] = data
            except smp.BotCheck as e:
                f.warn(where, f"couldn't load records: {e}")
                continue
            except (
                Exception,
                SystemExit,
            ) as e:  # network errors, 401s, size limits: report and move on
                f.note(where, f"couldn't fetch data to load records: {str(e)[:200]}")
                continue
            try:
                new_path = _excerpt_croissant(out, rs["@id"], files, tmpdir, "new")
                ok, msg = _load(new_path, rs["@id"], timeout)
            except (
                OSError,
                ValueError,
                KeyError,
            ) as e:  # the test's own failure: report it and go on
                f.warn(
                    where,
                    f"the record-load test itself failed ({type(e).__name__}: {str(e)[:200]}); "
                    "records "
                    "weren't tested for this layout",
                )
                continue
            if ok:
                if msg.startswith("0 records"):
                    f.warn(
                        where,
                        f"mlcroissant read no records from the first {head_mb:g} MB (an empty "
                        "file, or "
                        "rows it can't parse)",
                    )
                else:
                    f.note(where, f"loaded (from the first {head_mb:g} MB): {msg[:300]}")
                break
            old_path = _excerpt_croissant(original, rs["@id"], files, tmpdir, "original")
            ok_old, old_msg = _load(old_path, rs["@id"], timeout)
            if ok_old:
                f.error(
                    where,
                    f"loads from the original croissant but not the new one: "
                    f"{msg[:700]}{_hint(msg)}",
                )
                break
            dialect_spec = assignments[rs["@id"]].get("dialect")
            needs_dialect = bool(dialect_spec) and any(
                (d := dialects.for_file(dialect_spec, dists[i]))
                and (d.get("csvw", {}).get("commentPrefix") or d.get("columns") == "field_order")
                for i in record_set_files(rs)
                if i in dists and dists[i].get("@type") != "cr:FileSet"
            )
            if needs_dialect or ("encoding formats" in old_msg and "encoding formats" not in msg):
                PREEXISTING.setdefault(
                    "the files have comment lines or no header row, which mlcroissant can't skip "
                    "(it ignores "
                    "csvw:dialect); the sampled-data checks, which use the dialect, are the "
                    "evidence",
                    [],
                ).append(where)
                break
            if _gist(msg) != _gist(old_msg):
                f.warn(
                    where,
                    f"fails from the original croissant too, but with a different error, so the "
                    "plan may "
                    f"have added a problem. New: {msg[:400]} | Original: "
                    f"{old_msg[:300]}{_hint(msg)}",
                )
                break
            key = msg[:300]
            if "does not exist in node" in msg:
                key = (
                    "a field's column is missing from the excerpt read (a key only some records "
                    "have, or not "
                    "in the first lines): " + _gist(msg)[:120]
                )
            PREEXISTING.setdefault(key, []).append(where)
            if attempt + 1 == len(tries):
                break
    for msg, wheres in PREEXISTING.items():
        f.note(
            "records",
            f"{len(wheres)} record set(s) don't load from the original croissant either (not "
            "caused "
            f"by the plan; see the load_errors file): {msg} — {', '.join(wheres[:6])}"
            + (" …" if len(wheres) > 6 else ""),
        )
    tested = {assignments[item[1]["@id"]]["id"] for _, item in chosen}
    skipped = [lay["id"] for lay in plan.get("layouts") or [] if lay["id"] not in tested]
    if skipped:
        f.note(
            "records",
            f"not loaded for {len(skipped)} plan layout(s) (one record set is tested per set of "
            f"columns; also limit --load-layouts {max_layouts}, JSON files over {whole_mb} MB, "
            f"spreadsheets and databases): {', '.join(skipped[:10])}",
        )


def classify(term: str) -> str:
    p = term.split(":", 1)[0] if ":" in term and not term.startswith("http") else ""
    if p == "biolink":
        return "biolink"
    if p == "rcv":
        return "local"
    if p in ("sc", "cr", ""):
        return "none"
    return "other"


def coverage(out: dict) -> dict:
    level_rank = {"biolink": 0, "other": 1, "local": 2, "none": 3}
    fields_by = Counter()
    rs_total = rs_typed = rs_rowtype = 0
    ignore = ("codes_", "rc_local_terms")

    def walk(field, in_group):
        terms = [t for t in as_list(field.get("dataType")) if t not in ATOMIC and t != "sc:Thing"]
        terms += as_list(field.get("equivalentProperty"))
        if field.get("subField"):
            for sf in field["subField"]:
                if "source" in sf:
                    walk(sf, True)
            return
        if not terms and field.get("references"):
            ref = ((field["references"].get("field") or {}).get("@id") or "").split("/")[0]
            terms = code_targets.get(ref) or ["rcv:codes"]  # classified by what the codes map to
        kinds = sorted({classify(t) for t in terms}, key=level_rank.get)
        kind = kinds[0] if kinds else "none"
        if kind == "none" and in_group:
            kind = "grouped only"
        fields_by[kind] += 1

    code_targets = {}
    for rs in out.get("recordSet") or []:
        if rs["@id"].startswith("codes_"):
            urls = [
                str(v) for row in rs.get("data") or [] for k, v in row.items() if k.endswith("/url")
            ]
            kinds = Counter(classify(u) for u in urls)
            code_targets[rs["@id"]] = (
                [next((u for u in urls if classify(u) == kinds.most_common(1)[0][0]), "rcv:codes")]
                if urls
                else []
            )
    for rs in out.get("recordSet") or []:
        if rs["@id"].startswith(ignore):
            continue
        rs_total += 1
        if any(t not in ("sc:Enumeration",) for t in as_list(rs.get("dataType"))):
            rs_rowtype += 1
        if any(t not in ("sc:Enumeration",) for t in as_list(rs.get("dataType"))) or any(
            fl.get("subField") or fl.get("equivalentProperty") for fl in rs.get("field") or []
        ):
            rs_typed += 1
        for fl in rs.get("field") or []:
            walk(fl, False)
    total = sum(fields_by.values())
    return {
        "record_sets": rs_total,
        "record_sets_with_semantics": rs_typed,
        "record_sets_with_row_type": rs_rowtype,
        "fields": total,
        "fields_by_kind": dict(fields_by),
    }


def write_report(args, out: dict, original: dict, f: "Findings", stage_note: str = "") -> None:
    if (
        not stage_note
        and not getattr(args, "layouts", None)
        and not getattr(args, "skip_layouts", None)
    ):  # with --layouts, other layouts weren't checked
        for i, e in enumerate(f.explained):
            if i not in f.used and e.get("reason"):
                f.note(
                    "plan explained", f"matched no warning (now fixed, or the text changed?): {e}"
                )
    cov = coverage(out)
    before = coverage(original)

    lines = [f"# Semantics check: {text(out.get('name'))}", ""]
    if stage_note:
        lines += [f"**{stage_note}**", ""]
    lines.append(
        f"- Record sets with semantics: {cov['record_sets_with_semantics']} of "
        f"{cov['record_sets']} "
        f"(before: {before['record_sets_with_semantics']}); with a row type: "
        f"{cov['record_sets_with_row_type']}"
    )
    lines.append(
        f'- Fields: {cov["fields"]} ("before" counts semantics already in the input croissant, '
        "e.g. nested fields from the original pipeline)"
    )
    for kind in ("biolink", "other", "local", "grouped only", "none"):
        n = cov["fields_by_kind"].get(kind, 0)
        b = before["fields_by_kind"].get(kind, 0)
        lines.append(f"  - {kind}: {n} ({100 * n / max(cov['fields'], 1):.1f}%; before {b})")
    # only table files can have a dialect (not PDFs, spreadsheets, JSON or archives)
    tables = [d for d in out.get("distribution") or [] if d.get("csvw:dialect") or _table_like(d)]
    with_dialect = sum(1 for d in tables if d.get("csvw:dialect"))
    lines.append(
        f"- Table files with a dialect (how to read them): {with_dialect} of {len(tables)}"
    )
    lines.append(f"- MLCommons validator: {VALIDATION['result']}")
    lines.append(
        f"- Findings: {f.count('error')} errors, {f.count('warning')} warnings, {f.count('note')} "
        "notes"
    )
    lines.append("")
    for level in ("error", "warning", "note"):
        # the same message from many places (one per plan entry) is listed once, with where it came
        # from
        grouped: dict[str, list[str]] = {}
        for lv, where, msg in f.items:
            if lv == level:
                grouped.setdefault(msg, []).append(str(where))
        if grouped:
            lines.append(f"## {level.capitalize()}s")
            lines.append("")
            for msg, wheres in grouped.items():
                if len(wheres) == 1:
                    lines.append(f"- **{wheres[0]}**: {msg}")
                else:
                    shown = ", ".join(wheres[:8]) + (
                        f", … ({len(wheres)} in all)" if len(wheres) > 8 else ""
                    )
                    lines.append(f"- **{shown}**: {msg}")
            lines.append("")
    report = "\n".join(lines)
    print(report)
    if args.report:
        Path(args.report).write_text(report)
        log = Path(args.report).with_suffix(".load_errors.txt")
        if LOAD_LOG:
            log.write_text("\n\n".join(LOAD_LOG))
            print(f"\nFull mlcroissant errors: {log}")
        elif log.exists():
            log.unlink()  # from an earlier run: it would describe failures this run didn't have
        # every finding as data, so a plan's "explained" entries can quote a warning's where and
        # message exactly
        Path(args.report).with_suffix(".findings.json").write_text(
            json.dumps(
                [{"level": lv, "where": str(w), "message": m} for lv, w, m in f.items], indent=1
            )
        )


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("original")
    ap.add_argument("plan")
    ap.add_argument("out")
    ap.add_argument("--report")
    ap.add_argument("--no-data", action="store_true")
    ap.add_argument("--no-validate", action="store_true")
    ap.add_argument("--load-records", action="store_true")
    ap.add_argument(
        "--layouts",
        help="comma-separated plan layout ids: check data and records for only these "
        "(plan and validator checks still cover everything)",
    )
    ap.add_argument("--max-mb", type=float, default=40)
    ap.add_argument(
        "--load-layouts", type=int, default=0, help="layouts to load records for (default 0: all)"
    )
    ap.add_argument(
        "--skip-layouts",
        help="comma-separated plan layout ids to leave out of the data and record checks",
    )
    ap.add_argument("--validate-timeout", type=int, default=3600)
    ap.add_argument(
        "--answers",
        help="maintainer answers (default: semantics_answers.json next to the original "
        "croissant, if any); merged into the plan as apply_plan.py does",
    )
    ap.add_argument(
        "--plan-only",
        action="store_true",
        help=(
            "check only the plan (terms, prefixes, dialects); seconds, so run it before the full "
            "check"
        ),
    )
    ap.add_argument(
        "--url-map",
        action="append",
        metavar="OLD=NEW",
        help="replace OLD with NEW in data URLs before downloading (repeatable), for a croissant "
        "whose URLs point at a release that's gone; say so in notes.md",
    )
    args = ap.parse_args()
    import sample as smp

    smp.parse_url_maps(args.url_map)

    try:
        plan = resolve_plan(json.loads(Path(args.plan).read_text()))
    except PlanError as e:
        sys.exit(f"Plan error: {e}")
    original = load_croissant(args.original)
    answer_lines = []
    if (apath := answers_path(args.original, args.answers)) is not None:
        # the same merge apply_plan.py does, so the checks see the plan that was applied
        plan, answer_lines = merge_answers(
            plan, original, json.loads(apath.read_text()).get("answers") or []
        )
    out = load_croissant(args.out)
    f = Findings(plan.get("explained"))
    for ln in answer_lines:
        (f.warn if "matched no layout" in ln else f.note)("maintainer answers", ln)
    for e in plan.get("explained") or []:
        if not (e.get("reason") and (e.get("where") or e.get("message"))):
            f.error(
                "plan explained", f"each entry needs a reason and a where or message to match: {e}"
            )
    try:
        import vocab

        model = vocab.biolink()
    except Exception as e:  # noqa: BLE001 - reported; Biolink checks are then skipped
        model = None
        f.warn("plan", f"couldn't load Biolink ({e}); Biolink term checks skipped")
    import signal

    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    stage = "plan"
    try:
        progress("checking the plan")
        check_plan(plan, f, model)
        check_units(plan, f)
        try:
            import vocab

            catalog = vocab.infores()
        except Exception as e:  # noqa: BLE001 - reported as a warning by check_infores
            progress(f"couldn't load the infores catalog: {e}")
            catalog = None
        check_infores(plan, f, catalog)
        check_formats(original, plan, f)
        check_roles(plan, f, model)
        check_code_merges(plan, f)
        check_prefix_twins(plan, f)
        check_local_term_links(plan, f)
        for key in CONTEXT_TERMS:
            if key not in out.get("@context", {}):
                f.error("croissant", f"@context lacks {key}")
        if args.plan_only:
            write_report(args, out, original, f, stage_note="Plan checks only (--plan-only).")
            sys.exit(1 if f.count("error") else 0)
        data_plan = plan
        if args.layouts or args.skip_layouts:
            keep = (
                set(args.layouts.split(","))
                if args.layouts
                else {lay.get("id") for lay in plan.get("layouts") or []}
            )
            keep -= set((args.skip_layouts or "").split(","))
            data_plan = {
                **plan,
                "layouts": [lay for lay in plan.get("layouts") or [] if lay.get("id") in keep],
            }
        if not args.no_data:
            stage = "data"
            check_data(original, data_plan, f, args.max_mb, model)
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out_plain = plain_json(args.out, tmp)
            if not args.no_validate:
                stage = "validation"
                progress("running mlcroissant validate (minutes for large croissants)")
                run_validate(out_plain, f, args.validate_timeout)
            if args.load_records:
                stage = "record loading"
                load_records(
                    original, out, data_plan, f, args.load_layouts, tmp, archive_mb=args.max_mb
                )
    except KeyboardInterrupt:
        write_report(args, out, original, f, stage_note=f"Incomplete: interrupted during {stage}.")
        sys.exit(130)
    except (
        Exception
    ) as e:  # a crash in the check itself: keep what was found so far, then show the crash
        f.error("check.py", f"crashed during {stage}: {type(e).__name__}: {e}")
        write_report(
            args, out, original, f, stage_note=f"Incomplete: check.py crashed during {stage}."
        )
        raise
    write_report(args, out, original, f)
    progress(
        f"done: {f.count('error')} errors, {f.count('warning')} warnings, {f.count('note')} notes"
        + (f"; report in {args.report}" if args.report else "")
    )
    sys.exit(1 if f.count("error") else 0)


if __name__ == "__main__":
    main()
