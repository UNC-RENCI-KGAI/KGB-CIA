"""Shared helpers: reading croissants and grouping record sets into layouts.

A *layout* is the set of column names a record set has. Knowledge bases often publish hundreds of
files with the same columns (one per species, release or chunk); they share one layout, and the
semantics plan describes each layout once.
"""

import gzip
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path


def load_croissant(path: str | Path) -> dict:
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def save_croissant(croissant: dict, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(croissant, indent=1, ensure_ascii=False)
    if path.suffix == ".gz":
        with gzip.GzipFile(path, "wb", mtime=0) as f:
            f.write(text.encode("utf-8"))
    else:
        path.write_text(text, encoding="utf-8")


def as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def text(value) -> str:
    """Croissant names and descriptions may be plain strings or language maps."""
    if isinstance(value, dict):
        return value.get("en") or next(iter(value.values()), "")
    return value or ""


def record_sets(croissant: dict) -> list[dict]:
    return croissant.get("recordSet") or []


def column_names(record_set: dict) -> list[str]:
    return [text(f.get("name")) for f in record_set.get("field") or []]


def file_columns(record_set: dict) -> list[str]:
    """The columns a record set's fields read from its file: `extract.column` where a field gives
    one (generators name fields `column_1` while extracting the real header name, or the reverse),
    else the field's name."""
    out = []
    for f in record_set.get("field") or []:
        col = ((f.get("source") or {}).get("extract") or {}).get("column")
        out.append(text(col) if col else text(f.get("name")))
    return out


def by_field_name(rows: list[dict], record_set: dict) -> list[dict]:
    """Rows keyed by file column, re-keyed by field name (plans refer to fields by name). Several
    fields can read one column (a derived field and the column it's derived from); each gets the
    value. Cells no field reads keep their column name."""
    pairs = list(zip(file_columns(record_set), column_names(record_set), strict=True))
    if all(c == n for c, n in pairs):
        return rows
    read = {c for c, _ in pairs}
    return [
        {**{k: v for k, v in r.items() if k not in read}, **{n: r[c] for c, n in pairs if c in r}}
        for r in rows
    ]


def layout_id(columns: list[str]) -> str:
    """Stable short id for a set of column names (order ignored)."""
    key = "\x1f".join(sorted(columns))
    return "L" + hashlib.sha1(key.encode()).hexdigest()[:8]


def layouts(croissant: dict) -> dict[str, list[dict]]:
    """layout id -> record sets with that set of columns, largest layouts first."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for rs in record_sets(croissant):
        groups[layout_id(column_names(rs))].append(rs)
    return dict(sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])))


def distribution_index(croissant: dict) -> dict[str, dict]:
    return {d["@id"]: d for d in croissant.get("distribution") or [] if "@id" in d}


def field_source(field: dict) -> tuple[str | None, str | None, dict]:
    """(kind, distribution id, extract) for a field's source; kind is fileObject or fileSet."""
    source = field.get("source") or {}
    for kind in ("fileObject", "fileSet"):
        ref = source.get(kind)
        if isinstance(ref, dict) and "@id" in ref:
            return kind, ref["@id"], source.get("extract") or {}
    return None, None, source.get("extract") or {}


def record_set_files(record_set: dict) -> list[str]:
    """Distribution ids the record set reads from (including through grouped sub-fields)."""
    ids = []

    def walk(fields):
        for f in fields:
            _, dist_id, _ = field_source(f)
            if dist_id and dist_id not in ids:
                ids.append(dist_id)
            walk(f.get("subField") or [])

    walk(record_set.get("field") or [])
    return ids


_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?i?B)?\s*$", re.I)
_UNITS = {
    "": 1,
    "b": 1,
    "kb": 1e3,
    "mb": 1e6,
    "gb": 1e9,
    "tb": 1e12,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
}


def content_size(dist: dict) -> int | None:
    m = _SIZE.match(str(dist.get("contentSize") or ""))
    if not m:
        return None
    return int(float(m.group(1)) * _UNITS.get((m.group(2) or "").lower(), 1))


CURIE = re.compile(r"^([A-Za-z][A-Za-z0-9_.-]*):(\S+)$")
