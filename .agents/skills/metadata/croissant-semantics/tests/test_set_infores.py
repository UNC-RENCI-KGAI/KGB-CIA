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
    "infores:hgnc-old": {"name": "HGNC (old)", "status": "deprecated"},
}


def bundle(root: Path, name: str, kb: str, title: str, **plan) -> Path:
    path = root / name
    path.mkdir()
    (path / "semantics_plan.json").write_text(json.dumps({"kb": kb, **plan, "layouts": []}))
    (path / "croissant.semantic.json").write_text(json.dumps({"name": title, "recordSet": []}))
    return path


def test_suggest_then_apply_sets_only_the_infores(tmp_path, monkeypatch):
    monkeypatch.setattr(vocab, "infores", lambda: CATALOG)
    go = bundle(tmp_path, "gene_ontology", "go", "Gene Ontology")
    bundle(tmp_path, "bioplex", "bioplex", "BioPlex")
    bundle(tmp_path, "hgnc", "hgnc", "HGNC", infores="infores:hgnc")
    found = set_infores.bundles(tmp_path)

    assert set_infores.suggest(found["gene_ontology"])["infores"] == "infores:go"
    assert set_infores.suggest(found["hgnc"]) == {"infores": "infores:hgnc", "proposed": False}
    bioplex = set_infores.suggest(found["bioplex"])
    assert bioplex["infores"] == "infores:bioplex" and bioplex["proposed"] and bioplex["review"]

    assert set_infores.apply(go, {"infores": "infores:go"}, CATALOG) == [
        ("note", "infores:go (Gene Ontology), infores catalog v1.1.14")
    ]
    plan = json.loads((go / "semantics_plan.json").read_text())
    assert list(plan) == ["kb", "infores", "layouts"]  # placed after kb, nothing else changed
    croissant = json.loads((go / "croissant.semantic.json").read_text())
    assert croissant == {"name": "Gene Ontology", "recordSet": [], "rcv:infores": "infores:go"}

    [(level, msg)] = set_infores.apply(
        found["bioplex"], {"infores": "infores:bioplex", "proposed": True}, CATALOG
    )
    assert level == "warning" and "proposed" in msg
    croissant = json.loads((found["bioplex"] / "croissant.semantic.json").read_text())
    assert croissant["rcv:inforesProposed"] is True
