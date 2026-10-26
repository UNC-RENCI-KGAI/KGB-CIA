#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "jsonpath-ng", "openpyxl", "truststore"]
# ///
"""File dialects: how to read a table file, as a computable description.

A dialect is W3C CSVW dialect properties (delimiter, commentPrefix, header, ...) plus where the
column names come from ('header' row, or 'field_order': no header row, columns are the croissant's
fields in order), plus the format's published spec when there is one (VCF, GAF, MITAB). Named
dialects live in dialects.json.

    dialects.py list                                   # the named dialects
    dialects.py show gaf-2.2
    dialects.py detect CROISSANT --layout ID           # which named dialects read its files
    # read records using only the croissant's own description
    dialects.py read CROISSANT --record-set ID [-n 5]

`read` uses nothing but the croissant: the file's encodingFormat, its csvw:dialect and
rcv:columnOrder. If it works, the croissant describes the file well enough for any reader."""

import argparse
import csv
import itertools
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import (
    as_list,
    by_field_name,
    distribution_index,
    file_columns,
    layouts,
    load_croissant,
    record_set_files,
    text,
)

REGISTRY_PATH = Path(__file__).parent / "dialects.json"
csv.field_size_limit(
    sys.maxsize
)  # sequences and free text can exceed csv's default 128 KB per field
CSVW = "http://www.w3.org/ns/csvw#"


GENERIC = {
    "csv",
    "tsv",
    "tsv-quoted",
    "csv-noheader",
    "tsv-noheader",
    "tsv-commented",
    "tsv-comment-header",
    "tsv-commented-noheader",
    "tsv-skip-header",
    "csv-skip-header",
    "tsv-quoted-noheader",
    "ssv",
}
# ROBOT template strings (https://robot.obolibrary.org/template): a row of these under the header is
# not data
# not data: keywords alone, annotations naming a property ("A rdfs:comment", "AL
# definition@en"), logical templates with a '%' ("SC %"), and split markers
ROBOT_CELL = re.compile(
    r"^(ID|LABEL|TYPE|CLASS_TYPE|PROPERTY_TYPE|DOMAIN|RANGE|CHARACTERISTIC|INDIVIDUAL_TYPE)$"
    r"|^>?(A|AT|AL|AI|AA) \S+[:@]\S*"
    r"|^>?(C|SC|EC|EQ|DC|DI|SP|EP|DP|IP|I|II|SI) .*%"
    r"|^SPLIT="
)


def registry() -> dict:
    return {k: v for k, v in json.loads(REGISTRY_PATH.read_text()).items() if not k.startswith("_")}


def resolve(spec) -> dict:
    """A dialect from a registry name, or an inline dict (optionally {"based_on": name,
    ...overrides})."""
    reg = registry()
    if isinstance(spec, str):
        if spec not in reg:
            raise KeyError(f"no dialect {spec!r}; known: {', '.join(reg)}")
        return {"name": spec, **reg[spec]}
    spec = dict(spec)
    base = reg.get(spec.pop("based_on", None) or "", {})
    out = {**base, **spec, "csvw": {**base.get("csvw", {}), **spec.get("csvw", {})}}
    if "columns" not in out:
        raise KeyError("an inline dialect needs 'columns': 'header' or 'field_order'")
    return out


def file_name(dist: dict) -> str:
    """The file's name without a compression suffix, lower-cased (FileSets: their first include
    pattern)."""
    name = str(
        dist.get("contentUrl")
        or (as_list(dist.get("includes")) or [""])[0]
        or dist.get("name")
        or ""
    )
    return name.lower().removesuffix(".gz").removesuffix(".bz2").removesuffix(".zip")


def for_file(spec, dist: dict) -> dict | None:
    """The dialect a plan's `dialect` value gives this file: a name, an inline dict, or
    {"by_suffix": {".csv": ..., ".tsv": ...}} choosing by file name (compression suffix ignored;
    longest match wins). None when by_suffix has no entry for the file."""
    if isinstance(spec, dict) and "by_suffix" in spec:
        name = file_name(dist)
        hits = sorted(
            (s for s in spec["by_suffix"] if name.endswith(s.lower())), key=len, reverse=True
        )
        return resolve(spec["by_suffix"][hits[0]]) if hits else None
    return resolve(spec)


def check_spec(spec) -> None:
    """Raise KeyError if any dialect a plan's `dialect` value names is unknown or incomplete."""
    for d in (
        spec["by_suffix"].values() if isinstance(spec, dict) and "by_suffix" in spec else [spec]
    ):
        resolve(d)


def to_croissant(dialect: dict, columns: list[str] | None = None) -> dict:
    """The properties written onto a FileObject. For a file without a header row, `columns` (the
    column names in file order) is written as rcv:columnNames, since grouping fields changes the
    croissant's order."""
    # a null is kept: CSVW's "quoteChar": null means no quoting, and leaving it out would mean the
    # default '"' to a reader working from the croissant
    props = {"@type": "csvw:Dialect"}
    for k, v in dialect.get("csvw", {}).items():
        props[f"csvw:{k}"] = v
    out = {"csvw:dialect": props, "rcv:columnOrder": dialect["columns"]}
    if dialect.get("header_in_comments"):
        out["rcv:headerInComments"] = True
    if dialect["columns"] == "field_order" and columns:
        out["rcv:columnNames"] = list(columns)
    if dialect.get("rest_column") and columns:
        out["rcv:restColumn"] = columns[-1]  # takes the row's remaining cells as a list
    if dialect.get("name"):
        out["rcv:dialect"] = dialect["name"]
    return out


def from_croissant(dist: dict) -> dict | None:
    """Read a dialect back from a FileObject (the inverse of to_croissant)."""
    props = dist.get("csvw:dialect")
    if not props:
        return None
    csvw = {k.split(":", 1)[1]: v for k, v in props.items() if k.startswith("csvw:")}
    return {
        "csvw": csvw,
        "columns": dist.get("rcv:columnOrder", "header"),
        "name": dist.get("rcv:dialect"),
        "column_names": dist.get("rcv:columnNames"),
        "rest_column": dist.get("rcv:restColumn"),
        "header_in_comments": bool(dist.get("rcv:headerInComments")),
    }


def encoding_formats(dist: dict, dialect: dict) -> list[str]:
    """encodingFormat for a file read with this dialect: the content type first, then compression,
    then the spec. mlcroissant picks its reader from the first entry, so a gzipped table must not
    lead with application/gzip."""
    name = " ".join(
        str(x)
        for x in [dist.get("contentUrl") or dist.get("name") or "", *as_list(dist.get("includes"))]
    )
    existing = as_list(dist.get("encodingFormat"))
    table_types = ("text/csv", "text/tab-separated-values", "text/plain")
    formats = (
        [dialect["media_type"]]
        if dialect.get("media_type")
        else [f for f in existing if f in table_types][:1]
    )
    if ".gz" in name or "application/gzip" in existing:
        formats.append("application/gzip")
    if dialect.get("spec"):
        formats.append(dialect["spec"])
    for f in existing:
        if f not in formats and f not in table_types:
            formats.append(f)
    return formats


def iter_rows(lines, dialect: dict, field_names: list[str]):
    """Rows from text lines, following the dialect. Implements the CSVW properties used in
    dialects.json: delimiter, quoteChar, doubleQuote (false: backslash escapes), commentPrefix,
    header, headerRowCount, skipRows, skipBlankRows, null. A row wider than the column names keeps
    the surplus cells under '__extra__', unless they're all empty (a trailing delimiter, as in
    CTD's TSVs)."""
    c = dialect.get("csvw", {})
    quote = c.get("quoteChar", '"')
    fmt = {
        "delimiter": c.get("delimiter", ","),
        "quotechar": quote or '"',
        "doublequote": c.get("doubleQuote", True),
        # CSVW: doubleQuote false means a backslash escapes the quote character (\")
        "escapechar": None if c.get("doubleQuote", True) else "\\",
        "quoting": csv.QUOTE_NONE if quote is None else csv.QUOTE_MINIMAL,
    }
    comment = c.get("commentPrefix")
    skip = int(c.get("skipRows", 0) or 0)
    header_rows = int(c.get("headerRowCount", 1 if c.get("header", True) else 0))
    nulls = set(as_list(c.get("null")))
    rest = dialect.get("rest_column") or None
    header_in_comments = bool(dialect.get("header_in_comments"))
    if dialect.get("columns", "header") == "header":
        names = None
    else:
        names = list(dialect.get("column_names") or field_names)

    def physical():
        """Lines that aren't skipped or comments, with their line ends restored, for one csv.reader:
        quoted fields may span lines (free text exported from spreadsheets)."""
        last_comment, started = None, False
        for i, line in enumerate(lines):
            line = line.rstrip("\r\n")
            if i == 0:
                line = line.removeprefix(
                    "\ufeff"
                )  # a UTF-8 byte-order mark (Excel exports) isn't part of a name
            if i < skip:
                continue
            if comment and line.startswith(comment):
                # blank and rule lines ("#", "#-----") after the names don't replace them
                if not started and re.search(r"\w", line[len(comment) :]):
                    last_comment = line
                continue
            if not started and header_in_comments and last_comment is not None:
                yield last_comment + "\n"  # the comment block's last line names the columns
            started = True
            yield line + "\n"

    for cells in csv.reader(physical(), **fmt):
        if not cells or (len(cells) == 1 and not cells[0].strip()):
            if c.get("skipBlankRows", True):
                continue
        if header_rows:
            header_rows -= 1
            if names is None:
                # a header row that starts with a comment mark ('#CHROM', FlyBase '#gene_symbol'):
                # keep the mark only
                # if the croissant's column has it
                names = [
                    h  # exact when the croissant keeps the header's spaces
                    if h in field_names
                    else h.strip()
                    if h.strip() in field_names or not h.strip().startswith(("#", "!"))
                    else h.strip().lstrip("#! ")
                    for h in cells
                ]
                while names and not names[-1]:  # a trailing delimiter on the header row
                    names.pop()
            continue
        if names is None:
            names = list(dialect.get("column_names") or field_names)
        row = {n: (None if v in nulls else v) for n, v in zip(names, cells, strict=False)}
        if rest is not None:  # the last column takes the remaining cells (GMT's genes)
            row[names[-1]] = [c for c in cells[len(names) - 1 :] if c.strip() and c not in nulls]
        elif any(
            c.strip() for c in cells[len(names) :]
        ):  # empty surplus cells are a trailing delimiter: ignored
            row["__extra__"] = cells[len(names) :]
        yield row


def parse(lines, dialect: dict, field_names: list[str], limit: int | None = None) -> list[dict]:
    """The first `limit` rows (all if None) of iter_rows."""
    return list(itertools.islice(iter_rows(lines, dialect, field_names), limit))


def fit(rows: list[dict], field_names: list[str], dialect: dict) -> tuple[float, str]:
    """How well parsed rows match the croissant's fields: (score 0..1, explanation). Sparse rows are
    fine; rows with too few or too many cells, or column names the croissant doesn't have, aren't.
    """
    if not rows:
        return 0.0, "no rows"
    widths = [len([k for k in r if k != "__extra__"]) for r in rows]
    if dialect.get("columns") == "field_order":
        expected, named = len(dialect.get("column_names") or field_names), 1.0
    else:
        present = set().union(*(set(r) for r in rows)) - {"__extra__"}
        expected, named = max(widths), len(present & set(field_names)) / max(len(field_names), 1)
    if dialect.get("columns") == "field_order":
        first = {str(v).strip() for k, v in rows[0].items() if k != "__extra__" and v}
        if len(first & set(field_names)) >= max(2, len(field_names) // 2):
            return (
                0.0,
                "the first row holds the column names: the file has a header row, so field_order "
                "is wrong",
            )
    if dialect.get("csvw", {}).get("headerRowCount", 1) < 2:
        cells = [str(v).strip() for k, v in rows[0].items() if k != "__extra__" and v]
        robot = [c for c in cells if ROBOT_CELL.match(c)]
        # and one cell that only a template has: the ID keyword, a '%' or a split marker
        strong = any(c == "ID" or "%" in c or c.startswith("SPLIT=") for c in robot)
        if cells and strong and len(robot) >= 0.5 * len(cells):
            return (
                0.0,
                "the first data row is a ROBOT template row, not data (use a robot-template "
                "dialect)",
            )
    wide = sum(1 for r in rows if r.get("__extra__"))
    narrow = sum(1 for w in widths if w < expected)
    score = named * (1 - wide / len(rows)) * (1 - narrow / len(rows))
    names = (
        "columns by position (no header row)"
        if dialect.get("columns") == "field_order"
        else f"{100 * named:.0f}% of field names matched"
    )
    why = f"{names}; {wide} rows too wide; {narrow} rows too narrow"
    return score, why


def _lines(data: bytes, limit: int | None = None) -> list[str]:
    """Lines of a capped read. The last line is dropped only when `data` actually hit `limit`
    (so it may be a partial line); a file read in full, even one lacking a trailing newline,
    keeps its last line."""
    text_ = data.decode("utf-8", errors="replace")
    lines = text_.splitlines()
    if lines and not text_.endswith("\n") and limit is not None and len(data) >= limit:
        lines = lines[:-1]
    return lines


def _head(croissant: dict, rs: dict, max_bytes: int = 4_000_000) -> tuple[dict, list[str]]:
    import sample as smp

    dists = distribution_index(croissant)
    dist = dists[record_set_files(rs)[0]]
    return dist, _lines(smp.file_bytes(dist, dists, max_bytes, whole=False), limit=max_bytes)


def cmd_list(args) -> None:
    for name, d in registry().items():
        print(f"{name:12} {d['description']}")


def cmd_show(args) -> None:
    print(json.dumps(resolve(args.name), indent=1))


def _looks_like_header(lines: list[str]) -> bool:
    """Whether the first non-comment line names columns rather than holding data: its cells are
    all text, and some column below it holds numbers."""
    body = [ln for ln in lines if ln.strip() and not ln.startswith(("#", "!"))][:50]
    if len(body) < 3:
        return False
    delim = "\t" if body[0].count("\t") >= body[0].count(",") and "\t" in body[0] else ","
    rows = list(csv.reader(body, delimiter=delim))
    first, rest = rows[0], rows[1:]
    if not first or any(re.fullmatch(r"-?[\d.,]+(e-?\d+)?", c.strip()) for c in first if c.strip()):
        return False
    numeric = re.compile(r"-?\d+(\.\d+)?(e-?\d+)?")
    return any(
        sum(bool(len(r) > i and numeric.fullmatch(r[i].strip())) for r in rest) > len(rest) / 2
        for i in range(len(first))
    )


def _pick_record_set(croissant: dict, args) -> dict:
    if args.record_set:
        rs = next(
            (
                r
                for r in croissant.get("recordSet") or []
                if args.record_set in (r["@id"], text(r.get("name")))
            ),
            None,
        )
        return rs or sys.exit(f"No record set {args.record_set}")
    sets = layouts(croissant).get(args.layout) or sys.exit(f"No layout {args.layout}")
    return sets[0]


def cmd_detect(args) -> None:
    import sample as smp

    smp.parse_url_maps(args.url_map)
    croissant = load_croissant(args.croissant)
    rs = _pick_record_set(croissant, args)
    dist, lines = _head(croissant, rs, int(args.max_mb * 1e6))
    fields = file_columns(rs)
    positional = bool(fields) and all(re.fullmatch(r"column_\d+", f) for f in fields)
    header_like = _looks_like_header(lines)
    commented = bool(lines) and lines[0].startswith(("#", "!"))
    # among equal fits, prefer a dialect named for this knowledge base or file type (flybase-tsv for
    # FlyBase files)
    name, home = text(croissant.get("name")), text(croissant.get("url"))
    context = f"{dist.get('contentUrl')} {name} {home}".lower()
    results = []
    for name in registry():
        d = resolve(name)
        rows = parse(lines, d, fields, limit=200)
        score, why = fit(rows, fields, d)
        generic = name in GENERIC
        named = not generic and name.split("-")[0] in context
        skips = int(d.get("csvw", {}).get("skipRows", 0) or 0) > 0
        # a first row that names columns is skipped, not read as data; one that's data isn't
        right_first_row = skips == (header_like and (positional or d.get("columns") != "header"))
        # names that all match beat reading columns by position
        by_names = d.get("columns") == "header" and why.startswith("100%")
        # a file that starts with '#' or '!' is best read by a dialect that knows comment lines
        knows_comments = commented == bool(
            d.get("csvw", {}).get("commentPrefix") or d.get("header_in_comments")
        )
        results.append(
            (round(score, 2), right_first_row, knows_comments, by_names, named, generic, name, why)
        )
    results.sort(reverse=True)
    print(f"File: {dist.get('contentUrl')}")
    print("Dialects, best fit first (* named for this knowledge base or file type):")
    for score, _, _, _, named, _, name, why in results:
        print(f"  {score:4.2f} {'*' if named else ' '} {name:24}  {why}")
    best = results[0]
    print(
        f"\nBest: {best[6]} ({best[0]:.2f})"
        + (" — named for this knowledge base or file type" if best[4] else "")
    )
    if header_like and positional:
        print(
            "\nThe first row looks like a header, but the croissant names the fields column_1, "
            "column_2, …: the generator didn't use the header. Use a skip-header dialect "
            "(tsv-skip-header, csv-skip-header, or skipRows: 1 inline), report it as a generator "
            "problem, and put each column's header name in its description."
        )
    if len([r for r in results if r[0] == best[0]]) > 1:
        print(
            "\nSeveral dialects read the file equally well. Pick the one whose spec describes this "
            "file (the knowledge base's own format, or the file type), not just one that parses it."
        )
    if best[0] < 0.9:
        print(
            "\nNo named dialect fits well. Describe one inline in the plan (see "
            "reference/dialects.md), and note it for the skill's maintainers."
        )
    print("\nFirst lines:")
    for ln in lines[:4]:
        print(f"  {ln[:150]!r}")


def cmd_read(args) -> None:
    croissant = load_croissant(args.croissant)
    rs = next(
        (
            r
            for r in croissant.get("recordSet") or []
            if args.record_set in (r["@id"], text(r.get("name")))
        ),
        None,
    )
    rs or sys.exit("No such record set")
    dists = distribution_index(croissant)
    dialect = from_croissant(dists[record_set_files(rs)[0]])
    if not dialect:
        sys.exit("The file has no csvw:dialect in this croissant.")
    _, lines = _head(croissant, rs)
    rows = by_field_name(parse(lines, dialect, file_columns(rs), limit=args.n), rs)
    for r in rows:
        print(json.dumps(r, ensure_ascii=False)[:600])


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    p = sub.add_parser("show")
    p.add_argument("name")
    p.set_defaults(fn=cmd_show)
    p = sub.add_parser("detect")
    p.add_argument("croissant")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--layout")
    g.add_argument("--record-set", help="one record set (@id or name), for layouts that mix files")
    p.add_argument(
        "--url-map",
        action="append",
        metavar="OLD=NEW",
        help="replace OLD with NEW in data URLs, as in sample.py",
    )
    p.add_argument(
        "--max-mb",
        type=float,
        default=40,
        help="bytes to read: the head of a plain file, or the archive a member is in (default 40)",
    )
    p.set_defaults(fn=cmd_detect)
    p = sub.add_parser("read")
    p.add_argument("croissant")
    p.add_argument("--record-set", required=True)
    p.add_argument("-n", type=int, default=5)
    p.set_defaults(fn=cmd_read)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
