#!/usr/bin/env python3
"""Step 1: collect the edges a question checks, with the PubMed abstracts each one cites.

    gather_evidence.py QUESTION [GRAPH] [--limit N]

GRAPH is a directory with nodes.jsonl and edges.jsonl (default data/transformation/merged), or a
source's ORION normalized_nodes.jsonl / normalized_edges.jsonl. An edge is selected when it
matches the question's `select` and cites at least one PubMed article, in `publications` (PMID:
CURIEs) or as a PubMed URL in any of its fields. Abstracts come from NCBI E-utilities through
`shared.pubmed`, whose cache the augmentation agent shares (KGB_PUBMED_CACHE, default
data/cache/pubmed.sqlite), so only what neither has fetched goes over the network.

Writes data/cleaning/evidence/<name>/evidence.jsonl: one line per (edge, article) with the edge's
key, its subject and object names, the claim in words, and the article's title and abstract.
Run it from the repository root with `uv run python`.
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(1, str(Path(__file__).resolve().parents[5]))  # the repo root, for shared/
import question  # noqa: E402

from shared.pubmed import cited_pmids, fetch_abstracts  # noqa: E402


def graph_files(graph: Path) -> tuple[Path, Path]:
    for nodes, edges in (
        ("nodes.jsonl", "edges.jsonl"),
        ("normalized_nodes.jsonl", "normalized_edges.jsonl"),
    ):
        if (graph / nodes).exists() and (graph / edges).exists():
            return graph / nodes, graph / edges
    raise SystemExit(f"{graph}: no nodes.jsonl and edges.jsonl (or ORION's normalized_*) here")


def claim(edge: dict, names: dict[str, str]) -> str:
    """The edge in words: 'imatinib interacts with (inhibition) ABL1'."""
    predicate = edge["predicate"].removeprefix("biolink:").replace("_", " ")
    qualifiers = [
        str(v).removeprefix("biolink:").replace("_", " ")
        for k, v in sorted(edge.items())
        if k.endswith("_qualifier") and v
    ]
    middle = predicate + (f" ({', '.join(qualifiers)})" if qualifiers else "")
    subject = names.get(edge["subject"], edge["subject"])
    return f"{subject} {middle} {names.get(edge['object'], edge['object'])}"


def spread(key: tuple) -> str:
    """A stable pseudo-random order, so --limit takes the same spread-out edges every run."""
    return hashlib.sha1("\t".join(key).encode()).hexdigest()


def candidate_edges(q: dict, edges_path: Path):
    """Selected edges that cite PubMed, as {edge, key, pmids}."""
    with open(edges_path) as f:
        for line in f:
            edge = json.loads(line)
            if not question.selects(q, edge):
                continue
            ids = cited_pmids(edge)
            if ids:
                key = (
                    edge["subject"],
                    edge["predicate"],
                    edge["object"],
                    edge.get("primary_knowledge_source", ""),
                )
                yield {"edge": edge, "key": key, "pmids": ids}


def select_edges(q: dict, edges_path: Path, limit: int | None) -> list[dict]:
    """Candidate edges, one per key, or with a limit the same spread-out sample on every run.

    An unmerged source can repeat an edge (one row per citation); those become one edge citing
    all of their articles, as ORION's merge would make them.
    """
    by_key: dict[tuple, dict] = {}
    for record in candidate_edges(q, edges_path):
        seen = by_key.get(record["key"])
        if seen:
            seen["pmids"] = sorted(set(seen["pmids"]) | set(record["pmids"]))
        else:
            by_key[record["key"]] = record
    if limit is None:
        return list(by_key.values())
    return heapq.nsmallest(limit, by_key.values(), key=lambda r: spread(r["key"]))


def node_names(nodes_path: Path, wanted: set[str]) -> dict[str, str]:
    names = {}
    with open(nodes_path) as f:
        for line in f:
            node = json.loads(line)
            if node.get("id") in wanted and node.get("name"):
                names[node["id"]] = node["name"]
    return names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("question", type=Path, help="question file (reference/question-format.md)")
    parser.add_argument(
        "graph", type=Path, nargs="?", default=Path("data/transformation/merged"),
        help="graph directory (default: data/transformation/merged)",
    )  # fmt: skip
    parser.add_argument("--limit", type=int, help="check only this many edges (a trial run)")
    args = parser.parse_args(argv)

    q = question.load(args.question)
    nodes_path, edges_path = graph_files(args.graph)
    records = select_edges(q, edges_path, args.limit)
    if not records:
        print("No selected edge cites a PubMed article; nothing to check.")
        return 0
    names = node_names(nodes_path, {e for r in records for e in (r["key"][0], r["key"][2])})
    work = question.work_dir(q)
    work.mkdir(parents=True, exist_ok=True)
    articles = fetch_abstracts(sorted({p for r in records for p in r["pmids"]}, key=int))

    written = no_abstract = 0
    with open(work / "evidence.jsonl", "w") as out:
        for r in records:
            edge = r["edge"]
            for pmid in r["pmids"]:
                article = articles.get(pmid)
                if article is None or not article.has_text:
                    no_abstract += 1
                    continue
                out.write(
                    json.dumps(
                        {
                            "key": r["key"],
                            "pmid": pmid,
                            "based_on_text": question.text_based(edge),
                            "subject": f"{names.get(edge['subject'], '')} [{edge['subject']}]",
                            "object": f"{names.get(edge['object'], '')} [{edge['object']}]",
                            "claim": claim(edge, names),
                            "text": f"{article.title}\n\n{article.abstract}",
                        }
                    )
                    + "\n"
                )
                written += 1
    print(
        f"{len(records):,} edges, {written:,} edge-article pairs with an abstract "
        f"({no_abstract:,} citations had none). Wrote {work / 'evidence.jsonl'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
