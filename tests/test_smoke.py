from shared.pipeline import STAGES, main
from shared.schema import Edge, Graph, Node, Provenance


def test_dry_run_lists_every_stage(capsys):
    assert main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    for name, _ in STAGES:
        assert f"[{name}]" in out


def test_schema_round_trip():
    g = Graph(
        nodes=[Node(id="NCBIGene:1017", category="biolink:Gene", name="CDK2")],
        edges=[
            Edge(
                subject="CHEBI:45783",
                predicate="biolink:binds",
                object="NCBIGene:1017",
                provenance=Provenance(source="TCRD", file="tdl_info", columns=["lig", "prot"]),
            )
        ],
    )
    assert Graph.model_validate_json(g.model_dump_json()) == g
