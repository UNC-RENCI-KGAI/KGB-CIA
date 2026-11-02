"""Step 6: apply the cached labels to the graph, in code.

Step 6 reads only the input graph, the label cache and which run to apply (`label_run.json`,
or the command line), so the graph can be rebuilt without calling a model, as "agents decide,
code transforms" asks. Nothing in the baseline graph changes except `publications`, and
nothing is removed; conflicts are left to the cleaning stage.

- `confirmed` with its quote found: the PMID joins the `publications` of every edge that
  makes the statement.
- `contradicted` with its quote found and an accepting review: one new `negated` edge per
  statement, with the original's subject, predicate, object and qualifiers and every
  contradicting PMID.
- Qualifiers proposed with the quote found, valid in Biolink and an accepting review: one new
  edge per distinct qualifier set, the original's qualifiers updated with the proposed ones.
- `ambiguous`, `no_evidence`: nothing; they stay in the label cache.

A review is required unless `--no-review` is given, for runs whose second pass was skipped.
Only statements in the work dir's `statements.jsonl` (the current scope) are applied, so labels
cached for edges a later `select` dropped don't reach the graph.

The input is streamed, so it scales to millions of edges: memory holds the findings, the first
edge of each statement that gets a new edge, the statement ids of the qualified input edges (a
merged qualifier set is never empty, so only those can already make a new edge's statement) and
the ids of edges this step added on an earlier run (input that already holds this stage's
output gets no duplicates). Input records are written back as they were read, with only
`publications` replaced where PMIDs are added, so slots this step doesn't touch keep their exact
values. The output files are written under temporary names and renamed into place at the end,
so a failed run leaves no half-written graph.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from pubmed_evidence import cache, config, graphio, records
from shared.schema import Edge

APPLY_REPORT = records.APPLY_REPORT
ID_PREFIX = "kgbcia-augmentation:"


class ApplyError(RuntimeError):
    """A problem with the inputs that stops step 6, with a one-line message."""


def resolve_run(
    work: Path,
    model: str | None = None,
    prompt_version: str | None = None,
    reviewer: str | None = None,
    review_prompt_version: str | None = None,
) -> records.LabelRun:
    """The run to apply: `label_run.json` in the work dir, with each given value overriding it."""
    stored = records.read_label_run(work)
    run = records.LabelRun(
        model=model or (stored.model if stored else ""),
        prompt_version=prompt_version or (stored.prompt_version if stored else ""),
        prompt=stored.prompt if stored else None,
        reviewer=reviewer or (stored.reviewer if stored else None),
        review_prompt_version=review_prompt_version
        or (stored.review_prompt_version if stored else None),
    )
    if not run.model or not run.prompt_version:
        raise ApplyError(
            f"no label run: {work / records.LABEL_RUN} not found or incomplete; run "
            "`kgb-augment label` first, or pass --model and --prompt-version"
        )
    if run.reviewer and not run.review_prompt_version:
        raise ApplyError(f"reviewer {run.reviewer} has no review prompt version; pass one")
    return run


def _counts() -> dict[str, Any]:
    return {
        # Label and proposal rows not used, by reason.
        "invalid_quote": {"confirmed": 0, "contradicted": 0, "proposal": 0},
        "invalid_qualifiers": 0,
        "unreviewed": {"contradicted": 0, "proposal": 0},
        "rejected": {"contradicted": 0, "proposal": 0},
        # Label and proposal rows of statements outside the current scope (statements.jsonl).
        "out_of_scope": 0,
        # Distinct (statement, proposed qualifier set) that merge to the original's qualifiers.
        "same_as_original": 0,
        # Distinct new edges already in the input: a qualified statement an input edge makes, or
        # an edge this step added on an earlier run (same id).
        "already_in_graph": 0,
        # Distinct statements with usable findings that no input edge makes.
        "orphan_statements": 0,
    }


@dataclass
class Findings:
    """What the run's labels say, by statement id; PMIDs are bare digits."""

    support: dict[str, set[str]] = field(default_factory=dict)
    contradict: dict[str, set[str]] = field(default_factory=dict)
    # statement -> canonical proposed qualifiers -> PMIDs
    proposals: dict[str, dict[str, set[str]]] = field(default_factory=dict)
    skipped: dict[str, Any] = field(default_factory=_counts)


def collect_findings(
    labels: cache.LabelCache,
    run: records.LabelRun,
    require_review: bool = True,
    scope: set[str] | None = None,
) -> Findings:
    """The run's usable labels. A contradiction or proposal needs an `accept` from the run's
    reviewer unless `require_review` is False; a `reject` drops it either way. With `scope`,
    only statements in it count."""
    found = Findings()
    skipped = found.skipped

    def in_scope(statement_id: str) -> bool:
        if scope is None or statement_id in scope:
            return True
        skipped["out_of_scope"] += 1
        return False

    verdicts: dict[tuple[str, str, str, str], str] = {}
    if run.reviewer:
        for r in labels.reviews(
            run.prompt_version, run.model, run.reviewer, run.review_prompt_version
        ):
            key = "" if r.qualifiers is None else cache.canonical(r.qualifiers)
            verdicts[(r.kind, r.statement_id, r.pmid, key)] = r.verdict

    def status(key: tuple[str, str, str, str]) -> str:
        verdict = verdicts.get(key)
        if verdict == "accept":
            return "accept"
        if verdict == "reject":
            return "rejected"
        return "unreviewed" if require_review else "accept"

    for row in labels.labels(run.prompt_version, run.model, label="confirmed"):
        if not in_scope(row.statement_id):
            continue
        if row.quote_valid is not True:
            skipped["invalid_quote"]["confirmed"] += 1
            continue
        found.support.setdefault(row.statement_id, set()).add(row.pmid)

    for row in labels.labels(run.prompt_version, run.model, label="contradicted"):
        if not in_scope(row.statement_id):
            continue
        if row.quote_valid is not True:
            skipped["invalid_quote"]["contradicted"] += 1
            continue
        verdict = status(("contradicted", row.statement_id, row.pmid, ""))
        if verdict != "accept":
            skipped[verdict]["contradicted"] += 1
            continue
        found.contradict.setdefault(row.statement_id, set()).add(row.pmid)

    for row in labels.proposals(run.prompt_version, run.model):
        if not in_scope(row.statement_id):
            continue
        if row.quote_valid is not True:
            skipped["invalid_quote"]["proposal"] += 1
            continue
        if row.valid is not True or not all(map(graphio.is_qualifier_slot, row.qualifiers)):
            skipped["invalid_qualifiers"] += 1
            continue
        key = cache.canonical(row.qualifiers)
        verdict = status(("qualifier", row.statement_id, row.pmid, key))
        if verdict != "accept":
            skipped[verdict]["proposal"] += 1
            continue
        found.proposals.setdefault(row.statement_id, {}).setdefault(key, set()).add(row.pmid)
    return found


def has_labels(labels: cache.LabelCache, run: records.LabelRun) -> bool:
    """True if the cache holds any label or proposal of the run."""
    return any(
        labels.db.execute(
            f"SELECT 1 FROM {table} WHERE prompt_version = ? AND model = ? LIMIT 1",
            (run.prompt_version, run.model),
        ).fetchone()
        for table in ("labels", "proposals")
    )


def read_scope(work: Path) -> set[str] | None:
    """Statement ids in the work dir's `statements.jsonl`, or None if there is none."""
    path = work / records.STATEMENTS
    if not path.exists():
        return None
    return {d["statement_id"] for d in graphio.read_jsonl(path)}


def write_records(path: Path, items: Iterable[dict[str, Any]]) -> int:
    """One compact JSON object per line, keys in their order, as pydantic and ORION write them."""
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
            f.write("\n")
            n += 1
    return n


def _pmid_order(pmid: str) -> tuple[bool, int, str]:
    return (not pmid.isdigit(), int(pmid) if pmid.isdigit() else 0, pmid)


def new_edge(
    subject: str,
    predicate: str,
    object: str,
    qualifiers: dict[str, Any],
    pmids: Iterable[str],
    infores: str = config.INFORES,
    negated: bool = False,
) -> Edge:
    """An edge this step adds, with its own provenance and an id derived from its statement."""
    slots: dict[str, Any] = dict(sorted(qualifiers.items()))
    if negated:
        slots["negated"] = True
    edge = Edge(
        subject=subject,
        predicate=predicate,
        object=object,
        primary_knowledge_source=infores,
        aggregator_knowledge_source=[],
        knowledge_level=config.NEW_EDGE_KNOWLEDGE_LEVEL,
        agent_type=config.NEW_EDGE_AGENT_TYPE,
        publications=[graphio.publication_of(p) for p in sorted(set(pmids), key=_pmid_order)],
        **slots,
    )
    edge.id = ID_PREFIX + graphio.edge_statement_id(edge)
    return edge


class _Pass:
    """One pass over the input edges: add supporting PMIDs, then the new edges."""

    def __init__(self, found: Findings, infores: str):
        self.found = found
        self.infores = infores
        self.wanted = set(found.contradict) | set(found.proposals)
        self.first: dict[str, tuple[str, str, str, dict[str, Any]]] = {}
        self.seen: set[str] = set()
        self.qualified_in_graph: set[str] = set()
        self.own_ids: set[str] = set()  # ids of edges this step added on an earlier run
        self.stats = {
            "edges_read": 0,
            "negated_in_input": 0,
            "edges_given_pmids": 0,
            "pmids_added": 0,
            "negated_edges_added": 0,
            "qualified_edges_added": 0,
        }

    def originals(self, rows: Iterable[dict[str, Any]], path: Path) -> Iterator[dict[str, Any]]:
        """The input edges as read, with supporting PMIDs added. Each is validated as an Edge to
        compute its statement, but written back as its own dict."""
        found, stats = self.found, self.stats
        for row in rows:
            stats["edges_read"] += 1
            try:
                edge = Edge.model_validate(row)
            except ValidationError as e:
                problem = e.errors()[0]
                where = ".".join(map(str, problem["loc"])) or "record"
                raise ApplyError(
                    f"edge {stats['edges_read']} of {path} is not a valid Edge: "
                    f"{where}: {problem['msg']}"
                ) from e
            if edge.id and edge.id.startswith(ID_PREFIX):
                self.own_ids.add(edge.id)
            if graphio.is_negated(edge):
                stats["negated_in_input"] += 1
                yield row
                continue
            sid = graphio.edge_statement_id(edge)
            qualifiers = graphio.qualifiers_of(edge)
            if qualifiers:
                self.qualified_in_graph.add(sid)
            if sid in self.wanted and sid not in self.first:
                self.first[sid] = (edge.subject, edge.predicate, edge.object, qualifiers)
            pmids = found.support.get(sid)
            if pmids is not None or sid in self.wanted:
                self.seen.add(sid)
            if pmids:
                have = {graphio.pmid_of(p) for p in edge.publications}
                added = sorted(pmids - have, key=_pmid_order)
                if added:
                    # Replacing the value keeps the key where it was; a missing key is appended.
                    publications = [*edge.publications, *map(graphio.publication_of, added)]
                    row = {**row, "publications": publications}
                    stats["edges_given_pmids"] += 1
                    stats["pmids_added"] += len(added)
            yield row

    def new_edges(self) -> Iterator[dict[str, Any]]:
        """Negated edges, then qualified ones, each sorted by id. Runs only once `originals` is
        exhausted, since it needs the first edge of each statement."""
        found, skipped = self.found, self.found.skipped
        negated: dict[str, Edge] = {}
        qualified: dict[str, tuple[tuple[str, str, str], dict[str, Any], set[str]]] = {}
        for sid in sorted(self.wanted):
            if sid not in self.first:
                continue  # an orphan, counted in `apply_labels`
            s, p, o, original = self.first[sid]
            if sid in found.contradict:
                edge = new_edge(s, p, o, original, found.contradict[sid], self.infores, True)
                negated[edge.id] = edge
            for key, pmids in sorted(found.proposals.get(sid, {}).items()):
                proposed = {k: v for k, v in json.loads(key).items() if v}
                merged = dict(sorted({**original, **proposed}.items()))
                if merged == original:
                    skipped["same_as_original"] += 1
                    continue
                # Proposals on different statements can merge to the same one: one edge.
                new_sid = graphio.statement_id(s, p, o, merged)
                entry = qualified.setdefault(new_sid, ((s, p, o), merged, set()))
                entry[2].update(pmids)
        for edge_id in sorted(negated):
            if edge_id in self.own_ids:
                skipped["already_in_graph"] += 1
                continue
            self.stats["negated_edges_added"] += 1
            yield negated[edge_id].model_dump(exclude_none=True)
        for new_sid in sorted(qualified):
            if new_sid in self.qualified_in_graph or ID_PREFIX + new_sid in self.own_ids:
                skipped["already_in_graph"] += 1
                continue
            (s, p, o), merged, pmids = qualified[new_sid]
            self.stats["qualified_edges_added"] += 1
            yield new_edge(s, p, o, merged, pmids, self.infores).model_dump(exclude_none=True)


def apply_labels(
    input_dir: Path,
    output_dir: Path,
    cache_dir: Path,
    work: Path,
    *,
    infores: str = config.INFORES,
    require_review: bool = True,
    model: str | None = None,
    prompt_version: str | None = None,
    reviewer: str | None = None,
    review_prompt_version: str | None = None,
) -> dict[str, Any]:
    """Write the labelled graph to `output_dir` and `apply_report.json` to `work`; return the
    report. Raises ApplyError on a problem with the inputs."""
    input_dir, output_dir, work = Path(input_dir), Path(output_dir), Path(work)
    if output_dir.resolve() == input_dir.resolve():
        raise ApplyError(f"output dir {output_dir} is the input dir; choose another")
    for name in (graphio.NODES, graphio.EDGES):
        if not graphio.graph_file(input_dir, name).exists():
            raise ApplyError(f"{input_dir / name} not found")
    if not infores.startswith("infores:"):
        raise ApplyError(f"--infores must be an infores CURIE, got {infores!r}")
    run = resolve_run(work, model, prompt_version, reviewer, review_prompt_version)
    label_path = Path(cache_dir) / config.LABEL_CACHE
    if not label_path.exists():
        raise ApplyError(f"no label cache at {label_path}; run `kgb-augment label` first")

    scope = read_scope(work)
    if scope is None:
        print(
            f"apply: warning: no {work / records.STATEMENTS}; applying every cached label of "
            "the run, whatever the current scope",
            file=sys.stderr,
        )
    with cache.LabelCache(label_path) as labels:
        if not has_labels(labels, run):
            raise ApplyError(
                f"no labels for {run.model} {run.prompt_version} in {label_path}; run "
                "`kgb-augment label`, or pass the --model and --prompt-version of a run"
            )
        found = collect_findings(labels, run, require_review, scope)
    waiting = found.skipped["unreviewed"]
    if waiting["contradicted"] or waiting["proposal"]:
        why = "the run has no review" if not run.reviewer else f"{run.reviewer} has not reviewed"
        print(
            f"apply: warning: {waiting['contradicted']} contradicted labels and "
            f"{waiting['proposal']} qualifier proposals await review ({why}); run "
            "`kgb-augment review`, or pass --no-review to apply them unreviewed",
            file=sys.stderr,
        )

    graph = _Pass(found, infores)
    output_dir.mkdir(parents=True, exist_ok=True)
    temps = [output_dir / f".{name}.tmp" for name in (graphio.NODES, graphio.EDGES)]
    nodes_in = graphio.graph_file(input_dir, graphio.NODES)
    edges_in = graphio.graph_file(input_dir, graphio.EDGES)
    try:
        nodes = write_records(temps[0], graphio.read_jsonl(nodes_in))
        # `new_edges` is a generator, so its body runs after `originals` has read every edge.
        edges = write_records(
            temps[1],
            itertools.chain(
                graph.originals(graphio.read_jsonl(edges_in), edges_in), graph.new_edges()
            ),
        )
        os.replace(temps[0], output_dir / graphio.NODES)
        os.replace(temps[1], output_dir / graphio.EDGES)
    finally:
        for temp in temps:
            temp.unlink(missing_ok=True)

    usable = set(found.support) | set(found.contradict) | set(found.proposals)
    found.skipped["orphan_statements"] = len(usable - graph.seen)
    report = {
        "run": run.model_dump() | {"require_review": require_review},
        "input": str(input_dir),
        "output": str(output_dir),
        "infores": infores,
        "nodes": nodes,
        **graph.stats,
        "statements_confirmed": len(set(found.support) & graph.seen),
        "edges_written": edges,
        "skipped": found.skipped,
    }
    records.write_json(work / APPLY_REPORT, report)
    return report


def summary(report: dict[str, Any]) -> list[str]:
    run, skipped = report["run"], report["skipped"]
    review = (
        f"reviewer={run['reviewer']} review_prompt={run['review_prompt_version']}"
        if run["reviewer"]
        else "no review"
    )
    lines = [
        f"run model={run['model']} prompt={run['prompt_version']} {review}",
        f"read {report['edges_read']} edges; added {report['pmids_added']} PMIDs to "
        f"{report['edges_given_pmids']} edges ({report['statements_confirmed']} statements)",
        f"added {report['negated_edges_added']} negated and "
        f"{report['qualified_edges_added']} qualified edges",
        "skipped: "
        + ", ".join(
            f"{k} {sum(v.values()) if isinstance(v, dict) else v}" for k, v in skipped.items()
        ),
        f"wrote {report['nodes']} nodes and {report['edges_written']} edges to {report['output']}",
    ]
    if not run["require_review"]:
        lines.insert(1, "--no-review: contradictions and proposals applied without a review")
    return [f"apply: {line}" for line in lines]


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--infores",
        default=config.INFORES,
        help="primary_knowledge_source of the new edges (default: %(default)s)",
    )
    parser.add_argument(
        "--no-review",
        action="store_true",
        help="apply contradictions and qualifier proposals that have no review, for runs "
        "whose second pass was skipped (a rejection still drops one)",
    )
    parser.add_argument("--model", help="bulk model of the run (default: from label_run.json)")
    parser.add_argument(
        "--prompt-version", help="label prompt version (default: from label_run.json)"
    )
    parser.add_argument("--reviewer", help="review model (default: from label_run.json)")
    parser.add_argument(
        "--review-prompt-version", help="review prompt version (default: from label_run.json)"
    )


def run(args: argparse.Namespace) -> int:
    try:
        report = apply_labels(
            args.input,
            args.output,
            args.cache_dir,
            args.work,
            infores=args.infores,
            require_review=not args.no_review,
            model=args.model,
            prompt_version=args.prompt_version,
            reviewer=args.reviewer,
            review_prompt_version=args.review_prompt_version,
        )
    except ApplyError as e:
        print(f"apply: {e}", file=sys.stderr)
        return 1
    print("\n".join(summary(report)))
    return 0
