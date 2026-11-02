"""Tests for set_infores.py: a knowledge base's infores id on finished bundles. No network."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import set_infores  # noqa: E402
import vocab  # noqa: E402

CATALOG = {
    "infores:go": {"name": "Gene Ontology", "status": "released"},
    "infores:hgnc": {"name": "HUGO Gene Nomenclature Committee", "status": "released"},
    "infores:bioplex-old": {"name": "BioPlex (old)", "status": "deprecated"},
}


def bundle(root: Path, name: str, kb: str, title: str, **plan) -> Path:
    path = root / name
    path.mkdir()
    (path / "semantics_plan.json").write_text(json.dumps({"kb": kb, **plan, "layouts": []}))
    (path / "croissant.semantic.json").write_text(json.dumps({"name": title, "recordSet": []}))
    return path


def test_ids_are_chosen_without_review_and_written_into_only_the_infores(tmp_path, monkeypatch):
    monkeypatch.setattr(vocab, "infores", lambda: CATALOG)
    go = bundle(tmp_path, "gene-ontology", "go", "Gene Ontology")
    bioplex = bundle(tmp_path, "bioplex", "bioplex", "BioPlex")
    hgnc = bundle(tmp_path, "hgnc", "hgnc", "HGNC", infores="infores:hgnc")

    assert set_infores.choose(go)["infores"] == "infores:go"
    assert set_infores.choose(hgnc) == {
        "infores": "infores:hgnc",
        "proposed": False,
        "why": "already in the plan",
    }
    choice = set_infores.choose(bioplex)  # only a deprecated, inexact entry: propose one
    assert choice["infores"] == "infores:bioplex" and choice["proposed"]
    assert "infores:bioplex-old" in choice["why"]
    assert set_infores.choose(bioplex, "infores:go")["proposed"] is False

    assert set_infores.apply(go, set_infores.choose(go), CATALOG) == [
        ("note", "infores:go (Gene Ontology), infores catalog v1.1.14")
    ]
    plan = json.loads((go / "semantics_plan.json").read_text())
    assert list(plan) == ["kb", "infores", "layouts"]  # placed after kb, nothing else changed
    croissant = json.loads((go / "croissant.semantic.json").read_text())
    assert croissant == {"name": "Gene Ontology", "recordSet": [], "rcv:infores": "infores:go"}

    [(level, _)] = set_infores.apply(bioplex, choice, CATALOG)
    assert level == "warning"  # proposed: flagged, not blocked
    croissant = json.loads((bioplex / "croissant.semantic.json").read_text())
    assert croissant["rcv:inforesProposed"] is True


def test_every_proposed_id_under_root_is_listed_for_registering(tmp_path, monkeypatch):
    monkeypatch.setattr(vocab, "infores", lambda: CATALOG)
    bundle(tmp_path, "bioplex", "bioplex", "BioPlex", infores="infores:bioplex",
           infores_proposed=True)  # fmt: skip
    bundle(tmp_path, "gene-ontology", "go", "Gene Ontology", infores="infores:go")
    found = set_infores.bundles(tmp_path)
    assert set_infores.write_to_register(tmp_path, found) == 0
    text = (tmp_path / "infores_to_register.yaml").read_text()
    assert "id: infores:bioplex" in text and "infores:go" not in text
    assert "information-resource-registry" in text
