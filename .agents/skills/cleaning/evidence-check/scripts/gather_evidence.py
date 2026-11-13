#!/usr/bin/env python3
"""Step 1: collect the edges a question checks, with the PubMed abstracts each one cites.

    gather_evidence.py QUESTION [GRAPH] [--limit N]

GRAPH is a directory with nodes.jsonl and edges.jsonl, or a source's ORION normalized_nodes.jsonl
/ normalized_edges.jsonl. Without it, the latest stage's graph that exists: data/augmentation,
data/filter, data/cleaning, then data/transformation/merged, so the check runs on what the final
cleaning pass will apply its drop lists to. An edge is selected when it matches the question's
`select` and cites at least one PubMed article, in `publications` (PMID: CURIEs) or as a PubMed
URL in any of its fields. Edges the augmentation stage has already checked against PubMed or
proposed (they carry `augmentation`) are skipped and counted, so no edge is paid for twice.

Abstracts come from NCBI E-utilities through `shared.pubmed`, whose cache the augmentation agent
shares (KGB_PUBMED_CACHE, default data/cache/pubmed.sqlite), so only what neither has fetched
goes over the network.

Writes data/cleaning/evidence/<name>/evidence.jsonl: one line per (edge, article) with the edge's
key, its subject and object (name, other names, identifier), the claim in words, and the
article's title and abstract. The other names come from NodeNorm (cached with the abstracts):
papers often name a drug by its development code ("FYX-051" for topiroxostat) or a protein by its
gene symbol, and without them the judge can't tell the paper is about this edge.
Run it from the repository root with `uv run python`.
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(1, str(Path(__file__).resolve().parents[5]))  # the repo root, for shared/
import question  # noqa: E402

from shared.pubmed import (  # noqa: E402
    PubMedCache,
    cited_pmids,
    entity_terms,
    fetch_abstracts,
    load_nodenorm,
)
from shared.pubmed.records import Entity  # noqa: E402

OTHER_NAMES = 5  # other names shown per node
# Babel names UniProtKB entries "ABL1_HUMAN Tyrosine-protein kinase ABL1 (sprot)".
UNIPROT_LABEL = re.compile(r"^[A-Z0-9]+_[A-Z0-9]+\s+(.+?)(?:\s*\((?:sprot|trembl)\))?$")
# the stages' outputs, latest first: the evidence check runs on the latest one there is
DEFAULT_GRAPHS = (
    Path("data/augmentation"),
    Path("data/filter"),
    Path("data/cleaning"),
    Path("data/transformation/merged"),
)


def default_graph() -> Path:
    for graph in DEFAULT_GRAPHS:
        if (graph / "edges.jsonl").exists():
            return graph
    raise SystemExit(
        "No graph given and none of " + ", ".join(map(str, DEFAULT_GRAPHS)) + " has edges.jsonl"
    )


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


def candidate_edges(q: dict, edges_path: Path, skipped: dict | None = None):
    """Selected edges that cite PubMed, as {edge, key, pmids}. Selected edges augmentation already
    checked or proposed are counted in `skipped` instead."""
    with open(edges_path) as f:
        for line in f:
            edge = json.loads(line)
            if not question.selects(q, edge):
                continue
            if edge.get("augmentation"):
                if skipped is not None:
                    skipped["augmentation"] = skipped.get("augmentation", 0) + 1
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


def select_edges(
    q: dict, edges_path: Path, limit: int | None, skipped: dict | None = None
) -> list[dict]:
    """Candidate edges, one per key, or with a limit the same spread-out sample on every run.

    An unmerged source can repeat an edge (one row per citation); those become one edge citing
    all of their articles, as ORION's merge would make them.
    """
    by_key: dict[tuple, dict] = {}
    for record in candidate_edges(q, edges_path, skipped):
        seen = by_key.get(record["key"])
        if seen:
            seen["pmids"] = sorted(set(seen["pmids"]) | set(record["pmids"]))
        else:
            by_key[record["key"]] = record
    if limit is None:
        return list(by_key.values())
    return heapq.nsmallest(limit, by_key.values(), key=lambda r: spread(r["key"]))


def tidy(name: str) -> str:
    """A node name as a paper would write it: UniProt's "XDH_HUMAN Xanthine ... (sprot)" -> the
    protein name."""
    match = UNIPROT_LABEL.match(name)
    return match.group(1) if match else name


def node_names(nodes_path: Path, wanted: set[str]) -> dict[str, str]:
    names = {}
    with open(nodes_path) as f:
        for line in f:
            node = json.loads(line)
            if node.get("id") in wanted and node.get("name"):
                names[node["id"]] = tidy(node["name"])
    return names


def other_names(names: dict[str, str], ids: set[str]) -> dict[str, list[str]]:
    """Up to OTHER_NAMES names for each node besides its own, from NodeNorm's labels for its
    equivalent identifiers (gene symbols, development codes, brand names). If NodeNorm can't be
    reached, nodes get none and the run goes on."""
    with PubMedCache() as cache:
        entries = load_nodenorm(sorted(ids), cache)
    found = {}
    for curie in ids:
        name = names.get(curie, "")
        terms, _ = entity_terms(Entity(id=curie, name=name or None), entries)
        seen = {name.lower()}
        found[curie] = []
        for term in map(tidy, terms):
            if term.lower() not in seen and len(found[curie]) < OTHER_NAMES:
                seen.add(term.lower())
                found[curie].append(term)
    return found


def describe(curie: str, names: dict[str, str], others: dict[str, list[str]]) -> str:
    """'Topiroxostat (also: FYX-051) [CHEBI:42614]'."""
    also = f" (also: {'; '.join(others[curie])})" if others.get(curie) else ""
    return f"{names.get(curie, '')}{also} [{curie}]"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("question", type=Path, help="question file (reference/question-format.md)")
    parser.add_argument(
        "graph", type=Path, nargs="?",
        help="graph directory (default: the first of data/augmentation, data/filter, "
        "data/cleaning, data/transformation/merged that has edges.jsonl)",
    )  # fmt: skip
    parser.add_argument("--limit", type=int, help="check only this many edges (a trial run)")
    args = parser.parse_args(argv)

    q = question.load(args.question)
    graph = args.graph or default_graph()
    print(f"Graph: {graph}")
    nodes_path, edges_path = graph_files(graph)
    skipped: dict[str, int] = {}
    records = select_edges(q, edges_path, args.limit, skipped)
    if skipped:
        print(f"Skipped {skipped['augmentation']:,} selected edges augmentation already checked.")
    if not records:
        print("No selected edge cites a PubMed article; nothing to check.")
        return 0
    ids = {e for r in records for e in (r["key"][0], r["key"][2])}
    names = node_names(nodes_path, ids)
    others = other_names(names, ids)
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
                            "subject": describe(edge["subject"], names, others),
                            "object": describe(edge["object"], names, others),
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
