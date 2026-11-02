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
}


def bundle(root: Path, name: str, kb: str, title: str, **plan) -> Path:
    path = root / name
    path.mkdir()
    (path / "semantics_plan.json").write_text(json.dumps({"kb": kb, "layouts": [], **plan}))
    (path / "croissant.semantic.json").write_text(json.dumps({"name": title, "recordSet": []}))
    return path


def test_ids_are_chosen_and_written_into_only_the_infores(tmp_path, monkeypatch):
    monkeypatch.setattr(vocab, "infores", lambda: CATALOG)
    go = bundle(tmp_path, "gene-ontology", "go", "Gene Ontology")
    bioplex = bundle(tmp_path, "bioplex", "bioplex", "BioPlex")
    hgnc = bundle(tmp_path, "hgnc", "hgnc", "HGNC", infores="infores:hgnc")

    # an id in --ids or already in the plan wins; otherwise the plan's evidence decides
    assert set_infores.choose(bioplex, "infores:go")["proposed"] is False
    assert set_infores.choose(hgnc)["why"] == "already in the plan"
    assert set_infores.choose(go)["infores"] == "infores:go"

    assert set_infores.apply(go, set_infores.choose(go), CATALOG) == [
        ("note", "infores:go (Gene Ontology), infores catalog v1.1.14")
    ]
    plan = json.loads((go / "semantics_plan.json").read_text())
    assert list(plan) == ["kb", "infores", "layouts"]  # placed after kb, nothing else changed
    croissant = json.loads((go / "croissant.semantic.json").read_text())
    assert croissant == {"name": "Gene Ontology", "recordSet": [], "rcv:infores": "infores:go"}

    # a proposed id is written and flagged, not blocked
    [(level, _)] = set_infores.apply(bioplex, set_infores.choose(bioplex), CATALOG)
    assert level == "warning"
    croissant = json.loads((bioplex / "croissant.semantic.json").read_text())
    assert croissant["rcv:inforesProposed"] is True


def test_only_the_infores_lines_of_a_plan_change():
    raw = '{\n  "kb": "eram",\n  "prefixes": {"HP": "x"},\n  "layouts": [\n    {"id": "a"}\n  ]\n}'
    plan = {"kb": "eram", "infores": "infores:eram", "prefixes": {"HP": "x"},
            "layouts": [{"id": "a"}]}  # fmt: skip
    written = set_infores.plan_text(raw, plan)
    assert written == raw.replace('"eram",\n', '"eram",\n  "infores": "infores:eram",\n')
    # set again, now proposed: the old lines are replaced, not repeated
    again = {**plan, "infores": "infores:x", "infores_proposed": True}
    rewritten = set_infores.plan_text(written, again)
    assert json.loads(rewritten) == again and rewritten.count('"infores"') == 1
    assert '  "infores_proposed": true,\n' in rewritten
    # an "infores" key deeper in the plan (a prefix declaration) is left alone
    nested = '{\n "kb": "c",\n "prefixes": {\n  "HP": "x",\n  "infores": "https://i/"\n }\n}'
    with_id = {
        "kb": "c",
        "infores": "infores:clinvar",
        "prefixes": {"HP": "x", "infores": "https://i/"},
    }
    assert json.loads(set_infores.plan_text(nested, with_id)) == with_id
    # a plan written on one line can't take a line: rewritten whole
    assert json.loads(set_infores.plan_text(json.dumps({"kb": "eram"}), {"kb": "eram"})) == {
        "kb": "eram"
    }


def test_every_proposed_id_under_root_is_listed_for_registering(tmp_path, monkeypatch):
    monkeypatch.setattr(vocab, "infores", lambda: CATALOG)
    bundle(tmp_path, "bioplex", "bioplex", "BioPlex", infores="infores:bioplex",
           infores_proposed=True)  # fmt: skip
    bundle(tmp_path, "gene-ontology", "go", "Gene Ontology", infores="infores:go")
    set_infores.write_to_register(tmp_path, set_infores.bundles(tmp_path))
    text = (tmp_path / "infores_to_register.yaml").read_text()
    assert "id: infores:bioplex" in text and "infores:go" not in text
    assert "information-resource-registry" in text
