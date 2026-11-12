"""The augmentation agent's SQLite caches: the PubMed cache with a table of its own, and the
label cache.

- `PubMedCache` is `shared.pubmed`'s cache (ESearch results by query string, abstracts by PMID,
  NodeNorm entries by CURIE), with the groundings of the anatomy names claims give, by name. It
  is the file `shared.pubmed.default_cache_path()` names: KGB_PUBMED_CACHE, or
  data/cache/pubmed.sqlite. A rerun makes no requests for anything already there.
- `LabelCache` keeps each abstract's answer for a pair by (pair, PMID, prompt version, model):
  the study design and the claims. Code's checks of a claim, the review's verdicts and the
  sentence repairs are keyed by that and the claim's `claim_id`, and a verdict or repair by its
  own model and prompt version too. Each prompt version (label, review and repair) covers its
  prompt, message format, reply schema and reasoning effort, so a change to any of them asks
  again. It also logs each call's token use. Step 6 reads it, with the article types in the
  PubMed cache and the work files, so the graph can be rebuilt without calling a model. It lives
  in the cache directory (`config.DEFAULT_CACHE_DIR`, shared on the RENCI drive).
- An older label cache also has the tables of the statement labels that came before the claims
  (`labels`, `proposals`, `reviews`, `repairs`). No code reads or writes them any more, and they
  stay as they are.

Both use SQLite's default rollback journal rather than WAL, which is unsafe on network
filesystems. Write from one process at a time.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import shared.pubmed
from pubmed_evidence.graphio import chunks
from shared.schema import ReviewVerdict, StudyDesign


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def canonical(data: dict[str, Any] | None) -> str:
    """Canonical JSON of a dict (a claim, a statement): sorted keys, no spaces, so two equal
    ones read the same."""
    return json.dumps(data or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def claim_id(claim: dict[str, Any]) -> str:
    """Stable id of a claim in an abstract's answer: the first 16 hex digits of the SHA-256 of
    its canonical JSON, the claim as `abstract_claims` stores it. Each statement in it (the
    claim's own and its `also_supports`) names its subject and object by the pair's CURIEs,
    never the prompt's X and Y, which raises ValueError. Two identical claims in one answer are
    one claim. Its check, review and repair hang on this id, so code never rewrites a stored
    claim: grounding and repair are kept beside it."""
    for s in (claim.get("statement") or {}, *(claim.get("also_supports") or ())):
        if not all(":" in str(s.get(end) or "") for end in ("subject", "object")):
            raise ValueError(f"a claim's statements need CURIEs as subject and object: {s!r}")
    return hashlib.sha256(canonical(claim).encode("utf-8")).hexdigest()[:16]


# Ids per `IN (...)` query, well under SQLite's limit on bound parameters.
QUERY_CHUNK = 500


# --- PubMed ---------------------------------------------------------------------------------

# What a grounded name names: anatomy grounds to UBERON. (Species come from the reply schema's
# enum, mapped in code, so they need no grounding.)
GroundingKind = Literal["anatomy"]


@dataclass
class Grounding:
    """A name as a claim gives it, grounded to a term of its kind, with the term's label.
    `curie` is None for a name that couldn't be grounded, which is kept too so a rerun doesn't
    ask again; `source` says what grounded it, or failed to."""

    text: str
    kind: GroundingKind
    curie: str | None = None
    label: str | None = None
    source: str | None = None
    created: str = ""


class PubMedCache(shared.pubmed.PubMedCache):
    """`shared.pubmed.PubMedCache`, with the ground step's `groundings` table."""

    SCHEMA = (
        shared.pubmed.PubMedCache.SCHEMA
        + """
    CREATE TABLE IF NOT EXISTS groundings (
        text TEXT NOT NULL, kind TEXT NOT NULL, curie TEXT, label TEXT, source TEXT,
        created TEXT NOT NULL,
        PRIMARY KEY (text, kind));
    """
    )

    def get_groundings(self, kind: GroundingKind, texts: Iterable[str]) -> dict[str, Grounding]:
        """Cached groundings of the names, by name, exactly as given; a name that couldn't be
        grounded is here with curie None."""
        out: dict[str, Grounding] = {}
        for chunk in chunks(list(dict.fromkeys(texts)), QUERY_CHUNK):
            marks = ",".join("?" * len(chunk))
            for row in self.db.execute(
                f"SELECT * FROM groundings WHERE kind = ? AND text IN ({marks})", (kind, *chunk)
            ):
                out[row["text"]] = Grounding(
                    row["text"],
                    row["kind"],
                    row["curie"],
                    row["label"],
                    row["source"],
                    row["created"],
                )
        return out

    def put_groundings(self, groundings: Iterable[Grounding]) -> None:
        stamp = now()
        self.db.executemany(
            _insert("groundings", "text, kind, curie, label, source, created"),
            [(g.text, g.kind, g.curie, g.label, g.source, g.created or stamp) for g in groundings],
        )
        self.db.commit()


# --- Labels ---------------------------------------------------------------------------------


@dataclass
class CallRow:
    """One call's token use and cost. `input_tokens` counts every prompt token, the cache's
    reads and writes among them (None when unknown, as in calls logged before they were)."""

    step: Literal["label", "review", "repair"]
    pair_id: str
    model: str
    prompt_version: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float | None = None
    seconds: float = 0.0
    error: str | None = None
    created: str = ""
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    answered_by: str | None = None


@dataclass
class AbstractClaimsRow:
    """One abstract's answer in a pair's label run of the given (prompt_version, model): the
    extractor's study design and its claims, [] for an abstract with no claims. Each claim is the
    reply's, as a dict, with the pair's CURIEs in place of X and Y (see `claim_id`)."""

    pair_id: str
    pmid: str
    prompt_version: str
    model: str
    study_design: StudyDesign
    claims: list[dict[str, Any]] = field(default_factory=list)
    created: str = ""
    answered_by: str | None = None  # the model that answered: a fallback's, if one did


@dataclass
class ClaimCheckRow:
    """Code's checks of one claim in a label run. `valid` is every check but the sentence's
    (the slots and values the predicate allows, `causes`, `negative_finding_type`,
    `also_supports`), and `errors` says what failed it; `sentence_valid` says whether the
    evidence sentence is in the abstract."""

    pair_id: str
    pmid: str
    prompt_version: str
    model: str
    claim_id: str
    valid: bool
    sentence_valid: bool
    errors: list[str] = field(default_factory=list)


@dataclass
class ClaimReviewRow:
    """The reviewer's verdict on one claim of the given label run (prompt_version, model)."""

    pair_id: str
    pmid: str
    prompt_version: str
    model: str
    claim_id: str
    reviewer: str
    review_prompt_version: str
    verdict: ReviewVerdict
    reason: str = ""
    created: str = ""
    answered_by: str | None = None


@dataclass
class ClaimRepairRow:
    """The sentence repair pass's answer for one claim of the given label run whose evidence
    sentence wasn't found: a sentence (`sentence_valid` from the same check) or None."""

    pair_id: str
    pmid: str
    prompt_version: str
    model: str
    claim_id: str
    repairer: str
    repair_prompt_version: str
    evidence_sentence: str | None
    sentence_valid: bool | None = None
    created: str = ""
    answered_by: str | None = None


def _bool(value) -> bool | None:
    return None if value is None else bool(value)


def _insert(table: str, columns: str) -> str:
    """INSERT OR REPLACE naming its columns, which an older cache has in another order."""
    n = len(columns.split(","))
    return f"INSERT OR REPLACE INTO {table} ({columns}) VALUES ({', '.join('?' * n)})"


def _int(value: bool | None) -> int | None:
    return None if value is None else int(value)


class LabelCache:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT, step TEXT NOT NULL, pair_id TEXT NOT NULL,
        model TEXT NOT NULL, prompt_version TEXT NOT NULL, input_tokens INTEGER NOT NULL,
        output_tokens INTEGER NOT NULL, cost REAL, seconds REAL NOT NULL, error TEXT,
        created TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS abstract_claims (
        pair_id TEXT NOT NULL, pmid TEXT NOT NULL, prompt_version TEXT NOT NULL,
        model TEXT NOT NULL, study_design TEXT NOT NULL, claims TEXT NOT NULL,
        created TEXT NOT NULL, answered_by TEXT,
        PRIMARY KEY (pair_id, pmid, prompt_version, model));
    CREATE TABLE IF NOT EXISTS claim_checks (
        pair_id TEXT NOT NULL, pmid TEXT NOT NULL, prompt_version TEXT NOT NULL,
        model TEXT NOT NULL, claim_id TEXT NOT NULL, valid INTEGER NOT NULL,
        errors TEXT NOT NULL, sentence_valid INTEGER NOT NULL,
        PRIMARY KEY (pair_id, pmid, prompt_version, model, claim_id));
    CREATE TABLE IF NOT EXISTS claim_reviews (
        pair_id TEXT NOT NULL, pmid TEXT NOT NULL, prompt_version TEXT NOT NULL,
        model TEXT NOT NULL, claim_id TEXT NOT NULL, reviewer TEXT NOT NULL,
        review_prompt_version TEXT NOT NULL, verdict TEXT NOT NULL, reason TEXT NOT NULL,
        created TEXT NOT NULL, answered_by TEXT,
        PRIMARY KEY (pair_id, pmid, prompt_version, model, claim_id, reviewer,
                     review_prompt_version));
    CREATE TABLE IF NOT EXISTS claim_repairs (
        pair_id TEXT NOT NULL, pmid TEXT NOT NULL, prompt_version TEXT NOT NULL,
        model TEXT NOT NULL, claim_id TEXT NOT NULL, repairer TEXT NOT NULL,
        repair_prompt_version TEXT NOT NULL, evidence_sentence TEXT, sentence_valid INTEGER,
        created TEXT NOT NULL, answered_by TEXT,
        PRIMARY KEY (pair_id, pmid, prompt_version, model, claim_id, repairer,
                     repair_prompt_version));
    CREATE INDEX IF NOT EXISTS abstract_claims_run ON abstract_claims (prompt_version, model);
    CREATE INDEX IF NOT EXISTS claim_checks_run ON claim_checks (prompt_version, model);
    """
    # Columns added to a table after it was first released, {table: {column: declaration}}.
    # Opening a cache adds those it lacks, so an older cache gets them with NULL in its rows.
    ADDED = {
        "calls": {
            "cache_read_tokens": "INTEGER",
            "cache_write_tokens": "INTEGER",
            "answered_by": "TEXT",
        },
    }

    def __init__(self, path: Path | str):
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

    # claims

    def _rows(
        self, table: str, where: str, args: list[Any], pair_ids: Iterable[str] | None
    ) -> list[sqlite3.Row]:
        """The table's rows that match `where`, and only those of `pair_ids` if given."""
        sql = f"SELECT * FROM {table} WHERE {where}"
        if pair_ids is None:
            return list(self.db.execute(sql, args))
        rows: list[sqlite3.Row] = []
        for chunk in chunks(list(dict.fromkeys(pair_ids)), QUERY_CHUNK):
            marks = ",".join("?" * len(chunk))
            rows.extend(self.db.execute(f"{sql} AND pair_id IN ({marks})", [*args, *chunk]))
        return rows

    def put_abstract_claims(self, rows: Iterable[AbstractClaimsRow]) -> None:
        stamp = now()
        self.db.executemany(
            _insert(
                "abstract_claims",
                "pair_id, pmid, prompt_version, model, study_design, claims, created, answered_by",
            ),
            [
                (
                    r.pair_id,
                    r.pmid,
                    r.prompt_version,
                    r.model,
                    r.study_design,
                    json.dumps(r.claims, ensure_ascii=False),
                    r.created or stamp,
                    r.answered_by,
                )
                for r in rows
            ],
        )
        self.db.commit()

    def abstract_claims(
        self, prompt_version: str, model: str, pair_ids: Iterable[str] | None = None
    ) -> list[AbstractClaimsRow]:
        """The answers of one label run, optionally only for some pairs."""
        rows = self._rows(
            "abstract_claims", "prompt_version = ? AND model = ?", [prompt_version, model], pair_ids
        )
        return [
            AbstractClaimsRow(
                r["pair_id"],
                r["pmid"],
                r["prompt_version"],
                r["model"],
                r["study_design"],
                json.loads(r["claims"]),
                r["created"],
                r["answered_by"],
            )
            for r in rows
        ]

    def answered(
        self, prompt_version: str, model: str, pair_ids: Iterable[str]
    ) -> set[tuple[str, str]]:
        """(pair_id, pmid) of the abstracts that already have an answer in this run."""
        return {(r.pair_id, r.pmid) for r in self.abstract_claims(prompt_version, model, pair_ids)}

    def claim_prompt_versions(self, model: str) -> list[str]:
        """Every label prompt version `model` has answers for, oldest first."""
        rows = self.db.execute(
            "SELECT prompt_version FROM abstract_claims WHERE model = ? GROUP BY prompt_version "
            "ORDER BY MIN(created), prompt_version",
            (model,),
        )
        return [r["prompt_version"] for r in rows]

    def put_claim_checks(self, rows: Iterable[ClaimCheckRow]) -> None:
        self.db.executemany(
            _insert(
                "claim_checks",
                "pair_id, pmid, prompt_version, model, claim_id, valid, errors, sentence_valid",
            ),
            [
                (
                    r.pair_id,
                    r.pmid,
                    r.prompt_version,
                    r.model,
                    r.claim_id,
                    int(r.valid),
                    json.dumps(r.errors, ensure_ascii=False),
                    int(r.sentence_valid),
                )
                for r in rows
            ],
        )
        self.db.commit()

    def claim_checks(
        self, prompt_version: str, model: str, pair_ids: Iterable[str] | None = None
    ) -> list[ClaimCheckRow]:
        rows = self._rows(
            "claim_checks", "prompt_version = ? AND model = ?", [prompt_version, model], pair_ids
        )
        return [
            ClaimCheckRow(
                r["pair_id"],
                r["pmid"],
                r["prompt_version"],
                r["model"],
                r["claim_id"],
                bool(r["valid"]),
                bool(r["sentence_valid"]),
                json.loads(r["errors"]),
            )
            for r in rows
        ]

    def put_claim_reviews(self, rows: Iterable[ClaimReviewRow]) -> None:
        stamp = now()
        self.db.executemany(
            _insert(
                "claim_reviews",
                "pair_id, pmid, prompt_version, model, claim_id, reviewer, "
                "review_prompt_version, verdict, reason, created, answered_by",
            ),
            [
                (
                    r.pair_id,
                    r.pmid,
                    r.prompt_version,
                    r.model,
                    r.claim_id,
                    r.reviewer,
                    r.review_prompt_version,
                    r.verdict,
                    r.reason,
                    r.created or stamp,
                    r.answered_by,
                )
                for r in rows
            ],
        )
        self.db.commit()

    def claim_reviews(
        self,
        prompt_version: str,
        model: str,
        reviewer: str | None = None,
        review_prompt_version: str | None = None,
        pair_ids: Iterable[str] | None = None,
    ) -> list[ClaimReviewRow]:
        """The verdicts on one label run's claims, optionally only one reviewer's, with one
        review prompt version, or for some pairs."""
        where = "prompt_version = ? AND model = ?"
        args: list[Any] = [prompt_version, model]
        if reviewer is not None:
            where += " AND reviewer = ?"
            args.append(reviewer)
        if review_prompt_version is not None:
            where += " AND review_prompt_version = ?"
            args.append(review_prompt_version)
        return [
            ClaimReviewRow(
                r["pair_id"],
                r["pmid"],
                r["prompt_version"],
                r["model"],
                r["claim_id"],
                r["reviewer"],
                r["review_prompt_version"],
                r["verdict"],
                r["reason"],
                r["created"],
                r["answered_by"],
            )
            for r in self._rows("claim_reviews", where, args, pair_ids)
        ]

    def put_claim_repairs(self, rows: Iterable[ClaimRepairRow]) -> None:
        stamp = now()
        self.db.executemany(
            _insert(
                "claim_repairs",
                "pair_id, pmid, prompt_version, model, claim_id, repairer, "
                "repair_prompt_version, evidence_sentence, sentence_valid, created, answered_by",
            ),
            [
                (
                    r.pair_id,
                    r.pmid,
                    r.prompt_version,
                    r.model,
                    r.claim_id,
                    r.repairer,
                    r.repair_prompt_version,
                    r.evidence_sentence,
                    _int(r.sentence_valid),
                    r.created or stamp,
                    r.answered_by,
                )
                for r in rows
            ],
        )
        self.db.commit()

    def claim_repairs(
        self,
        prompt_version: str,
        model: str,
        repairer: str,
        repair_prompt_version: str,
        pair_ids: Iterable[str] | None = None,
    ) -> list[ClaimRepairRow]:
        """The sentence repairs of one label run's claims by one repairer and repair prompt."""
        rows = self._rows(
            "claim_repairs",
            "prompt_version = ? AND model = ? AND repairer = ? AND repair_prompt_version = ?",
            [prompt_version, model, repairer, repair_prompt_version],
            pair_ids,
        )
        return [
            ClaimRepairRow(
                r["pair_id"],
                r["pmid"],
                r["prompt_version"],
                r["model"],
                r["claim_id"],
                r["repairer"],
                r["repair_prompt_version"],
                r["evidence_sentence"],
                _bool(r["sentence_valid"]),
                r["created"],
                r["answered_by"],
            )
            for r in rows
        ]

    # calls

    def log_call(self, call: CallRow) -> None:
        self.db.execute(
            "INSERT INTO calls (step, pair_id, model, prompt_version, input_tokens, "
            "output_tokens, cost, seconds, error, created, cache_read_tokens, "
            "cache_write_tokens, answered_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                call.cache_read_tokens,
                call.cache_write_tokens,
                call.answered_by,
            ),
        )
        self.db.commit()

    def usage(self, step: str, model: str, prompt_version: str) -> dict[str, Any]:
        """Totals over the calls of one run: calls, errors, tokens and cost. `cost` sums the
        calls whose cost is known (None if none is); `unknown_cost` counts the others. The
        cache's reads and writes are among `input_tokens`, and sum only the calls that logged
        them."""
        row = self.db.execute(
            "SELECT COUNT(*) AS calls, SUM(error IS NOT NULL) AS errors, "
            "SUM(cost IS NULL) AS unknown_cost, "
            "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens, "
            "COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens, "
            "COALESCE(SUM(output_tokens), 0) AS output_tokens, SUM(cost) AS cost, "
            "COUNT(DISTINCT pair_id) AS pairs, COALESCE(SUM(seconds), 0) AS seconds "
            "FROM calls WHERE step = ? AND model = ? AND prompt_version = ?",
            (step, model, prompt_version),
        ).fetchone()
        return {k: row[k] for k in row.keys()} | {
            "errors": row["errors"] or 0,
            "unknown_cost": row["unknown_cost"] or 0,
        }
