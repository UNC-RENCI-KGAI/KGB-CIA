#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests", "jsonpath-ng", "openpyxl", "truststore", "pypdf", "pyarrow", "xlrd"]
# ///
"""Show real values from a layout's files, so semantics are based on data, not column names.

    sample.py CROISSANT --layout L1a2b [-n 200] [--max-mb 40]
    sample.py CROISSANT --record-set RECORD_SET_ID_OR_NAME     # one particular file
    sample.py CROISSANT --record-set ID --profile COL1,COL2   # every distinct value, whole file
    sample.py CROISSANT --layout L1a2b --dialect gaf-2.2       # read with a named dialect
    sample.py CROISSANT --record-set-name 'curated.*tsv' --profile DirectEvidence   # by name
    sample.py CROISSANT --layout L1a2b --profile GeneID --shapes   # id families a column mixes

Reads the first rows of three of the layout's files (the newest-looking by name, the largest, the
smallest; --files for more) and prints, for each column: example values, how often it's empty, and
the shape of its values. CURIE prefixes are counted, list-like values are flagged, and bare
identifiers are reported so you can decide on an id prefix. Use vocab.py identify to match bare ids
to Bioregistry prefixes."""

import argparse
import csv
import gzip
import http.client
import io
import itertools
import json
import re
import sys
import tarfile
import zipfile
from collections import Counter
from pathlib import Path

import requests
import truststore
import urllib3
from jsonpath_ng.ext import parse as jsonpath_parse

sys.path.insert(0, str(Path(__file__).parent))
import dialects  # noqa: E402
from common import (  # noqa: E402
    CURIE,
    as_list,
    by_field_name,
    column_names,
    content_size,
    distribution_index,
    field_source,
    file_columns,
    layouts,
    load_croissant,
    record_set_files,
    text,
)

# Use the operating system's certificate store: certifi's bundle lacks some roots that
# knowledge-base servers chain to (data.glygen.org's emSign root), and every download then fails
# with CERTIFICATE_VERIFY_FAILED.
truststore.inject_into_ssl()

UA = {
    "User-Agent": "RoboCroissantBot/1.0 (+https://roboboulangerie.github.io/Boulangerie/crawler/)"
}
LIST_SEPS = ["|", ";", ","]
# file -> (rows wider than the header, rows narrower, rows read), from the last table read of each
# file
WIDTHS: dict[str, tuple[int, int, int]] = {}
TABLE_HEAD_BYTES = 4_000_000  # compressed bytes read from the start of a table file
NULL_MARKERS = {"n/a", "na", "-", ".", "none", "null", "nan", "unknown", "not available"}


# (old, new) substrings applied to every URL before download: --url-map, for a croissant whose data
# URLs point at a
# release that is gone (e.g. 202607 -> 202610). Whatever is learned this way must be noted in the
# plan's basis.
URL_MAP: list[tuple[str, str]] = []
TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")


def mapped(url: str) -> str:
    for old, new in URL_MAP:
        url = url.replace(old, new)
    return url


def parse_url_maps(items: list[str] | None) -> None:
    """OLD=NEW, or OLD=>NEW when the URLs themselves contain '=' (query strings)."""
    for item in items or []:
        old, sep, new = item.partition("=>") if "=>" in item else item.partition("=")
        if not sep or not old:
            raise SystemExit(f"--url-map takes OLD=NEW or OLD=>NEW, not {item!r}")
        if (
            new
            and not re.match(r"^[a-z]+://", new)
            and re.match(r"^[a-z]+://", old)
            and "=>" not in item
        ):
            raise SystemExit(
                f"--url-map {item!r}: the part after the first '=' isn't a URL; if the URLs "
                "contain '=', "
                "write OLD=>NEW"
            )
        URL_MAP.append((old, new))


class AccessDenied(requests.HTTPError):
    """The host wants a login or key (HTTP 401/403): every other file there will be refused too."""


class BotCheck(requests.HTTPError):
    """The host answered with a bot challenge instead of the file."""


CACHE_DIR = Path.home() / ".cache" / "croissant-semantics" / "downloads"
USE_CACHE = True  # False: don't read or write the cache (tests)
REFRESH = False  # sample.py --refresh: download again and replace the cached copy
MIN_INTERVAL = 1.0  # seconds between requests to one host from this process
DOWNLOAD_LIMIT_S = 1800  # give up on one download after this long
HOLD_S = 3600  # after a bot challenge or a 403, leave the host alone this long, across runs
_last_request: dict[str, float] = {}


def _hold_path(host: str) -> Path:
    return CACHE_DIR.parent / "refused" / host.replace(":", "_")


def _check_hold(host: str) -> None:
    """Refuse to ask a host that refused us within HOLD_S, in this process or an earlier one: a
    loop of fresh sample.py runs would otherwise keep provoking its bot check."""
    import time

    path = _hold_path(host)
    if not USE_CACHE or not path.exists():
        return
    left = HOLD_S - (time.time() - path.stat().st_mtime)
    if left <= 0:
        return
    reason = path.read_text().strip()
    raise (BotCheck if reason.startswith("bot") else AccessDenied)(
        f"{host} refused a request {(HOLD_S - left) / 60:.0f} min ago ({reason}); no requests go "
        f"there for another {left / 60:.0f} min. Work from cached files meanwhile. "
        f"(Delete {path} only once you know the host will answer again.)"
    )


def _hold(host: str, reason: str) -> None:
    if USE_CACHE:
        path = _hold_path(host)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(reason + "\n")


def _cache_path(*key) -> Path:
    import hashlib

    return CACHE_DIR / hashlib.sha256("|".join(map(str, key)).encode()).hexdigest()


def _cached(key: tuple, fetch) -> bytes:
    """fetch() once per key; kept on disk across runs (delete ~/.cache/croissant-semantics/downloads
    to clear)."""
    path = _cache_path(*key)
    if USE_CACHE and not REFRESH and path.exists():
        _index(path, key)
        return path.read_bytes()
    for attempt in range(
        3
    ):  # servers drop long downloads (IncompleteRead, reset connections): try again
        try:
            data = fetch()
            break
        except (
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ConnectionError,
            requests.exceptions.ReadTimeout,
        ) as e:
            if attempt == 2:
                raise
            print(f"[sample] {type(e).__name__}; retrying ({attempt + 2}/3)", file=sys.stderr)
    if USE_CACHE:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(path)
        _index(path, key)
    return data


_INDEXED: set[str] | None = None


def _index(path: Path, key: tuple) -> None:
    """index.tsv: cached file, then what it holds, so a download can be found by URL (grep URL
    index.tsv)."""
    global _INDEXED
    index = CACHE_DIR / "index.tsv"
    if _INDEXED is None:
        _INDEXED = (
            {ln.split("\t", 1)[0] for ln in index.read_text().splitlines()}
            if index.exists()
            else set()
        )
    if str(path) not in _INDEXED:
        with index.open("a") as fh:
            fh.write(f"{path}\t{' '.join(map(str, key))}\n")
        _INDEXED.add(str(path))


def _get(url: str):
    """A streamed GET that has been checked: paced per host, and failing clearly on logins, missing
    files and bot challenges, which some hosts answer with HTTP 202 and an empty body."""
    import time
    from urllib.parse import urlparse

    host = urlparse(url).netloc
    _check_hold(host)
    wait = _last_request.get(host, 0) + MIN_INTERVAL - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_request[host] = time.monotonic()
    r = requests.get(url, stream=True, timeout=60, headers=UA)
    challenge = (
        r.status_code in (202, 429)
        or "x-amzn-waf-action" in r.headers
        or "cf-mitigated" in r.headers
        or (
            r.status_code == 403
            and "challenge" in r.headers.get("content-type", "") + r.headers.get("server", "")
        )
    )
    if challenge:
        r.close()
        _hold(host, f"bot challenge, HTTP {r.status_code}")
        raise BotCheck(
            f"HTTP {r.status_code}: {host} answered with a bot challenge, not the file. Don't try "
            "to get "
            "past it. Stop sampling this host for a while, and sample one file at a time (no "
            "parallel runs "
            "against one host); files already downloaded are cached."
        )
    if r.status_code in (401, 403):
        r.close()
        if r.status_code == 403:  # also how hosts block automated requests; 401 is about the file
            _hold(host, "HTTP 403")
        raise AccessDenied(
            f"HTTP {r.status_code}: {url} needs a login or key, or the host is blocking automated "
            "requests; this data can't be sampled (see the croissant's conditionsOfAccess). Plan "
            "from documentation and cached files instead."
        )
    if r.status_code in (404, 410):
        r.close()
        raise requests.HTTPError(
            f"HTTP {r.status_code}: {url} is gone. If the knowledge base has moved on to a "
            "newer release, find its URL and pass --url-map OLD=NEW."
        )
    if r.status_code != 200:
        r.close()
        raise requests.HTTPError(f"HTTP {r.status_code} for {url}")
    path = urlparse(url).path.lower()
    if "text/html" in r.headers.get("content-type", "") and not path.endswith(
        (".html", ".htm", "/")
    ):
        r.close()
        raise requests.HTTPError(
            f"{url} returned a web page (text/html), not the file: the data URL is probably "
            "dead, and the site answers unknown paths with its portal page"
        )
    return r


def _local_path(url: str) -> Path | None:
    """A file:// URL (from --url-map OLD=file:///dir/) as a path: files fetched by hand are read in
    place."""
    return Path(url[len("file://") :]) if url.startswith("file://") else None


def _cached_larger(url: str, max_bytes: int, whole: bool) -> bytes | None:
    """A copy of url cached under a larger --max-mb that covers this request, so changing --max-mb
    doesn't download again: its first max_bytes, or all of it when it's the whole file."""
    index = CACHE_DIR / "index.tsv"
    if not (USE_CACHE and not REFRESH and index.exists()):
        return None
    for line in index.read_text().splitlines():
        path, _, key = line.partition("\t")
        parts = key.split(" ")
        if len(parts) != 4 or parts[0] != "fetch" or parts[1] != url:
            continue
        limit, was_whole = int(parts[2]), parts[3] == "True"
        p = Path(path)
        if limit < max_bytes or not p.exists():
            continue
        size = p.stat().st_size
        complete = was_whole or size < limit  # a stream that ended before its limit is whole
        if whole and not complete:
            continue
        if whole and size > max_bytes:
            raise SystemExit(
                f"{url} is larger than --max-mb ({max_bytes / 1e6:.0f} MB); it must be read "
                "whole. Raise --max-mb (on check.py too)."
            )
        with p.open("rb") as fh:
            return fh.read(max_bytes)
    return None


def fetch_bytes(url: str, max_bytes: int, whole: bool) -> bytes:
    """Download up to max_bytes (cached). If whole is set and the file is larger, fail rather than
    truncate."""
    url = mapped(url)
    if not _local_path(url) and not _cache_path("fetch", url, max_bytes, whole).exists():
        if (larger := _cached_larger(url, max_bytes, whole)) is not None:
            return larger
    if (local := _local_path(url)) is not None:
        size = local.stat().st_size
        if whole and size > max_bytes:
            raise SystemExit(
                f"{local} is larger than --max-mb ({max_bytes / 1e6:.0f} MB); it must be read whole"
            )
        with local.open("rb") as fh:
            return fh.read(max_bytes)

    def fetch():
        import time

        started = time.monotonic()
        with _get(url) as r:
            buf = bytearray()
            for chunk in r.iter_content(1 << 16):
                buf.extend(chunk)
                if (
                    time.monotonic() - started > DOWNLOAD_LIMIT_S
                ):  # a server trickling bytes would hold us forever
                    raise requests.exceptions.ReadTimeout(
                        f"{url}: still downloading after {DOWNLOAD_LIMIT_S} s"
                    )
                if len(buf) > max_bytes:
                    if whole:
                        raise SystemExit(
                            f"{url} is larger than --max-mb ({max_bytes / 1e6:.0f} MB); it must be "
                            "read "
                            "whole. Pick a smaller record set with --record-set, or raise --max-mb "
                            "(on check.py too)."
                        )
                    break
            if not buf:
                raise requests.HTTPError(f"{url} returned an empty body")
            return bytes(buf)

    return _cached(("fetch", url, max_bytes, whole), fetch)


def decompress_prefix(data: bytes, name: str) -> bytes:
    if name.endswith(".gz") or data[:2] == b"\x1f\x8b":
        if data[:2] != b"\x1f\x8b":
            return data  # served decompressed (Content-Encoding: gzip)
        d = gzip.GzipFile(fileobj=io.BytesIO(data))
        out = bytearray()
        try:
            while chunk := d.read(1 << 16):
                out.extend(chunk)
        except EOFError:
            pass  # truncated download: keep what decompressed
        except OSError as e:
            if not out:
                raise ValueError(f"{name} isn't valid gzip ({e})") from e
        return bytes(out)
    return data


class _Counted(io.RawIOBase):
    """A read-only stream that stops with SystemExit once more than `limit` bytes have been read."""

    def __init__(self, raw, limit: int, url: str):
        self.raw, self.limit, self.url, self.n = raw, limit, url, 0

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        data = self.raw.read(len(b))
        self.n += len(data)
        if self.n > self.limit:
            raise SystemExit(
                f"{self.url}: the member wasn't found in the first {self.limit / 1e6:.0f} MB "
                "(compressed) of the tar archive; raise --max-mb, or sample another record set"
            )
        b[: len(data)] = data
        return len(data)


def _tar_stream(raw) -> tarfile.TarFile:
    """A streaming tar reader over raw bytes, peeling off however many gzip layers there are
    (Harmonizome gzips some of its .tar.gz archives twice, which tarfile alone can't read)."""
    import bz2
    import lzma

    stream = io.BufferedReader(raw) if not isinstance(raw, io.BufferedReader) else raw
    while True:
        magic = stream.peek(6)[:6]
        if magic[:2] == b"\x1f\x8b":
            stream = io.BufferedReader(gzip.GzipFile(fileobj=stream, mode="rb"))
        elif magic[:3] == b"BZh":
            stream = io.BufferedReader(bz2.BZ2File(stream))
        elif magic == b"\xfd7zXZ\x00":
            stream = io.BufferedReader(lzma.LZMAFile(stream))
        else:
            return tarfile.open(fileobj=stream, mode="r|")


def tar_member(url: str, member: str, max_bytes: int) -> bytes:
    """Up to max_bytes of one tar member, streaming the archive and stopping once the member is read
    (cached)."""
    url = mapped(url)
    if (local := _local_path(url)) is not None:
        with local.open("rb") as fh, _tar_stream(fh) as t:
            for info in t:
                if info.isfile() and (info.name == member or info.name.endswith("/" + member)):
                    f = t.extractfile(info)
                    return f.read(max_bytes) if f else b""
            raise SystemExit(f"{member} not found in {local}")

    for name in {
        member,
        member.removeprefix("./"),
    }:  # passed earlier while streaming for another member
        hit = _cache_path("tar", url, name, max_bytes)
        if USE_CACHE and not REFRESH and hit.exists():
            return hit.read_bytes()

    def fetch():
        with _get(url) as r:
            # count the bytes on the wire: a .tar.gz served with Content-Encoding: gzip would
            # otherwise be
            # decompressed by requests and the limit would apply to the (much larger) tar
            r.raw.decode_content = False
            with _tar_stream(_Counted(r.raw, max_bytes, url)) as t:
                for info in t:
                    if not info.isfile():
                        continue
                    if info.name == member or info.name.endswith("/" + member):
                        f = t.extractfile(info)
                        return f.read(max_bytes) if f else b""
                    if USE_CACHE and info.size <= min(max_bytes, 50_000_000):
                        # a member passed on the way: cache it, since sampling often asks for its
                        # siblings next
                        f = t.extractfile(info)
                        if f is not None:
                            other = _cache_path("tar", url, info.name.removeprefix("./"), max_bytes)
                            other.parent.mkdir(parents=True, exist_ok=True)
                            other.write_bytes(f.read())
                            _index(other, ("tar", url, info.name.removeprefix("./"), max_bytes))
        raise SystemExit(f"{member} not found in {url}")

    return _cached(("tar", url, member, max_bytes), fetch)


def file_bytes(dist: dict, dists: dict, max_bytes: int, whole: bool) -> bytes:
    """Bytes of a distribution entry, opening the archive it's contained in if needed."""
    parents = [
        p["@id"] for p in as_list(dist.get("containedIn")) if isinstance(p, dict) and "@id" in p
    ]
    if parents:
        archive = dists[parents[0]]
        member = str(dist.get("contentUrl") or dist.get("name")).split("!/")[
            -1
        ]  # "archive.tar.gz!/member" form
        if str(archive.get("contentUrl", "")).lower().endswith(TAR_SUFFIXES):
            return decompress_prefix(
                tar_member(archive["contentUrl"], str(member), max_bytes), str(member)
            )
        data = fetch_bytes(archive["contentUrl"], max_bytes, whole=True)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
            match = next((n for n in names if n == member or n.endswith("/" + str(member))), None)
            if not match:
                raise SystemExit(f"{member} not found in {archive['contentUrl']}")
            with z.open(match) as f:
                raw = f.read(max_bytes)
        return decompress_prefix(raw, match)
    url = dist.get("contentUrl")
    if not url or not str(url).startswith("http"):
        raise SystemExit(f"No downloadable URL for {dist.get('@id')}")
    return decompress_prefix(fetch_bytes(url, max_bytes, whole), url)


def table_rows(
    data: bytes, dist: dict, columns: list[str], n: int, dialect: dict | None = None
) -> list[dict]:
    """Rows of a table file. With a dialect (given, or the file's own csvw:dialect) the file is read
    exactly as the dialect says; without one, the delimiter and header are guessed."""
    lines = data.decode("utf-8", errors="replace").splitlines()
    if lines and not data.endswith(b"\n"):
        lines = lines[:-1]  # last line may be cut off
    # JSON opens with { "key" or [ followed by a value; a table header like "[class]" doesn't
    if re.match(rb'\s*(\{\s*["}]|\[\s*[\[{"\]\d-])', data[:200]) and not str(
        dist.get("contentUrl") or ""
    ).lower().removesuffix(".gz").endswith((".json", ".jsonl")):
        raise ValueError(
            "the file is JSON, but the croissant describes a table (CSV/TSV): the URL may need a "
            "format "
            "parameter or Accept header to return the table, which is a pipeline problem"
        )
    head = data.lstrip()[:400]
    kind = (
        "XML"
        if head.startswith(b"<?xml") or re.match(rb"<[A-Za-z!]", head)
        else "RDF (Turtle or N-Triples)"
        if re.match(rb"(@prefix|@base|PREFIX|BASE)\s", head) or re.match(rb"<[^>]+>\s+<", head)
        else "FASTA"
        if head.startswith(b">") and b"\n" in head
        else None
    )
    if kind:
        raise ValueError(
            f"the file is {kind}, not a table; the scripts can't sample it. Say so in notes.md "
            "and check it another way if it matters (a small script in OUT/work/)"
        )
    if data[:4] == b"PK\x03\x04" or data[:16] == b"SQLite format 3\x00":
        raise ValueError(
            "the file is a spreadsheet, zip or SQLite database, but the croissant doesn't say so "
            "(encodingFormat); it can't be read as a table"
        )
    dialect = dialect or dialects.from_croissant(dist)
    if dialect:
        rows = dialects.parse(lines, dialect, columns, limit=n)
        expected = max((len([k for k in r if k != "__extra__"]) for r in rows), default=0)
        WIDTHS[str(dist.get("contentUrl") or dist.get("name") or "")] = (
            sum(1 for r in rows if r.get("__extra__")),
            sum(len([k for k in r if k != "__extra__"]) < expected for r in rows),
            len(rows),
        )
        if rows and dialect.get("columns", "header") == "header" and columns:
            named = set(rows[0]) - {"__extra__"}
            if len(named & set(columns)) < max(1, len(columns) // 2):
                raise ValueError(
                    f"the dialect's header row doesn't name the croissant's columns (it reads "
                    f"{sorted(named)[:6]}); the header may be a comment line or missing. Try "
                    "dialects.py detect"
                )
        return rows
    # A commented header line that names the columns (VCF's "#CHROM", CTD's "# Fields:" list) is the
    # header.
    header_line = None
    for ln in lines:
        if ln.startswith(("#", "!")):
            cand = ln.lstrip("#! ").strip()
            parts = re.split(r"\t|,", cand)
            if len(set(p.strip() for p in parts) & set(columns)) >= max(2, len(columns) // 2):
                header_line = (
                    ln.strip()
                )  # names are cleaned of comment marks below, unless the croissant keeps them
        else:
            break
    # blank lines stay until after parsing: inside a quoted field they're part of the value
    body = [ln for ln in lines if not ln.startswith(("#", "!"))]
    sample = [ln for ln in body if ln.strip()][:20]
    tabs, commas = sum(ln.count("\t") for ln in sample), sum(ln.count(",") for ln in sample)
    name = str(dist.get("contentUrl") or dist.get("name") or "")
    delim = "\t" if (tabs >= commas or ".vcf" in name or ".tsv" in name) and tabs else ","
    if header_line is not None:
        body = [header_line] + body
    # one reader over the lines, so quoted fields that span lines stay whole
    reader = list(
        itertools.islice(
            (r for r in csv.reader((ln + "\n" for ln in body), delimiter=delim) if r), n + 1
        )
    )
    if not reader:
        return []
    # a commented header ("#CHROM", MITAB "#ID(s) interactor A"): keep the '#' when the croissant's
    # column has it
    header = [
        h
        if h in columns
        else h.strip()
        if h.strip() in columns
        else h.strip().lstrip("\ufeff#! ").strip()
        for h in reader[0]
    ]
    if len(set(header) & set(columns)) >= max(1, len(columns) // 2):
        names, rows = header, reader[1:]
    else:  # no header row (e.g. names given in comments): map by position
        names, rows = columns, reader
        width = len([c for c in reader[0] if c.strip()]) if reader else 0
        if width and not (len(columns) // 2 <= len(reader[0]) and width <= len(columns)):
            raise ValueError(
                f"the first row neither names the croissant's columns nor has {len(columns)} cells "
                f"(it has {len(reader[0])}: {reader[0][:4]}); the file isn't what the croissant "
                "describes, or needs a dialect (dialects.py detect)"
            )
    out = [dict(zip(names, r, strict=False)) for r in rows[:n]]
    key = str(dist.get("contentUrl") or dist.get("name") or "")
    WIDTHS[key] = (
        sum(len(r) > len(names) and any(c.strip() for c in r[len(names) :]) for r in rows[:n]),
        sum(len(r) < len(names) for r in rows[:n]),
        len(rows[:n]),
    )
    if out and any("\t" in str(v) for r in out[:20] for v in r.values()):
        raise ValueError(
            "rows still contain tab characters after splitting; the delimiter guess is wrong"
        )
    return out


SPREADSHEET_TYPES = ("spreadsheetml", "ms-excel")
SQLITE_TYPES = ("sqlite",)


def file_kind(dist: dict, fields: list[dict]) -> str:
    """jsonl, json, xlsx, sqlite or table: how a record set's file is read."""
    name = str(dist.get("contentUrl") or dist.get("name") or "").lower()
    if any("jsonPath" in ((f.get("source") or {}).get("extract") or {}) for f in fields):
        formats = " ".join(map(str, as_list(dist.get("encodingFormat")))).lower()
        lines = (
            ".jsonl" in name
            or ".ndjson" in name
            or ("ndjson" in formats and ".json" not in name.replace(".jsonl", ""))
        )
        return "jsonl" if lines else "json"
    formats = " ".join(map(str, as_list(dist.get("encodingFormat")))).lower()
    if name.endswith((".xlsx", ".xlsm", ".xls")) or any(t in formats for t in SPREADSHEET_TYPES):
        return "xlsx"
    if name.endswith((".db", ".sqlite", ".sqlite3")) or any(t in formats for t in SQLITE_TYPES):
        return "sqlite"
    if name.endswith(".parquet") or "parquet" in formats:
        return "parquet"
    return "table"


def parquet_rows(data: bytes, n: int) -> list[dict]:
    """Rows of a parquet file (read whole: its schema is at the end). List columns come back as
    lists."""
    import pyarrow.parquet as pq

    try:
        table = pq.read_table(io.BytesIO(data))
    except Exception as e:  # noqa: BLE001 - pyarrow raises its own error types for bad files
        raise ValueError(f"not a readable parquet file ({type(e).__name__}: {e})") from e
    return table.slice(0, n).to_pylist()


def first_member(dist: dict, dists: dict, max_bytes: int) -> dict | None:
    """For a FileSet inside an archive with no member named by its fields, the first archive member
    its `includes` glob matches, as a distribution entry: sampling one stands for the set."""
    import fnmatch

    parents = [p["@id"] for p in as_list(dist.get("containedIn")) if isinstance(p, dict)]
    if not parents or parents[0] not in dists:
        return None
    archive = dists[parents[0]]
    url = str(archive.get("contentUrl") or "")
    if url.lower().endswith(TAR_SUFFIXES):
        names = _tar_names(url, max_bytes)
    else:
        with zipfile.ZipFile(io.BytesIO(fetch_bytes(url, max_bytes, whole=True))) as z:
            names = z.namelist()
    globs = [str(g) for g in as_list(dist.get("includes"))] or ["*"]
    hit = next((n for n in names if any(fnmatch.fnmatch(n, g) for g in globs)), None)
    if hit is None:
        return None
    print(f"[sample] reading {hit}, the first member of FileSet {dist.get('@id')}", file=sys.stderr)
    return {
        "@id": f"{dist['@id']}#{hit}",
        "contentUrl": hit,
        "containedIn": dist.get("containedIn"),
        "encodingFormat": dist.get("encodingFormat"),
    }


def source_file(rs: dict, dists: dict) -> tuple[dict | None, str]:
    """The file a record set reads, as a distribution entry: its FileObject, or for a FileSet that
    is a directory (contentUrl ending in '/') the member its fields name with extract.path. (None,
    why) when there isn't one."""
    fields = rs.get("field") or []
    kind, dist_id, extract = field_source(fields[0]) if fields else (None, None, {})
    if dist_id not in dists:
        return None, f"reads from a {kind or 'non-file source'}"
    dist = dists[dist_id]
    if kind == "fileObject":
        return dist, ""
    member = extract.get("path") or next(
        (field_source(f)[2].get("path") for f in fields if field_source(f)[2].get("path")), None
    )
    base = str(dist.get("contentUrl") or "")
    if not member:
        return None, "reads from a FileSet without naming a member (extract.path)"
    if dist.get("containedIn"):
        return {**dist, "@id": f"{dist_id}#{member}", "contentUrl": member}, ""
    if not base.endswith("/"):
        return None, "reads from a FileSet whose contentUrl isn't a directory"
    return {
        "@id": f"{dist_id}#{member}",
        "contentUrl": base + member,
        "name": member,
        "encodingFormat": dist.get("encodingFormat"),
    }, ""


def part_name(rs: dict) -> str:
    """The record set's name, from which the sheet or table it reads is found: generators write
    'file.xlsx — Sheet1', 'file.xlsx: Sheet1' and the like (see _pick_part)."""
    return text(rs.get("name"))


def _pick_part(hint: str, parts: list[str], kind: str) -> str:
    """The sheet or table named by a record set name: the name itself, or the longest part name the
    record set name ends with. With several parts and no match, fail rather than guess: reading
    the wrong sheet gives plausible-looking but wrong samples."""
    if hint in parts:
        return hint
    ends = [p for p in parts if hint.rstrip().endswith(p.strip())]
    if ends:
        return max(ends, key=len)
    if len(parts) == 1:
        return parts[0]
    raise ValueError(
        f"can't tell which {kind} the record set {hint!r} reads (its name doesn't end with one of "
        f"{parts[:20]}); sample it with --dump FILE --sheet NAME, and report the croissant's naming"
    )


def _header_name(cell, columns: list[str]) -> str:
    """A header cell as the croissant names it: exact when the croissant keeps its spaces ('Genus ',
    a line break), else stripped."""
    name = "" if cell is None else str(cell)
    return name if name in columns else name.strip()


def _named_rows(header_rows: list[list], columns: list[str], n: int) -> list[dict]:
    """Rows of a sheet: the header is the first of the top rows that names most of the croissant's
    columns."""
    want = set(columns)
    best = max(
        range(min(10, len(header_rows))),
        default=None,
        key=lambda i: len(want & {_header_name(c, columns) for c in header_rows[i]}),
    )
    if best is None:
        return []
    names = [_header_name(c, columns) for c in header_rows[best]]
    if not want & set(names):
        if not all(re.fullmatch(r"column_\d+", c) for c in columns):
            raise ValueError(
                "no row among the sheet's first 10 names the croissant's columns "
                f"({columns[:6]}); it may be the wrong sheet, or the header is elsewhere"
            )
        names, best = list(columns), -1  # no header row, fields named by position
    return [
        dict(zip(names, ["" if v is None else str(v) for v in row], strict=False))
        for row in header_rows[best + 1 : best + 1 + n]
    ]


def xlsx_rows(data: bytes, sheet: str | None, columns: list[str], n: int) -> list[dict]:
    import openpyxl

    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":  # a legacy Excel 97-2003 (.xls)
        import xlrd

        try:
            book = xlrd.open_workbook(file_contents=data)
        except (xlrd.XLRDError, xlrd.compdoc.CompDocError) as e:
            raise ValueError(f"not a readable legacy Excel (.xls) workbook ({e})") from e
        names = book.sheet_names()
        ws = (
            book.sheet_by_name(_pick_part(sheet, names, "sheet"))
            if sheet
            else book.sheet_by_index(0)
        )
        rows = [ws.row_values(i) for i in range(min(ws.nrows, n + 10))]
        return _named_rows(rows, columns, n)
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except (zipfile.BadZipFile, KeyError, OSError, ValueError) as e:
        raise ValueError(f"not a readable xlsx workbook ({e})") from e
    ws = wb[_pick_part(sheet, wb.sheetnames, "sheet")] if sheet else wb.worksheets[0]
    rows = []
    for row in ws.iter_rows(values_only=True):
        rows.append(list(row))
        if len(rows) >= n + 10:
            break
    return _named_rows(rows, columns, n)


def sqlite_rows(data: bytes, table: str | None, columns: list[str], n: int) -> list[dict]:
    import sqlite3
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        tmp.write(data)
        tmp.flush()
        con = sqlite3.connect(f"file:{tmp.name}?mode=ro", uri=True)
        try:
            tables = [
                r[0]
                for r in con.execute(
                    "select name from sqlite_master where type in ('table', 'view')"
                )
            ]
            try:
                table = _pick_part(table or "", tables, "table")
            except ValueError:  # by columns, for databases whose record sets don't name tables
                table = max(
                    tables,
                    key=lambda t: len(
                        set(columns) & {c[1] for c in con.execute(f'pragma table_info("{t}")')}
                    ),
                    default=None,
                )
            if table not in tables:
                raise ValueError(f"no table {table!r} (tables: {tables[:20]})")
            cur = con.execute(f'select * from "{table}" limit {int(n)}')
            names = [d[0] for d in cur.description]
            return [
                {k: "" if v is None else str(v) for k, v in zip(names, row, strict=False)}
                for row in cur
            ]
        finally:
            con.close()


def _record_root(paths: list[str]) -> str | None:
    """The shared path to the records ('$.response.docs[*]') if every field path goes through it."""
    roots = []
    for p in paths:
        i = p.rfind("[*]")
        if i < 0:
            return None
        roots.append(p[: i + 3])
    root = min(roots, key=len)
    return root if all(p.startswith(root) for p in paths) else None


def json_rows(data: bytes, fields: list[dict], n: int, lines: bool = False) -> list[dict]:
    """Rows from JSON or JSON Lines. Fields are read per record, so a record missing a key gives
    None for that field instead of shifting the other records' values. JSON Lines may be cut off:
    the last, partial line is dropped."""
    text_ = data.decode("utf-8", errors="replace")
    named = [
        (text(f.get("name")), (f.get("source") or {}).get("extract", {}).get("jsonPath"))
        for f in fields
    ]
    named = [(name, path) for name, path in named if path]
    if not lines:
        try:
            docs = [json.loads(text_)]
        except json.JSONDecodeError:
            lines = True
    if lines:
        complete = text_.splitlines()[: -1 if not text_.endswith("\n") else None]
        docs = [json.loads(ln) for ln in complete[:n] if ln.strip().startswith(("{", "["))]
        if docs and _record_root([path for _, path in named]) is None:
            # each line is one record, and the paths are relative to it ($.subject)
            exprs = {name: jsonpath_parse(path) for name, path in named}
            rows = []
            for doc in docs:
                hits = {name: [m.value for m in expr.find(doc)] for name, expr in exprs.items()}
                rows.append(
                    {k: None if not v else v[0] if len(v) == 1 else v for k, v in hits.items()}
                )
            return rows
    root = _record_root([path for _, path in named])
    if root:
        records = [m.value for doc in docs for m in jsonpath_parse(root).find(doc)][:n]
        rel = {
            name: jsonpath_parse("$" + path[len(root) :]) if path != root else None
            for name, path in named
        }
        rows = []
        for rec in records:
            row = {}
            for name, expr in rel.items():
                hits = [m.value for m in expr.find(rec)] if expr else [rec]
                row[name] = None if not hits else hits[0] if len(hits) == 1 else hits
            rows.append(row)
        return rows
    # no shared record path: fall back to reading each field's values in order
    columns = {
        name: [m.value for doc in docs for m in jsonpath_parse(path).find(doc)][:n]
        for name, path in named
    }
    length = max((len(v) for v in columns.values()), default=0)
    return [{k: (v[i] if i < len(v) else None) for k, v in columns.items()} for i in range(length)]


def shape(values: list, name: str = "") -> str:
    vals = [v for v in values if v not in (None, "", [], {})]
    if not vals:
        return "always empty in sample"
    notes = []
    flat = []
    lists = 0
    for v in vals:
        if isinstance(v, list):
            lists += 1
            flat.extend(v)
        else:
            flat.append(v)
    if lists:
        notes.append(f"JSON lists in {lists}/{len(vals)} values (no list_separator needed)")
    strs = [json.dumps(v) if isinstance(v, (dict, list)) else str(v) for v in flat]
    nulls = Counter(x for x in strs if x.strip().lower() in NULL_MARKERS)
    if nulls.get("-") and {x.strip() for x in strs} <= {"+", "-", ".", ""}:
        del nulls["-"]  # a strand column: '-' is the minus strand, not a missing value
        notes.append("strand codes (+/-)")
    if nulls:
        notes.append(
            "null markers "
            + ", ".join(f"{k!r} ({c})" for k, c in nulls.most_common())
            + " (declare with null_values)"
        )
        strs = [x for x in strs if x.strip().lower() not in NULL_MARKERS]
        if not strs:
            return "; ".join(notes)

    def looks_listed(x, sep):
        x = re.sub(
            r"\[[^\]]*\]|\([^)]*\)", "", x
        )  # separators inside [...] or (...) aren't list separators
        parts = [p.strip() for p in x.split(sep)]
        if len(parts) < 2 or not all(parts):
            return False
        # commas also appear in names and prose (1,1,1-trichloroethane): only count them when the
        # parts are short
        # tokens of more than one character
        return sep != "," or all(1 < len(p) <= 30 and " " not in p for p in parts)

    seps = [s for s in LIST_SEPS if sum(looks_listed(x, s) for x in strs) >= max(2, len(strs) // 3)]
    if seps and not all(re.fullmatch(r"[-+.\deE,]+", x) for x in strs):
        notes.append(f"looks like a list joined by {seps[0]!r}")
        strs = [p.strip() for x in strs for p in x.split(seps[0]) if p.strip()]
    curies = Counter(
        m.group(1) for x in strs if (m := CURIE.match(x)) and not x.startswith(("http:", "https:"))
    )
    urls = sum(x.startswith(("http://", "https://")) for x in strs)
    if curies and sum(curies.values()) >= 0.6 * len(strs):
        notes.append(
            "CURIEs, prefixes " + ", ".join(f"{p} ({c})" for p, c in curies.most_common(6))
        )
    elif urls >= 0.6 * len(strs):
        notes.append("URLs")
    elif all(re.fullmatch(r"-?\d+", x) for x in strs):
        idish = re.search(
            r"(^|[^a-z])(id|ids|identifier|accession)s?$", name, re.I
        ) or name.lower().endswith("id")
        notes.append("integers" + (" (name suggests bare numeric ids)" if idish else ""))
    elif all(re.fullmatch(r"-?\d*\.?\d+([eE][-+]?\d+)?", x) for x in strs):
        notes.append("numbers")
    elif all(re.fullmatch(r"[A-Za-z]+[_-]?\d+[\w.-]*", x) for x in strs):
        notes.append("bare identifiers like " + strs[0] + " (check with vocab.py identify)")
    distinct = len(set(strs))
    notes.append(
        f"{distinct} distinct of {len(strs)}"
        + (" (few: maybe codes)" if distinct <= 12 and len(strs) >= 30 else "")
    )
    return "; ".join(notes)


_DATES = [  # (pattern, function from the match to YYYYMMDD), most specific first
    (
        re.compile(r"(?<!\d)(20\d\d|19\d\d)[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?!\d)"),
        lambda m: m[1] + m[2] + m[3],
    ),
    (
        re.compile(r"(?<!\d)(0[1-9]|1[0-2])[-_.](0[1-9]|[12]\d|3[01])[-_.](\d{4}|\d{2})(?!\d)"),
        lambda m: (m[3] if len(m[3]) == 4 else "20" + m[3]) + m[1] + m[2],
    ),
    (
        re.compile(r"(?<!\d)(20\d\d|19\d\d)[-_.]?(0[1-9]|1[0-2])(?!\d)"),
        lambda m: m[1] + m[2] + "00",
    ),
]


def name_date(name: str) -> str:
    """The latest date written in a file name (YYYY-MM-DD, YYYYMMDD, MM-DD-YY, YYYYMM ...), as
    YYYYMMDD; '' if none."""
    found = [fn(m) for pattern, fn in _DATES for m in pattern.finditer(name)]
    return max(found, default="")


MIN_SAMPLE_BYTES = 2000


def pick_record_sets(candidates: list[dict], dists: dict, k: int) -> list[dict]:
    """Up to k record sets that differ: the newest-looking (last by name), the largest and the
    smallest file. None when there are no candidates.

    The smallest file alone is often an old snapshot or a filtered subset, so its values aren't
    representative."""

    if not candidates:
        return []

    def size_of(rs):
        sizes = [content_size(dists.get(i, {})) for i in record_set_files(rs)]
        return max((s for s in sizes if s), default=0)

    # files with their own URL first: a member of a huge archive (a 9 GB tar) is slow to reach
    direct = [
        rs
        for rs in candidates
        if not any(dists.get(i, {}).get("containedIn") for i in record_set_files(rs))
    ]
    candidates = direct or candidates
    # files of a few hundred bytes are usually a header and nothing else: skip them while there are
    # bigger ones
    sized = [rs for rs in candidates if size_of(rs) == 0 or size_of(rs) >= MIN_SAMPLE_BYTES]
    candidates = sized or candidates
    by_size = sorted(candidates, key=size_of)
    by_name = sorted(
        candidates, key=lambda rs: (name_date(text(rs.get("name"))), text(rs.get("name")))
    )
    order = [by_name[-1], by_size[-1], by_size[0]] + by_size[1:-1]
    out = []
    for rs in order:
        if rs["@id"] not in {r["@id"] for r in out}:
            out.append(rs)
    return out[:k]


def sample_rows(
    croissant: dict, candidates: list[dict], n: int, max_bytes: int, k: int = 3, dialect=None
):
    """Rows from up to k record sets of a layout: ([(record set, url, rows)], [errors]). A dialect
    (a plan's `dialect` value), when given, overrides each file's own (for checking a plan before
    it's applied)."""
    dists = distribution_index(croissant)
    got, errors = [], []
    failed_archives: set[str] = (
        set()
    )  # an archive that failed once fails for every member: don't download it again
    failures = 0  # files that couldn't be read (a file with a header and no rows isn't one)
    for rs in pick_record_sets(candidates, dists, len(candidates)):
        if len(got) >= k:
            break
        if failures >= k + 5:
            errors.append(
                f"stopped after {failures} failures; sample particular files with --record-set"
            )
            break
        fields = rs.get("field") or []
        dist, why = source_file(rs, dists)
        if dist is None and "without naming a member" in why:
            fs = dists.get(record_set_files(rs)[0]) if record_set_files(rs) else None
            try:
                dist = first_member(fs, dists, max_bytes) if fs else None
            except (requests.RequestException, zipfile.BadZipFile, tarfile.TarError) as e:
                why += f" (and its archive couldn't be listed: {e})"
        if dist is None:
            errors.append(f"{rs.get('@id')}: {why}; can't sample it directly")
            failures += 1
            continue
        archive = next(
            (
                p["@id"]
                for p in as_list(dist.get("containedIn"))
                if isinstance(p, dict) and "@id" in p
            ),
            None,
        )
        if archive in failed_archives:
            continue
        kind = file_kind(dist, fields)
        whole = kind not in ("table", "jsonl")
        try:
            # Plain tables only need their first rows; JSON, spreadsheets, databases and archives
            # must be read whole
            # (up to max_bytes).
            limit = max_bytes if whole or archive else min(max_bytes, TABLE_HEAD_BYTES)
            size = content_size(dists[archive]) if archive else content_size(dist)
            print(
                f"[sample] reading {dist.get('contentUrl')}"
                + (f" from {dists[archive].get('contentUrl')}" if archive else "")
                + (f" ({size / 1e6:.0f} MB)" if size else "")
                + f", up to {limit / 1e6:.0f} MB",
                file=sys.stderr,
            )
            data = file_bytes(dist, dists, limit, whole=whole)
            file_dialect = dialects.for_file(dialect, dist) if dialect else None
            if kind in ("json", "jsonl"):
                rows = json_rows(data, fields, n, lines=kind == "jsonl")
            elif kind == "parquet":
                rows = by_field_name(parquet_rows(data, n), rs)
            elif kind == "xlsx":
                rows = by_field_name(xlsx_rows(data, part_name(rs), file_columns(rs), n), rs)
            elif kind == "sqlite":
                rows = by_field_name(sqlite_rows(data, part_name(rs), file_columns(rs), n), rs)
            else:
                rows = by_field_name(table_rows(data, dist, file_columns(rs), n, file_dialect), rs)
        except (
            BotCheck,
            AccessDenied,
        ) as e:  # the host refuses us: every other file there would be too
            errors.append(f"{rs.get('@id')}: {e}")
            break
        except (
            requests.RequestException,
            zipfile.BadZipFile,
            tarfile.TarError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            ValueError,
        ) as e:
            errors.append(f"{rs.get('@id')}: {type(e).__name__}: {e}")
            failures += 1
            if archive:
                failed_archives.add(archive)
            continue
        except SystemExit as e:  # size limit for files that must be read whole
            errors.append(f"{rs.get('@id')}: {e}")
            failures += 1
            if archive:
                failed_archives.add(archive)
            continue
        if rows:
            got.append((rs, dist.get("contentUrl") or dist.get("name"), rows))
        else:
            errors.append(f"{rs.get('@id')}: the file has no data rows (a header only, or empty)")
            # half a failure: many empty files in a row shouldn't mean streaming an archive per file
            failures += 0.5
    return got, errors


def _guessed_rows(lines, columns: list[str], field_names: list[str]):
    """Rows of a table whose dialect isn't known: guess the delimiter and find the header. Leading
    comment lines are read one by one (one may name the columns); the rest goes through one
    csv.reader, so quoted fields that span lines stay whole."""
    names, delim = None, None
    lines = iter(lines)
    first = None
    for i, ln in enumerate(lines):
        ln = ln.rstrip("\r\n")
        if i == 0:
            ln = ln.removeprefix("\ufeff")
        if not ln.strip():
            continue
        if ln.startswith(("#", "!")):
            cand = ln.lstrip("#! ")
            if sum(c in cand for c in columns) == len(columns) and names is None:
                delim = "\t" if "\t" in cand else ","
                names = next(csv.reader([cand], delimiter=delim))
            continue
        first = ln
        break
    if first is None:
        return
    if delim is None:
        delim = "\t" if first.count("\t") >= first.count(",") else ","
    rest = itertools.chain([first + "\n"], (ln.rstrip("\r\n") + "\n" for ln in lines))
    for row in csv.reader(rest, delimiter=delim):
        if not row or (len(row) == 1 and not row[0].strip()):
            continue
        if names is None:
            if set(columns) <= set(row):
                names = row
                continue
            names = field_names
        yield dict(zip(names, row, strict=False))


_TAG = re.compile(
    r"^([^:\s]+(?: [^:\s]+)*?):"
)  # 'MONDO:' / MITAB 'entrez gene/locuslink:' / 'psi-mi:'


def _prefix_of(value: str) -> str:
    if value.startswith(("http://", "https://")):  # an IRI: its namespace, up to the last / or #
        cut = max(value.rfind("#"), value.rfind("/"))
        return value[: cut + 1] if cut > value.find("//") + 1 else "<url>"
    if value.startswith("_:"):
        return "_: (blank node)"
    kv = re.match(
        r"^([\w .-]+)=", value
    )  # key=value lists (GPI properties: 'db_subset=Swiss-Prot')
    if kv and ":" not in kv.group(1):
        return f"{kv.group(1)}="
    m = _TAG.match(value)
    return f"{m.group(1)}:" if m else "<no prefix>"


def _shape_of(value: str) -> str:
    """A value's pattern: runs of digits become 9 (D012345 -> D9, C0123 -> C9, 4524 -> 9)."""
    return re.sub(r"\d+", "9", value)


def profile(
    croissant: dict,
    record_sets: list[dict],
    columns: list[str],
    top: int,
    dialect,
    max_bytes: int,
    prefixes=None,
    by: str | None = None,
    shapes=None,
    split: str | None = None,
    stop_mb: float | None = None,
) -> None:
    """Count every distinct value of the given columns over whole files (several record sets add
    up). Plain files are streamed; archive members, JSON, spreadsheets and databases are read up to
    max_bytes. prefixes / shapes: a set of columns (or True for all) whose values are counted by
    CURIE prefix or tag, or by pattern (digits as 9), instead of whole values. by: count each value
    together with another column's value (a cross-tab). split: a list separator, so each item of a
    joined cell is counted. stop_mb: stop streaming each file after this many MB of text."""
    dists = distribution_index(croissant)
    # columns by their field names; a name the croissant writes with trailing spaces matches its
    # stripped form, and an unknown name is an error rather than a column of empty values
    known = {n for rs in record_sets for n in column_names(rs)}
    resolved = []
    for c in columns:
        match = c if c in known else next((n for n in known if n.strip() == c.strip()), None)
        if match is None:
            sys.exit(f"--profile: no column {c!r} in these record sets; they have {sorted(known)}")
        resolved.append(match)
    columns = resolved
    if by and by not in known:
        sys.exit(f"--by: no column {by!r} in these record sets")
    counts = {c: Counter() for c in columns}

    def count(rec: dict) -> None:
        for c in columns:
            v = rec.get(c)
            items = v if isinstance(v, list) else [v]
            if split:
                items = [
                    p.strip() for i in items if i not in (None, "") for p in str(i).split(split)
                ] or [None]
            for item in items:
                key = "" if item is None else str(item)
                if key and (prefixes is True or (prefixes and c in prefixes)):
                    key = _prefix_of(key)
                elif key and (shapes is True or (shapes and c in shapes)):
                    key = _shape_of(key)
                if by:
                    other = rec.get(by)
                    other = "<empty>" if other in (None, "") else other
                    key = f"{key or '<empty>'}  |  {by}={other}"
                counts[c][key] += 1

    skipped = []
    for rs in record_sets:
        try:
            _profile_one(rs, dists, columns, dialect, max_bytes, count, stop_mb)
        except (http.client.HTTPException, urllib3.exceptions.HTTPError) as e:
            # the server closed a long stream: what was counted so far stands
            print(
                f"(connection dropped while reading {text(rs.get('name'))} ({type(e).__name__}); "
                "its counts cover only the part read)"
            )
        except (
            SystemExit,
            ValueError,
            OSError,
            requests.RequestException,
            zipfile.BadZipFile,
            tarfile.TarError,
        ) as e:
            if isinstance(e, BotCheck):
                raise
            skipped.append(f"{text(rs.get('name'))}: {str(e)[:160]}")
            print(f"(skipped {skipped[-1]})")
    print()
    if skipped and len(skipped) == len(record_sets):
        sys.exit("No file could be profiled.")
    for c in columns:
        what = (
            "prefixes"
            if prefixes is True or (prefixes and c in prefixes)
            else "shapes"
            if shapes is True or (shapes and c in shapes)
            else "values"
        )
        print(f"- {c}: {len(counts[c])} distinct {what}" + (f" by {by}" if by else ""))
        for v, n in counts[c].most_common(top):
            print(f"    {n:9}  {v[:140] if v else '<empty>'!r}")


def _capped(lines, limit: float | None, stopped: list):
    """The lines until `limit` characters have been read (None: all of them)."""
    read = 0
    for ln in lines:
        read += len(ln)
        if limit is not None and read > limit:
            stopped.append(read)
            return
        yield ln


def _profile_one(
    rs: dict, dists: dict, columns: list[str], dialect, max_bytes: int, count, stop_mb=None
) -> None:
    """Count one record set's file into `count`."""
    dist, why = source_file(rs, dists)
    if dist is None and "without naming a member" in why and record_set_files(rs):
        dist = first_member(dists[record_set_files(rs)[0]], dists, max_bytes)
    if dist is None:
        raise ValueError(f"can't profile it: it {why}")
    fields = rs.get("field") or []
    file_dialect = dialects.for_file(dialect, dist) if dialect else dialects.from_croissant(dist)
    names = file_columns(rs)  # read by the columns the fields extract, counted by field name
    to_name = dict(zip(names, column_names(rs), strict=True))
    columns = [next((c2 for c2, n in to_name.items() if n == c), c) for c in columns]
    field_count = count

    def count(rec: dict) -> None:  # noqa: F811 - re-keys rows, then counts
        field_count({to_name.get(k, k): v for k, v in rec.items()})

    kind = file_kind(dist, fields)
    total = 0
    if kind in ("xlsx", "sqlite", "json", "parquet"):
        data = file_bytes(dist, dists, max_bytes, whole=True)
        if kind == "json":
            recs = json_rows(data, fields, 10**9)
        elif kind == "parquet":
            recs = parquet_rows(data, 10**9)
        else:
            recs = (xlsx_rows if kind == "xlsx" else sqlite_rows)(data, part_name(rs), names, 10**9)
        for rec in recs:
            count(rec)
        print(
            f"Profiled {len(recs)} rows of {dist.get('contentUrl')}"
            + (f" ({part_name(rs)})" if kind in ("xlsx", "sqlite") else "")
        )
        return
    if any(isinstance(p, dict) for p in as_list(dist.get("containedIn"))):
        data = file_bytes(dist, dists, max_bytes, whole=False)
        text_stream, where = (
            io.StringIO(data.decode("utf-8", errors="replace")),
            str(dist.get("contentUrl")),
        )
        if len(data) >= max_bytes:
            where += (
                f" (only the member's first {max_bytes / 1e6:.0f} MB; raise --max-mb for all of it)"
            )
        r = None
    elif (local := _local_path(mapped(str(dist.get("contentUrl"))))) is not None:
        fh = local.open("rb")
        stream = gzip.GzipFile(fileobj=fh) if local.name.endswith(".gz") else fh
        text_stream, where, r = (
            io.TextIOWrapper(stream, encoding="utf-8", errors="replace"),
            str(local),
            None,
        )
    else:
        url = mapped(str(dist.get("contentUrl")))
        r = _get(url)
        raw = r.raw
        raw.decode_content = True
        raw.auto_close = (
            False  # urllib3 closes a fully read response, which io.TextIOWrapper can't handle
        )
        stream = (
            gzip.GzipFile(fileobj=raw)
            if url.endswith(".gz") and r.headers.get("content-encoding") != "gzip"
            else raw
        )
        text_stream, where = io.TextIOWrapper(stream, encoding="utf-8", errors="replace"), url
    stopped: list = []
    text_stream = _capped(text_stream, stop_mb * 1e6 if stop_mb else None, stopped)
    try:
        if kind == "jsonl":
            exprs = {
                text(f.get("name")): jsonpath_parse(field_source(f)[2]["jsonPath"])
                for f in fields
                if field_source(f)[2].get("jsonPath")
            }
            for ln in text_stream:
                if not ln.strip():
                    continue
                doc = json.loads(ln)
                hits = {k: [m.value for m in e.find(doc)] for k, e in exprs.items()}
                count({k: None if not v else v[0] if len(v) == 1 else v for k, v in hits.items()})
                total += 1
        else:
            rows = (
                dialects.iter_rows(text_stream, file_dialect, names)
                if file_dialect
                else _guessed_rows(text_stream, columns, names)
            )
            for rec in rows:
                total += 1
                count(rec)
    finally:
        if r is not None:
            r.close()
    print(
        f"Profiled {total} rows of {where}"
        + (f" (stopped after {stop_mb:g} MB: counts cover only the start)" if stopped else "")
    )


def _tar_names(url: str, max_bytes: int) -> list[str]:
    """Member names of a tar archive, read as far as max_bytes allows."""
    url = mapped(url)
    names = []
    if (local := _local_path(url)) is not None:
        with local.open("rb") as fh, _tar_stream(fh) as t:
            return [i.name for i in t if i.isfile()][:500]
    with _get(url) as r:
        r.raw.decode_content = False
        try:
            with _tar_stream(_Counted(r.raw, max_bytes, url)) as t:
                for info in t:
                    if info.isfile():
                        names.append(info.name)
        except SystemExit:
            names.append(f"(listing stopped at --max-mb {max_bytes / 1e6:g})")
    return names[:500]


def dump(
    croissant: dict,
    which: str,
    member: str | None,
    sheet: str | None,
    max_bytes: int,
    out: str | None = None,
):
    """Print a distribution (or a member of an archive distribution, or a sheet) as text, or write
    it to `out` (a path in OUT/work/, for reading with other tools)."""
    dists = distribution_index(croissant)
    dist = dists.get(which) or next(
        (d for d in dists.values() if which in (text(d.get("name")), d.get("contentUrl"))), None
    )
    if dist is None:
        sys.exit(f"No distribution {which!r} (give its @id, name or contentUrl)")
    if member:
        dist = {"@id": f"{dist['@id']}!/{member}", "contentUrl": member,
                "containedIn": {"@id": dist["@id"]}}  # fmt: skip
    try:
        data = file_bytes(dist, dists, max_bytes, whole=False)
    except SystemExit as e:
        url = str(dists[dist["containedIn"]["@id"]]["contentUrl"]) if member else ""
        if member and url.lower().endswith(TAR_SUFFIXES):
            sys.exit(f"{e}. Members: {', '.join(_tar_names(url, max_bytes)[:200])}")
        if member:
            with zipfile.ZipFile(io.BytesIO(fetch_bytes(url, max_bytes, whole=True))) as z:
                sys.exit(f"{e}. Members: {', '.join(z.namelist()[:200])}")
        raise
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(data)
        print(
            f"Wrote {len(data):,} bytes to {out}"
            + (" (cut at --max-mb)" if len(data) >= max_bytes else "")
        )
        return
    if data[:5] == b"%PDF-":
        import pypdf

        try:
            reader = pypdf.PdfReader(io.BytesIO(data))
        except Exception as e:  # noqa: BLE001 - pypdf raises many types for damaged files
            sys.exit(f"A PDF that couldn't be read ({type(e).__name__}: {e}); raise --max-mb?")
        for i, page in enumerate(reader.pages):
            print(f"--- page {i + 1}")
            print(page.extract_text() or "")
        return
    if data[:4] == b"PK\x03\x04" and not member:
        import openpyxl

        try:
            book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except (zipfile.BadZipFile, KeyError, OSError):  # a zip archive, not a spreadsheet
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    names = ", ".join(z.namelist()[:200])
            except zipfile.BadZipFile:
                sys.exit(f"The file is cut off at --max-mb {max_bytes / 1e6:g}; raise it")
            sys.exit(f"An archive; pick a file with --member: {names}")
        if not sheet:
            print("Sheets:", ", ".join(book.sheetnames))
            return
        if sheet not in book.sheetnames:
            sys.exit(f"No sheet {sheet!r}. Sheets: {', '.join(book.sheetnames)}")
        for row in book[sheet].iter_rows(values_only=True):
            print("\t".join("" if v is None else str(v) for v in row))
        return
    sys.stdout.write(data.decode("utf-8", errors="replace"))
    if len(data) >= max_bytes:
        print(f"\n(cut at --max-mb {max_bytes / 1e6:g})")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("croissant")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--layout")
    g.add_argument(
        "--record-set",
        action="append",
        help="a record set id or name (repeatable: --profile adds the files up)",
    )
    g.add_argument(
        "--record-set-name",
        metavar="REGEX",
        help=(
            "every record set whose name matches (avoids building --record-set lists in the shell)"
        ),
    )
    g.add_argument(
        "--dump",
        metavar="FILE",
        help="print a file as text (a distribution's @id, name or URL), to read documentation "
        "the knowledge base ships: a README in an archive (with --member), a sheet of a "
        "spreadsheet (with --sheet; without it, the sheet names). Up to --max-mb",
    )
    ap.add_argument("--member", help="with --dump: the archive member to print (zip or tar)")
    ap.add_argument("--sheet", help="with --dump: the spreadsheet sheet to print, tab-separated")
    ap.add_argument(
        "-o",
        "--out",
        metavar="PATH",
        help="with --dump: write the file's bytes here (in OUT/work/) instead of printing them",
    )
    ap.add_argument("-n", type=int, default=200, help="rows to read per file (default 200)")
    ap.add_argument(
        "--files",
        default="3",
        help="files to sample or profile (default 3: the newest-looking by name, the largest, the "
        "smallest), or 'all'",
    )
    ap.add_argument("--max-mb", type=float, default=40)
    ap.add_argument("--examples", type=int, default=8)
    ap.add_argument(
        "--per-file",
        action="store_true",
        help="show each sampled file's columns separately, for files of one layout that differ "
        "(one id "
        "kind per file)",
    )
    ap.add_argument(
        "--profile",
        help="comma-separated columns: read whole files of the given record sets (or, with "
        "--layout, --files of its record sets) and count every distinct value, for "
        "code columns, where a sample misses values",
    )
    ap.add_argument("--top", type=int, default=60, help="values to list per column with --profile")
    ap.add_argument(
        "--prefixes",
        nargs="?",
        const="*",
        metavar="COLS",
        help="with --profile: count each value's CURIE prefix or tag (MONDO:, "
        "'entrez gene/locuslink:', "
        "'key=') instead of the value, for all profiled columns or only these (comma-separated)",
    )
    ap.add_argument(
        "--shapes",
        nargs="?",
        const="*",
        metavar="COLS",
        help="with --profile: count each value's pattern, digits as 9 (D9, C9, 9), to see "
        "which id families "
        "a bare-id column mixes; all profiled columns or only these",
    )
    ap.add_argument(
        "--by",
        metavar="COL",
        help="with --profile: count each value together with COL's value "
        "(a cross-tab, e.g. which object types each code occurs with)",
    )
    ap.add_argument(
        "--split",
        metavar="SEP",
        help="with --profile: split each value on SEP and count the items (list columns)",
    )
    ap.add_argument(
        "--stop-mb",
        type=float,
        help="with --profile: stop streaming each file after this many MB of text, for files of "
        "many GB (the counts then cover only the start of the file)",
    )
    ap.add_argument(
        "--url-map",
        action="append",
        metavar="OLD=NEW",
        help="replace OLD with NEW in data URLs before downloading (repeatable), e.g. when the "
        "croissant points at a release that's gone; say so in the plan's basis. NEW may be "
        "file:///dir/ to read files you fetched yourself",
    )
    ap.add_argument(
        "--dialect",
        help="read table files with this dialect instead of guessing: a name from "
        'dialects.json, or an inline dialect as JSON (\'{"based_on": "tsv", ...}\')',
    )
    ap.add_argument(
        "--refresh", action="store_true", help="download again instead of using cached files"
    )
    args = ap.parse_args()
    if args.files != "all" and not args.files.isdigit():
        sys.exit("--files takes a number or 'all'")
    args.files = 10**9 if args.files == "all" else int(args.files)
    dialect = (
        json.loads(args.dialect)
        if args.dialect and args.dialect.lstrip().startswith("{")
        else args.dialect
    )
    if dialect:
        try:
            dialects.check_spec(dialect)
        except KeyError as e:
            sys.exit(f"--dialect: {e.args[0]}")
    parse_url_maps(args.url_map)
    global REFRESH
    REFRESH = args.refresh

    croissant = load_croissant(args.croissant)
    if args.dump:
        dump(croissant, args.dump, args.member, args.sheet, int(args.max_mb * 1e6), args.out)
        return
    if args.record_set_name:
        candidates = [
            rs
            for rs in croissant.get("recordSet") or []
            if re.search(args.record_set_name, text(rs.get("name")))
        ]
        if not candidates:
            sys.exit(f"No record set name matches {args.record_set_name!r}")
        args.record_set = [rs["@id"] for rs in candidates] if args.profile else None
    elif args.layout:
        candidates = layouts(croissant).get(args.layout) or sys.exit(f"No layout {args.layout}")
    else:
        by_key = {
            k: rs
            for rs in croissant.get("recordSet") or []
            for k in (rs.get("@id"), text(rs.get("name")))
        }
        missing = [k for k in args.record_set if k not in by_key]
        if missing:
            sys.exit(f"No such record set: {', '.join(missing)}")
        candidates = [by_key[k] for k in args.record_set]
    if args.profile:
        chosen = (
            candidates
            if args.record_set
            else pick_record_sets(candidates, distribution_index(croissant), args.files)
        )
        # --per-file: each record set profiled on its own (ids unique per file, codes per file)
        for part in [[rs] for rs in chosen] if args.per_file else [chosen]:
            if args.per_file:
                print(f"\n== {text(part[0].get('name'))}")
            _run_profile(croissant, part, args, dialect)
        return
    got, errors = sample_rows(
        croissant, candidates, args.n, int(args.max_mb * 1e6), args.files, dialect
    )
    if not got:
        sys.exit("Could not sample:\n  " + "\n  ".join(errors))
    _show_sample(got, errors, args, candidates)


def _run_profile(croissant: dict, chosen: list[dict], args, dialect) -> None:
    profile(
        croissant,
        chosen,
        [c.strip() for c in args.profile.split(",")],
        args.top,
        dialect,
        int(args.max_mb * 1e6),
        by=args.by,
        split=args.split,
        stop_mb=args.stop_mb,
        prefixes=True
        if args.prefixes == "*"
        else set(args.prefixes.split(","))
        if args.prefixes
        else None,
        shapes=True if args.shapes == "*" else set(args.shapes.split(",")) if args.shapes else None,
    )


def _show_sample(got: list, errors: list[str], args, candidates: list[dict]) -> None:
    if args.record_set_name:
        print(
            f"{len(candidates)} record set(s) match {args.record_set_name!r}; sampled {len(got)} "
            f"of them (--files {args.files})"
        )
    for rs, url, rows in got:
        read_from = f" (read from {mapped(str(url))})" if mapped(str(url)) != str(url) else ""
        print(f"Sampled {len(rows)} rows of {text(rs.get('name'))} from {url}{read_from}")
        wide, narrow, total = WIDTHS.get(str(url), (0, 0, 0))
        if wide or narrow:
            print(
                f"  ({wide} of {total} rows have more cells than the header, {narrow} fewer: a "
                "delimiter inside values, shifted rows, or the wrong dialect)"
            )
    if len(got) > 1:
        sigs = [json.dumps(rows[:20], sort_keys=True, default=str) for _, _, rows in got]
        if len(set(sigs)) < len(sigs):
            print("(some sampled files start with the same rows; counts below include repeats)")
    for e in errors:
        print(f"(skipped {e})")
    print()
    groups = [[g] for g in got] if args.per_file else [got]
    for group in groups:
        if args.per_file:
            print(f"== {text(group[0][0].get('name'))} ({group[0][1]})")
        show_columns(group, args.examples)


def show_columns(got: list, n_examples: int) -> None:
    """Each column's examples, emptiness and shape, over the sampled rows of the given files."""
    for f in got[0][0].get("field") or []:
        name = text(f.get("name"))
        vals = [r.get(name) for _, _, rows in got for r in rows]
        examples = []
        for v in vals:
            sv = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
            if v not in (None, "") and sv not in examples:
                examples.append(sv)
            if len(examples) >= n_examples:
                break
        empty = sum(v in (None, "", [], {}) for v in vals)
        print(f"- {name}  [{', '.join(as_list(f.get('dataType')))}]  empty {empty}/{len(vals)}")
        print(f"    shape: {shape(vals, name)}")
        declared = set(as_list(f.get("dataType"))) & {
            "sc:Integer",
            "sc:Float",
            "sc:Number",
            "sc:Boolean",
        }
        if declared:
            numeric = re.compile(
                r"-?\d+"
                if "sc:Integer" in declared
                else r"-?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?"  # 5, 5., .5, 5.0, 5e-3
            )
            if "sc:Boolean" in declared:
                numeric = re.compile(r"(?i)true|false|0|1")
            off = [
                str(v)
                for v in vals
                if v not in (None, "", [], {}) and not numeric.fullmatch(str(v).strip())
            ]
            if off:
                print(
                    f"    declared {', '.join(sorted(declared))}, but {len(off)} values aren't "
                    "(e.g. "
                    f"{list(dict.fromkeys(off))[:3]}): fix with atomic, and note it as a pipeline "
                    "problem"
                )
        for e in examples:
            print(f"    e.g. {e[:120]}")


if __name__ == "__main__":
    main()
