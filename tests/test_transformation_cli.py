import json
from types import SimpleNamespace

from agents.transformation import transform
from agents.transformation.cli import main as cli_main


def test_discover_prefers_conventional_input(tmp_path, monkeypatch):
    monkeypatch.setattr(transform, "DATA_DIR", tmp_path)
    monkeypatch.setattr(transform, "CONVENTIONAL_INPUT", tmp_path / "metadata")
    bundle = tmp_path / "metadata" / "acme"
    bundle.mkdir(parents=True)
    (bundle / "croissant.semantic.json").write_text("{}")
    other = tmp_path / "semantics_croissant" / "other"
    other.mkdir(parents=True)
    (other / "croissant.semantic.json").write_text("{}")

    assert transform.discover_input_dir() == tmp_path / "metadata"


def test_discover_falls_back_to_one_unambiguous_root(tmp_path, monkeypatch):
    monkeypatch.setattr(transform, "DATA_DIR", tmp_path)
    monkeypatch.setattr(transform, "CONVENTIONAL_INPUT", tmp_path / "metadata")
    bundle = tmp_path / "semantics_croissant" / "acme"
    bundle.mkdir(parents=True)
    (bundle / "croissant.semantic.json").write_text("{}")

    assert transform.discover_input_dir() == tmp_path / "semantics_croissant"


def test_discover_raises_on_ambiguous_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(transform, "DATA_DIR", tmp_path)
    monkeypatch.setattr(transform, "CONVENTIONAL_INPUT", tmp_path / "metadata")
    for root in ("semantics_croissant", "other_bundles"):
        bundle = tmp_path / root / "acme"
        bundle.mkdir(parents=True)
        (bundle / "croissant.semantic.json").write_text("{}")

    try:
        transform.discover_input_dir()
        raise AssertionError("expected LookupError")
    except LookupError as error:
        assert "semantics_croissant" in str(error)
        assert "other_bundles" in str(error)


def test_run_with_no_input_autodetects(tmp_path, monkeypatch):
    monkeypatch.setattr(transform, "DATA_DIR", tmp_path)
    monkeypatch.setattr(transform, "CONVENTIONAL_INPUT", tmp_path / "metadata")
    bundle = tmp_path / "semantics_croissant" / "acme"
    bundle.mkdir(parents=True)
    (bundle / "croissant.semantic.json").write_text("{}")
    output_dir = tmp_path / "out"

    report = {
        "sources": [
            {
                "source_id": "Kgbcia_acme",
                "status": "needs_infores",
                "requires_infores": True,
                "skipped": [],
            }
        ],
        "errors": [],
    }
    monkeypatch.setattr(transform.generate_parser, "main", _stub_generation(report, output_dir))

    assert transform.run(None, output_dir) == 0


def test_cli_fails_fast_with_no_input(tmp_path):
    missing_input = tmp_path / "metadata"
    output = tmp_path / "out"
    assert cli_main(["--input", str(missing_input), "--output", str(output)]) == 1
    assert not output.exists()


def test_run_fails_when_input_has_no_bundles(tmp_path):
    input_dir = tmp_path / "metadata"
    input_dir.mkdir()
    assert transform.run(input_dir, tmp_path / "out") == 1


def _stub_generation(report, output_dir):
    def fake_main(argv):
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "generation_report.json").write_text(json.dumps(report))
        (output_dir / "run_orion.py").touch()
        return int(bool(report["errors"]))

    return fake_main


def test_run_reports_without_failing_on_needs_infores(tmp_path, monkeypatch):
    input_dir = tmp_path / "metadata" / "acme"
    input_dir.mkdir(parents=True)
    (input_dir / "croissant.semantic.json").write_text("{}")
    output_dir = tmp_path / "out"

    report = {
        "sources": [
            {
                "source_id": "Kgbcia_acme",
                "status": "needs_infores",
                "requires_infores": True,
                "skipped": [],
            }
        ],
        "errors": [],
    }
    monkeypatch.setattr(transform.generate_parser, "main", _stub_generation(report, output_dir))

    assert transform.run(input_dir, output_dir) == 0
    handoff = json.loads((output_dir / "transformation_report.json").read_text())
    assert handoff["sources"][0]["status"] == "needs_infores"
    assert "orion_exit_code" not in handoff["sources"][0]


def test_run_fails_when_orion_run_fails(tmp_path, monkeypatch):
    input_dir = tmp_path / "metadata" / "acme"
    input_dir.mkdir(parents=True)
    (input_dir / "croissant.semantic.json").write_text("{}")
    output_dir = tmp_path / "out"

    report = {
        "sources": [
            {
                "source_id": "Kgbcia_acme",
                "status": "ready",
                "requires_infores": False,
                "skipped": [],
            }
        ],
        "errors": [],
    }
    monkeypatch.setattr(transform.generate_parser, "main", _stub_generation(report, output_dir))
    monkeypatch.setattr(
        transform.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stderr="ORION blew up"),
    )

    assert transform.run(input_dir, output_dir) == 1
    handoff = json.loads((output_dir / "transformation_report.json").read_text())
    assert handoff["sources"][0]["orion_exit_code"] == 1
    assert handoff["sources"][0]["error"] == "ORION blew up"


def test_run_succeeds_when_orion_run_succeeds(tmp_path, monkeypatch, capsys):
    input_dir = tmp_path / "metadata" / "acme"
    input_dir.mkdir(parents=True)
    (input_dir / "croissant.semantic.json").write_text("{}")
    output_dir = tmp_path / "out"

    report = {
        "sources": [
            {
                "source_id": "Kgbcia_acme",
                "status": "partial",
                "requires_infores": False,
                "infores": "infores:acme",
                "infores_proposed": True,
                "skipped": [{"ref": "field_x", "name": "junk", "reason": "no meaning"}],
            }
        ],
        "errors": [],
    }
    monkeypatch.setattr(transform.generate_parser, "main", _stub_generation(report, output_dir))
    monkeypatch.setattr(
        transform.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stderr="")
    )

    assert transform.run(input_dir, output_dir) == 0
    handoff = json.loads((output_dir / "transformation_report.json").read_text())
    assert handoff["sources"][0]["orion_exit_code"] == 0
    # a proposed infores id runs, and is reported for registering
    assert handoff["sources"][0]["infores_proposed"] is True
    assert "proposed infores ids that need registering" in capsys.readouterr().out
