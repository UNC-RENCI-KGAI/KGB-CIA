#!/usr/bin/env python3
"""Step 1: collect the edges a question checks, with the PubMed abstracts each one cites.

    gather_evidence.py QUESTION [GRAPH] [--limit N]

GRAPH is a directory with nodes.jsonl and edges.jsonl (default data/transformation/merged), or a
source's ORION normalized_nodes.jsonl / normalized_edges.jsonl. An edge is selected when it
matches the question's `select` and cites at least one PubMed article, in `publications` (PMID:
CURIEs) or as a PubMed URL in any of its fields. Abstracts come from NCBI E-utilities and are
cached in the work directory, so a rerun fetches only what's new.

Writes data/cleaning/evidence/<name>/evidence.jsonl: one line per (edge, article) with the edge's
key, its subject and object names, the claim in words, and the article's title and abstract.
Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import question  # noqa: E402

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
BATCH = 200  # ids per E-utilities request
PAUSE = 0.4  # seconds between requests; NCBI allows 3 a second without an API key
PUBMED_URL = re.compile(r"(?:ncbi\.nlm\.nih\.gov/pubmed/|pubmed\.ncbi\.nlm\.nih\.gov/)(\d+)")
PMID_CURIE = re.compile(r"^PMID:(\d+)$")


def graph_files(graph: Path) -> tuple[Path, Path]:
    for nodes, edges in (
        ("nodes.jsonl", "edges.jsonl"),
        ("normalized_nodes.jsonl", "normalized_edges.jsonl"),
    ):
        if (graph / nodes).exists() and (graph / edges).exists():
            return graph / nodes, graph / edges
    raise SystemExit(f"{graph}: no nodes.jsonl and edges.jsonl (or ORION's normalized_*) here")


def pmids(edge: dict) -> list[str]:
    """PubMed ids an edge cites, from PMID CURIEs or PubMed URLs in any field."""
    found = []
    for value in edge.values():
        for item in value if isinstance(value, list) else [value]:
            if not isinstance(item, str):
                continue
            match = PMID_CURIE.match(item)
            found += [match.group(1)] if match else PUBMED_URL.findall(item)
    return sorted(set(found))


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
            ids = pmids(edge)
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


def parse_pubmed(xml_text: str) -> dict[str, dict]:
    """PMID -> {title, abstract} from an efetch XML response."""
    articles = {}
    for article in ET.fromstring(xml_text).iter("PubmedArticle"):
        pmid = article.findtext(".//MedlineCitation/PMID")
        title = (
            "".join(article.find(".//ArticleTitle").itertext())
            if article.find(".//ArticleTitle") is not None
            else ""
        )
        parts = []
        for section in article.iter("AbstractText"):
            text = "".join(section.itertext()).strip()
            label = section.get("Label")
            parts.append(f"{label}: {text}" if label else text)
        if pmid:
            articles[pmid] = {"title": title.strip(), "abstract": " ".join(parts)}
    return articles


def fetch_abstracts(ids: list[str], cache_path: Path) -> dict[str, dict]:
    """Abstracts for ids, from the cache where possible; new ones are appended to the cache."""
    cache = {}
    if cache_path.exists():
        with open(cache_path) as f:
            for line in f:
                record = json.loads(line)
                cache[record["pmid"]] = record
    todo = [i for i in ids if i not in cache]
    with open(cache_path, "a") as out:
        for start in range(0, len(todo), BATCH):
            batch = todo[start : start + BATCH]
            query = urllib.parse.urlencode(
                {"db": "pubmed", "id": ",".join(batch), "retmode": "xml", "tool": "kgbcia"}
            )
            request = urllib.request.Request(f"{EUTILS}?{query}", headers={"User-Agent": "kgbcia"})
            with urllib.request.urlopen(request, timeout=60) as response:
                articles = parse_pubmed(response.read().decode())
            for pmid in batch:  # cache misses too, so a withdrawn id isn't fetched every run
                record = {"pmid": pmid, **articles.get(pmid, {"title": "", "abstract": ""})}
                cache[pmid] = record
                out.write(json.dumps(record) + "\n")
            print(f"fetched {min(start + BATCH, len(todo))} of {len(todo)} articles", flush=True)
            time.sleep(PAUSE)
    return cache


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
    articles = fetch_abstracts(
        sorted({p for r in records for p in r["pmids"]}), work / "pubmed.jsonl"
    )

    written = no_abstract = 0
    with open(work / "evidence.jsonl", "w") as out:
        for r in records:
            edge = r["edge"]
            for pmid in r["pmids"]:
                article = articles.get(pmid, {})
                if not article.get("abstract"):
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
                            "text": f"{article['title']}\n\n{article['abstract']}",
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
