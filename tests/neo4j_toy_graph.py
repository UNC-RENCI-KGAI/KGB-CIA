"""Load a toy graph into an empty Neo4j and write the MCP artifacts that describe it.

Used by `tests/neo4j_smoke.sh` (`make test-neo4j`, and CI) so the MCP server's Neo4j smoke test
runs against a real database without the full graph. The toy graph is shaped like an ORION dump:
every node has the `biolink:NamedThing` label plus its categories, every relationship's type is
its predicate, and every edge carries Biolink provenance and `kgbcia_source_records` that point
into the toy transform report written beside it.

    uv run python tests/neo4j_toy_graph.py --uri bolt://localhost:7687 --artifacts <dir>
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from neo4j import GraphDatabase

ANCHOR = "biolink:NamedThing"
PREDICATE = "biolink:directly_physically_interacts_with"
SOURCE = "infores:chembl"
MAPPING = "chembl.interacts"
COUNT = 5


def toy_nodes() -> list[dict]:
    compounds = [
        {
            "id": f"CHEBI:{i + 1}",
            "name": f"Compound {i + 1}",
            "labels": ["biolink:SmallMolecule", "biolink:ChemicalEntity"],
            "equivalent_identifiers": [f"CHEBI:{i + 1}"],
        }
        for i in range(COUNT)
    ]
    genes = [
        {
            "id": f"NCBIGene:{i + 100}",
            "name": f"Gene {i + 100}",
            "labels": ["biolink:Gene", "biolink:BiologicalEntity"],
            "equivalent_identifiers": [f"NCBIGene:{i + 100}"],
        }
        for i in range(COUNT)
    ]
    return compounds + genes


def toy_edges() -> list[dict]:
    edges = []
    for i in range(COUNT):
        edge = {
            "id": f"edge-{i}",
            "subject": f"CHEBI:{i + 1}",
            "object": f"NCBIGene:{i + 100}",
            "primary_knowledge_source": SOURCE,
            "aggregator_knowledge_source": ["infores:tcrd"],
            "knowledge_level": "knowledge_assertion",
            "agent_type": "data_analysis_pipeline",
            "publications": [f"PMID:{i + 1000}"],
            "kgbcia_source_records": [f"{MAPPING}#row-{i}"],
        }
        if i == 0:
            edge["kgbcia_flags"] = ["wrong_mechanism"]
            edge["kgbcia_flag_reasons"] = ["PMID:1000 reports another action"]
        edges.append(edge)
    return edges


def load(uri: str, user: str | None, password: str | None) -> None:
    auth = (user, password) if user else None
    with GraphDatabase.driver(uri, auth=auth) as driver, driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n").consume()
        for node in toy_nodes():
            labels = ":".join(f"`{label}`" for label in [ANCHOR, *node["labels"]])
            properties = {k: v for k, v in node.items() if k != "labels"}
            session.run(f"CREATE (n:{labels}) SET n = $properties", properties=properties)
        for edge in toy_edges():
            properties = {k: v for k, v in edge.items() if k not in ("subject", "object")}
            session.run(
                f"MATCH (s:`{ANCHOR}` {{id: $subject}}), (o:`{ANCHOR}` {{id: $object}}) "
                f"CREATE (s)-[r:`{PREDICATE}`]->(o) SET r = $properties",
                subject=edge["subject"],
                object=edge["object"],
                properties=properties,
            ).consume()
        session.run(
            f"CREATE FULLTEXT INDEX kgbcia_node_fulltext IF NOT EXISTS FOR (n:`{ANCHOR}`) "
            "ON EACH [n.name, n.synonym, n.equivalent_identifiers]"
        ).consume()
        session.run(
            f"CREATE INDEX kgbcia_node_id IF NOT EXISTS FOR (n:`{ANCHOR}`) ON (n.id)"
        ).consume()
        session.run("CALL db.awaitIndexes()").consume()


def statistics() -> dict:
    nodes, edges = toy_nodes(), toy_edges()
    categories = Counter(label for node in nodes for label in node["labels"])
    flags = Counter(flag for edge in edges for flag in edge.get("kgbcia_flags", []))
    return {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "categories": [{"category": c, "count": n} for c, n in sorted(categories.items())],
        "predicates": [{"predicate": PREDICATE, "count": len(edges)}],
        "primary_knowledge_sources": [{"primary_knowledge_source": SOURCE, "count": len(edges)}],
        "meta_graph": [
            {
                "subject_category": "biolink:SmallMolecule",
                "predicate": PREDICATE,
                "object_category": "biolink:Gene",
                "count": len(edges),
            }
        ],
        "properties": [
            {"category": "biolink:Gene", "property": "name", "example": "Gene 100"},
            {"category": "biolink:SmallMolecule", "property": "name", "example": "Compound 1"},
        ],
        "property_names": {
            "biolink:Gene": ["id", "name", "equivalent_identifiers"],
            "biolink:SmallMolecule": ["id", "name", "equivalent_identifiers"],
        },
        "edge_properties": [
            {"property": "kgbcia_flags", "example": ["wrong_mechanism"]},
            {"property": "kgbcia_flag_reasons", "example": ["PMID:1000 reports another action"]},
        ],
        "flag_counts": [{"flag": f, "count": n} for f, n in sorted(flags.items())],
    }


def transform_report() -> dict:
    return {
        "source": SOURCE,
        "source_version": "toy",
        "croissant": "sources/chembl/croissant.json",
        "mappings": [
            {
                "id": MAPPING,
                "file": "activities.tsv",
                "record_set": "activities",
                "produces": "edge",
                "subject_category": "biolink:SmallMolecule",
                "predicate": PREDICATE,
                "object_category": "biolink:Gene",
                "columns": {
                    "subject": [{"field": "compound", "name": "compound_id"}],
                    "object": [{"field": "target", "name": "target_id"}],
                },
                "constants": {"knowledge_level": "knowledge_assertion"},
            }
        ],
    }


def write_artifacts(root: Path) -> None:
    report = root / "reports" / "chembl" / "transform_report.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(transform_report(), indent=2))
    (root / "graph_metadata.json").write_text(
        json.dumps({"graph_id": "toy", "statistics": statistics()}, indent=2)
    )
    (root / "hubs.json").write_text(json.dumps({"hubs": []}))
    manifest = {
        "graph_version": "toy-1",
        "files": ["graph_metadata.json", "hubs.json", "reports/chembl/transform_report.json"],
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--uri", required=True, help="Bolt URI of an empty, writable Neo4j")
    parser.add_argument("--user", help="Neo4j user, if the database needs auth")
    parser.add_argument("--password", help="Neo4j password, with --user")
    parser.add_argument("--artifacts", type=Path, required=True, help="directory to write")
    args = parser.parse_args(argv)
    load(args.uri, args.user, args.password)
    write_artifacts(args.artifacts)
    print(
        f"loaded {len(toy_nodes())} nodes and {len(toy_edges())} edges; "
        f"artifacts in {args.artifacts}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
