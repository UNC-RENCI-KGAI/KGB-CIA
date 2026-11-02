"""The two SQLite caches: PubMed responses, and LLM labels.

- `PubMedCache` keeps ESearch results by query string with their retrieval date, abstracts by
  PMID (so an abstract shared by several pairs is fetched once), and NodeNorm entries by CURIE.
  A rerun makes no requests for anything already here.
- `LabelCache` keeps every label by (statement, PMID, prompt version, model), with its quote,
  the qualifiers proposed, the second pass's verdicts and each call's token use. Step 6 reads
  only this cache, so the graph can be rebuilt without calling a model.

Both live under `config.DEFAULT_CACHE_DIR` (shared on the RENCI drive). They use SQLite's
default rollback journal rather than WAL, which is unsafe on network filesystems. Write from one
process at a time.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

Label = Literal["confirmed", "contradicted", "ambiguous", "no_evidence"]
LABELS: tuple[str, ...] = ("confirmed", "contradicted", "ambiguous", "no_evidence")
Claim = Literal["causal", "associative"]
ReviewKind = Literal["contradicted", "qualifier"]
Verdict = Literal["accept", "reject"]


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now(UTC).date().isoformat()


def canonical(data: dict[str, Any] | None) -> str:
    """Canonical JSON of a qualifier set: the key that makes two proposals the same claim."""
    return json.dumps(data or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _chunks(items: Sequence[str], size: int = 500) -> Iterator[Sequence[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


class _Cache:
    SCHEMA = ""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(self.SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# --- PubMed ---------------------------------------------------------------------------------


@dataclass
class Search:
    query: str
    retrieved: str
    total: int
    pmids: list[str]


@dataclass
class Abstract:
    pmid: str
    title: str
    abstract: str = ""
    year: int | None = None
    retrieved: str = ""

    @property
    def has_text(self) -> bool:
        return bool(self.abstract.strip())


class PubMedCache(_Cache):
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS searches (
        query TEXT PRIMARY KEY, retrieved TEXT NOT NULL, total INTEGER NOT NULL,
        pmids TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS abstracts (
        pmid TEXT PRIMARY KEY, title TEXT NOT NULL, abstract TEXT NOT NULL,
        year INTEGER, retrieved TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS nodenorm (
        curie TEXT PRIMARY KEY, entry TEXT, retrieved TEXT NOT NULL);
    """

    def get_search(self, query: str) -> Search | None:
        row = self.db.execute("SELECT * FROM searches WHERE query = ?", (query,)).fetchone()
        if row is None:
            return None
        return Search(row["query"], row["retrieved"], row["total"], json.loads(row["pmids"]))

    def put_search(self, search: Search) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO searches VALUES (?, ?, ?, ?)",
            (search.query, search.retrieved, search.total, json.dumps(search.pmids)),
        )
        self.db.commit()

    def get_abstracts(self, pmids: Iterable[str]) -> dict[str, Abstract]:
        """Cached abstracts by PMID. A PMID PubMed has no abstract for is here with abstract ''."""
        out: dict[str, Abstract] = {}
        for chunk in _chunks(list(dict.fromkeys(pmids))):
            marks = ",".join("?" * len(chunk))
            for row in self.db.execute(
                f"SELECT * FROM abstracts WHERE pmid IN ({marks})", tuple(chunk)
            ):
                out[row["pmid"]] = Abstract(
                    row["pmid"], row["title"], row["abstract"], row["year"], row["retrieved"]
                )
        return out

    def missing_abstracts(self, pmids: Iterable[str]) -> list[str]:
        wanted = list(dict.fromkeys(pmids))
        have = self.get_abstracts(wanted)
        return [p for p in wanted if p not in have]

    def put_abstracts(self, abstracts: Iterable[Abstract]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO abstracts VALUES (?, ?, ?, ?, ?)",
            [(a.pmid, a.title, a.abstract, a.year, a.retrieved or now()) for a in abstracts],
        )
        self.db.commit()

    def get_nodenorm(self, curies: Iterable[str]) -> dict[str, dict | None]:
        """Cached NodeNorm entries by CURIE; None for a CURIE NodeNorm doesn't know."""
        out: dict[str, dict | None] = {}
        for chunk in _chunks(list(dict.fromkeys(curies))):
            marks = ",".join("?" * len(chunk))
            for row in self.db.execute(
                f"SELECT curie, entry FROM nodenorm WHERE curie IN ({marks})", tuple(chunk)
            ):
                out[row["curie"]] = json.loads(row["entry"]) if row["entry"] else None
        return out

    def put_nodenorm(self, entries: dict[str, dict | None]) -> None:
        stamp = now()
        self.db.executemany(
            "INSERT OR REPLACE INTO nodenorm VALUES (?, ?, ?)",
            [(c, json.dumps(e) if e else None, stamp) for c, e in entries.items()],
        )
        self.db.commit()


# --- Labels ---------------------------------------------------------------------------------


@dataclass
class LabelRow:
    """One (statement, PMID) label. `quote_valid` says whether the quote is in the abstract, and
    is None when there is no quote (no_evidence, or an ambiguous label given without one);
    step 6 uses a confirmed or contradicted label only when it's True."""

    statement_id: str
    pmid: str
    prompt_version: str
    model: str
    label: Label
    quote: str | None = None
    quote_valid: bool | None = None
    claim: Claim | None = None
    created: str = ""


@dataclass
class ProposalRow:
    """A qualifier set the abstract supports for a statement, beyond what the edge says.
    `valid` is the Biolink check (bmt); `errors` says what failed it."""

    statement_id: str
    pmid: str
    prompt_version: str
    model: str
    qualifiers: dict[str, Any]
    quote: str | None = None
    quote_valid: bool | None = None
    valid: bool | None = None
    errors: list[str] = field(default_factory=list)
    claim: Claim | None = None
    created: str = ""


@dataclass
class ReviewRow:
    """The second pass's verdict on a contradicted label or a qualifier proposal of the given
    first-pass (prompt_version, model). `qualifiers` is None for contradicted labels."""

    kind: ReviewKind
    statement_id: str
    pmid: str
    prompt_version: str
    model: str
    qualifiers: dict[str, Any] | None
    reviewer: str
    review_prompt_version: str
    verdict: Verdict
    reason: str = ""
    created: str = ""


@dataclass
class CallRow:
    step: Literal["label", "review"]
    pair_id: str
    model: str
    prompt_version: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float | None = None
    seconds: float = 0.0
    error: str | None = None
    created: str = ""


def _bool(value) -> bool | None:
    return None if value is None else bool(value)


def _int(value: bool | None) -> int | None:
    return None if value is None else int(value)


class LabelCache(_Cache):
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS labels (
        statement_id TEXT NOT NULL, pmid TEXT NOT NULL, prompt_version TEXT NOT NULL,
        model TEXT NOT NULL, label TEXT NOT NULL, quote TEXT, quote_valid INTEGER, claim TEXT,
        created TEXT NOT NULL,
        PRIMARY KEY (statement_id, pmid, prompt_version, model));
    CREATE TABLE IF NOT EXISTS proposals (
        statement_id TEXT NOT NULL, pmid TEXT NOT NULL, prompt_version TEXT NOT NULL,
        model TEXT NOT NULL, qualifiers TEXT NOT NULL, quote TEXT, quote_valid INTEGER,
        valid INTEGER, errors TEXT NOT NULL, claim TEXT, created TEXT NOT NULL,
        PRIMARY KEY (statement_id, pmid, prompt_version, model, qualifiers));
    CREATE TABLE IF NOT EXISTS reviews (
        kind TEXT NOT NULL, statement_id TEXT NOT NULL, pmid TEXT NOT NULL,
        prompt_version TEXT NOT NULL, model TEXT NOT NULL, qualifiers TEXT NOT NULL,
        reviewer TEXT NOT NULL, review_prompt_version TEXT NOT NULL, verdict TEXT NOT NULL,
        reason TEXT NOT NULL, created TEXT NOT NULL,
        PRIMARY KEY (kind, statement_id, pmid, prompt_version, model, qualifiers, reviewer,
                     review_prompt_version));
    CREATE TABLE IF NOT EXISTS calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT, step TEXT NOT NULL, pair_id TEXT NOT NULL,
        model TEXT NOT NULL, prompt_version TEXT NOT NULL, input_tokens INTEGER NOT NULL,
        output_tokens INTEGER NOT NULL, cost REAL, seconds REAL NOT NULL, error TEXT,
        created TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS labels_run ON labels (prompt_version, model, label);
    CREATE INDEX IF NOT EXISTS proposals_run ON proposals (prompt_version, model);
    """

    # labels

    def put_labels(self, rows: Iterable[LabelRow]) -> None:
        stamp = now()
        self.db.executemany(
            "INSERT OR REPLACE INTO labels VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    r.statement_id,
                    r.pmid,
                    r.prompt_version,
                    r.model,
                    r.label,
                    r.quote,
                    _int(r.quote_valid),
                    r.claim,
                    r.created or stamp,
                )
                for r in rows
            ],
        )
        self.db.commit()

    def _label(self, row: sqlite3.Row) -> LabelRow:
        return LabelRow(
            row["statement_id"],
            row["pmid"],
            row["prompt_version"],
            row["model"],
            row["label"],
            row["quote"],
            _bool(row["quote_valid"]),
            row["claim"],
            row["created"],
        )

    def labels(
        self,
        prompt_version: str,
        model: str,
        statement_ids: Iterable[str] | None = None,
        label: Label | None = None,
    ) -> list[LabelRow]:
        """Labels of one run, optionally only for some statements or one label value."""
        sql = "SELECT * FROM labels WHERE prompt_version = ? AND model = ?"
        args: list[Any] = [prompt_version, model]
        if label is not None:
            sql += " AND label = ?"
            args.append(label)
        if statement_ids is None:
            return [self._label(r) for r in self.db.execute(sql, args)]
        out = []
        for chunk in _chunks(list(dict.fromkeys(statement_ids))):
            marks = ",".join("?" * len(chunk))
            rows = self.db.execute(f"{sql} AND statement_id IN ({marks})", [*args, *chunk])
            out.extend(self._label(r) for r in rows)
        return out

    def labeled(
        self, prompt_version: str, model: str, statement_ids: Iterable[str]
    ) -> set[tuple[str, str]]:
        """(statement_id, pmid) pairs that already have a label in this run."""
        return {(r.statement_id, r.pmid) for r in self.labels(prompt_version, model, statement_ids)}

    # proposals

    def put_proposals(self, rows: Iterable[ProposalRow]) -> None:
        stamp = now()
        self.db.executemany(
            "INSERT OR REPLACE INTO proposals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    r.statement_id,
                    r.pmid,
                    r.prompt_version,
                    r.model,
                    canonical(r.qualifiers),
                    r.quote,
                    _int(r.quote_valid),
                    _int(r.valid),
                    json.dumps(r.errors),
                    r.claim,
                    r.created or stamp,
                )
                for r in rows
            ],
        )
        self.db.commit()

    def proposals(
        self, prompt_version: str, model: str, statement_ids: Iterable[str] | None = None
    ) -> list[ProposalRow]:
        sql = "SELECT * FROM proposals WHERE prompt_version = ? AND model = ?"
        args: list[Any] = [prompt_version, model]
        rows: list[sqlite3.Row] = []
        if statement_ids is None:
            rows = list(self.db.execute(sql, args))
        else:
            for chunk in _chunks(list(dict.fromkeys(statement_ids))):
                marks = ",".join("?" * len(chunk))
                rows.extend(
                    self.db.execute(f"{sql} AND statement_id IN ({marks})", [*args, *chunk])
                )
        return [
            ProposalRow(
                r["statement_id"],
                r["pmid"],
                r["prompt_version"],
                r["model"],
                json.loads(r["qualifiers"]),
                r["quote"],
                _bool(r["quote_valid"]),
                _bool(r["valid"]),
                json.loads(r["errors"]),
                r["claim"],
                r["created"],
            )
            for r in rows
        ]

    # reviews

    def put_reviews(self, rows: Iterable[ReviewRow]) -> None:
        stamp = now()
        self.db.executemany(
            "INSERT OR REPLACE INTO reviews VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    r.kind,
                    r.statement_id,
                    r.pmid,
                    r.prompt_version,
                    r.model,
                    canonical(r.qualifiers) if r.qualifiers is not None else "",
                    r.reviewer,
                    r.review_prompt_version,
                    r.verdict,
                    r.reason,
                    r.created or stamp,
                )
                for r in rows
            ],
        )
        self.db.commit()

    def reviews(
        self,
        prompt_version: str,
        model: str,
        reviewer: str | None = None,
        review_prompt_version: str | None = None,
    ) -> list[ReviewRow]:
        sql = "SELECT * FROM reviews WHERE prompt_version = ? AND model = ?"
        args: list[Any] = [prompt_version, model]
        if reviewer is not None:
            sql += " AND reviewer = ?"
            args.append(reviewer)
        if review_prompt_version is not None:
            sql += " AND review_prompt_version = ?"
            args.append(review_prompt_version)
        return [
            ReviewRow(
                r["kind"],
                r["statement_id"],
                r["pmid"],
                r["prompt_version"],
                r["model"],
                json.loads(r["qualifiers"]) if r["qualifiers"] else None,
                r["reviewer"],
                r["review_prompt_version"],
                r["verdict"],
                r["reason"],
                r["created"],
            )
            for r in self.db.execute(sql, args)
        ]

    # calls

    def log_call(self, call: CallRow) -> None:
        self.db.execute(
            "INSERT INTO calls (step, pair_id, model, prompt_version, input_tokens, "
            "output_tokens, cost, seconds, error, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                call.step,
                call.pair_id,
                call.model,
                call.prompt_version,
                call.input_tokens,
                call.output_tokens,
                call.cost,
                call.seconds,
                call.error,
                call.created or now(),
            ),
        )
        self.db.commit()

    def prompt_versions(self, model: str) -> list[str]:
        """Every prompt version `model` has labels for, oldest first."""
        rows = self.db.execute(
            "SELECT prompt_version FROM labels WHERE model = ? GROUP BY prompt_version "
            "ORDER BY MIN(created), prompt_version",
            (model,),
        )
        return [r["prompt_version"] for r in rows]

    def usage(self, step: str, model: str, prompt_version: str) -> dict[str, Any]:
        """Totals over the calls of one run: calls, errors, tokens and cost. `cost` sums the
        calls whose cost is known (None if none is); `unknown_cost` counts the others."""
        row = self.db.execute(
            "SELECT COUNT(*) AS calls, SUM(error IS NOT NULL) AS errors, "
            "SUM(cost IS NULL) AS unknown_cost, "
            "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "COALESCE(SUM(output_tokens), 0) AS output_tokens, SUM(cost) AS cost, "
            "COUNT(DISTINCT pair_id) AS pairs, COALESCE(SUM(seconds), 0) AS seconds "
            "FROM calls WHERE step = ? AND model = ? AND prompt_version = ?",
            (step, model, prompt_version),
        ).fetchone()
        return {k: row[k] for k in row.keys()} | {
            "errors": row["errors"] or 0,
            "unknown_cost": row["unknown_cost"] or 0,
        }
