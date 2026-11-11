"""The PubMed cache (SQLite), and searches and abstract fetches that go through it.

`PubMedCache` keeps ESearch results by query string with their retrieval date, abstracts by PMID
(a PMID PubMed doesn't return is kept empty, so it isn't fetched again) and NodeNorm entries by
CURIE. A rerun makes no requests for anything already there, so agents that share one cache
file share what each fetched: whichever runs first fills it.

The file is `data/cache/pubmed.sqlite` (relative to where the command runs) unless
KGB_PUBMED_CACHE names another; point it at the shared RENCI drive to share it between people.
It uses SQLite's default rollback journal rather than WAL, which is unsafe on network
filesystems. Write from one process at a time.

The tables match the augmentation agent's `pubmed.sqlite`, which opens as it is; tables this
module doesn't know (that cache's `groundings`) are left alone. A subclass can add its own
tables by extending `SCHEMA`.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path

from shared.pubmed._util import chunks, now, today, warn
from shared.pubmed.eutils import EFETCH_BATCH, EUtils, EUtilsError
from shared.pubmed.records import Abstract, Search

CACHE_ENV = "KGB_PUBMED_CACHE"
DEFAULT_CACHE_PATH = Path("data/cache/pubmed.sqlite")
# Ids per `IN (...)` query, well under SQLite's limit on bound parameters.
QUERY_CHUNK = 500


def default_cache_path() -> Path:
    """KGB_PUBMED_CACHE if set, else data/cache/pubmed.sqlite."""
    return Path(os.environ.get(CACHE_ENV) or DEFAULT_CACHE_PATH)


class PubMedCache:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS searches (
        query TEXT PRIMARY KEY, retrieved TEXT NOT NULL, total INTEGER NOT NULL,
        pmids TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS abstracts (
        pmid TEXT PRIMARY KEY, title TEXT NOT NULL, abstract TEXT NOT NULL,
        year INTEGER, retrieved TEXT NOT NULL, publication_types TEXT);
    CREATE TABLE IF NOT EXISTS nodenorm (
        curie TEXT PRIMARY KEY, entry TEXT, retrieved TEXT NOT NULL);
    """
    # Columns added to a table after it was first released, {table: {column: declaration}}.
    # Opening a cache adds those it lacks, so an older cache gets them with NULL in its rows.
    ADDED: dict[str, dict[str, str]] = {"abstracts": {"publication_types": "TEXT"}}

    def __init__(self, path: Path | str | None = None):
        path = default_cache_path() if path is None else path
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(self.SCHEMA)
        self._add_columns()
        self.db.commit()

    def _add_columns(self) -> None:
        for table, columns in self.ADDED.items():
            have = {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}
            for column, declaration in columns.items():
                if column not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def close(self) -> None:
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- Searches ---

    def get_search(self, query: str) -> Search | None:
        row = self.db.execute("SELECT * FROM searches WHERE query = ?", (query,)).fetchone()
        if row is None:
            return None
        return Search(row["query"], row["retrieved"], row["total"], json.loads(row["pmids"]))

    def put_search(self, search: Search) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO searches (query, retrieved, total, pmids) VALUES (?, ?, ?, ?)",
            (search.query, search.retrieved, search.total, json.dumps(search.pmids)),
        )
        self.db.commit()

    # --- Abstracts ---

    def get_abstracts(self, pmids: Iterable[str]) -> dict[str, Abstract]:
        """Cached abstracts by PMID. A PMID PubMed has no abstract for is here with abstract ''."""
        out: dict[str, Abstract] = {}
        for chunk in chunks(list(dict.fromkeys(pmids)), QUERY_CHUNK):
            marks = ",".join("?" * len(chunk))
            for row in self.db.execute(
                f"SELECT * FROM abstracts WHERE pmid IN ({marks})", tuple(chunk)
            ):
                types = row["publication_types"]
                out[row["pmid"]] = Abstract(
                    row["pmid"],
                    row["title"],
                    row["abstract"],
                    row["year"],
                    row["retrieved"],
                    json.loads(types) if types is not None else None,
                )
        return out

    def articles(self, pmids: Iterable[str]) -> dict[str, tuple[int | None, list[str] | None]]:
        """(year, publication types) of the cached PMIDs, without their text."""
        out: dict[str, tuple[int | None, list[str] | None]] = {}
        for chunk in chunks(list(dict.fromkeys(pmids)), QUERY_CHUNK):
            marks = ",".join("?" * len(chunk))
            for row in self.db.execute(
                f"SELECT pmid, year, publication_types FROM abstracts WHERE pmid IN ({marks})",
                tuple(chunk),
            ):
                types = row["publication_types"]
                out[row["pmid"]] = (row["year"], json.loads(types) if types is not None else None)
        return out

    def missing_abstracts(self, pmids: Iterable[str]) -> list[str]:
        """The PMIDs to fetch: those not cached, and those cached without publication types."""
        wanted = list(dict.fromkeys(pmids))
        have = self.get_abstracts(wanted)
        return [p for p in wanted if p not in have or have[p].publication_types is None]

    def put_abstracts(self, abstracts: Iterable[Abstract]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO abstracts (pmid, title, abstract, year, retrieved, "
            "publication_types) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    a.pmid,
                    a.title,
                    a.abstract,
                    a.year,
                    a.retrieved or now(),
                    json.dumps(a.publication_types) if a.publication_types is not None else None,
                )
                for a in abstracts
            ],
        )
        self.db.commit()

    # --- NodeNorm ---

    def get_nodenorm(self, curies: Iterable[str]) -> dict[str, dict | None]:
        """Cached NodeNorm entries by CURIE; None for a CURIE NodeNorm doesn't know."""
        out: dict[str, dict | None] = {}
        for chunk in chunks(list(dict.fromkeys(curies)), QUERY_CHUNK):
            marks = ",".join("?" * len(chunk))
            for row in self.db.execute(
                f"SELECT curie, entry FROM nodenorm WHERE curie IN ({marks})", tuple(chunk)
            ):
                out[row["curie"]] = json.loads(row["entry"]) if row["entry"] else None
        return out

    def put_nodenorm(self, entries: dict[str, dict | None]) -> None:
        stamp = now()
        self.db.executemany(
            "INSERT OR REPLACE INTO nodenorm (curie, entry, retrieved) VALUES (?, ?, ?)",
            [(c, json.dumps(e) if e else None, stamp) for c, e in entries.items()],
        )
        self.db.commit()


# --- Through the cache --------------------------------------------------------------------------


def needs_search(cached: Search | None, retmax: int, refresh: bool = False) -> bool:
    """Whether to run a search again: a cached one serves unless it has fewer PMIDs than asked
    for and PubMed had more."""
    if refresh or cached is None:
        return True
    return len(cached.pmids) < retmax and cached.total > len(cached.pmids)


def keep_cached(fetched: list[Abstract], cached: dict[str, Abstract]) -> list[Abstract]:
    """The fetched abstracts, except that a PMID PubMed now returns nothing for keeps what the
    cache had, marked as fetched: fetching it again (for its publication types, or to refresh)
    mustn't lose its text."""
    out = []
    for a in fetched:
        old = cached.get(a.pmid)
        if old is not None and not a.title and not a.has_text:
            a = replace(old, publication_types=old.publication_types or [])
        out.append(a)
    return out


def fetch_into(cache: PubMedCache, client: EUtils, pmids: Iterable[str]) -> int:
    """EFetch `pmids` into the cache, EFETCH_BATCH at a time, each batch cached as it arrives.
    A batch that fails is warned about and skipped: its PMIDs stay out of the cache, so a rerun
    fetches them. Returns how many PMIDs were fetched."""
    fetched = 0
    for batch in chunks(list(dict.fromkeys(pmids)), EFETCH_BATCH):
        try:
            got = client.fetch(batch)
        except EUtilsError as e:
            warn(f"EFetch of {len(batch)} PMIDs failed: {e}")
            continue
        cache.put_abstracts(keep_cached(got, cache.get_abstracts(batch)))
        fetched += len(batch)
    return fetched


def fetch_abstracts(
    pmids: Iterable[str],
    *,
    cache: PubMedCache | None = None,
    client: EUtils | None = None,
    refresh: bool = False,
) -> dict[str, Abstract]:
    """Abstracts for `pmids` by PMID, in their order, from the cache where it has them.

    Only the PMIDs the cache lacks (or has without publication types), or every one with
    `refresh`, go to EFetch. A PMID PubMed doesn't return is cached, and returned, with an empty
    title and abstract (check `has_text`), so it isn't asked for again; refetched, one PubMed
    no longer returns keeps its cached text. If a batch fails after retries, with a warning,
    its PMIDs keep what the cache had: one it didn't have is left out of the result, so the
    caller can tell it apart, and a rerun fetches it. Anything that isn't all digits is left
    out unasked. Without `cache` or
    `client`, opens the default ones (a client only if something needs fetching) and closes
    them after."""
    wanted = [p for p in dict.fromkeys(pmids) if p.isascii() and p.isdigit()]
    with ExitStack() as stack:
        if cache is None:
            cache = stack.enter_context(PubMedCache())
        todo = wanted if refresh else cache.missing_abstracts(wanted)
        if todo:
            if client is None:
                client = stack.enter_context(EUtils())
            fetch_into(cache, client, todo)
        got = cache.get_abstracts(wanted)
    return {p: got[p] for p in wanted if p in got}


def cached_search(
    query: str,
    retmax: int = 20,
    *,
    cache: PubMedCache | None = None,
    client: EUtils | None = None,
    refresh: bool = False,
) -> Search:
    """ESearch `query` for up to `retmax` PMIDs by relevance, through the cache: a cached search
    serves unless it has fewer PMIDs than asked for and PubMed had more (or with `refresh`).
    Raises EUtilsError if the search fails. Without `cache` or `client`, opens the default ones
    and closes them after."""
    with ExitStack() as stack:
        if cache is None:
            cache = stack.enter_context(PubMedCache())
        cached = cache.get_search(query)
        if needs_search(cached, retmax, refresh):
            if client is None:
                client = stack.enter_context(EUtils())
            total, pmids = client.search(query, retmax)
            cached = Search(query, today(), total, pmids)
            cache.put_search(cached)
    return replace(cached, pmids=cached.pmids[: max(0, retmax)])
