import json
import shutil
from types import SimpleNamespace

import pytest

from agents.metadata import semantics
from agents.metadata.cli import main as cli_main


def croissant(*columns, extra=""):
    fields = [{"name": column} for column in columns]
    return {
        "name": "Acme",
        "description": extra,
        "recordSet": [{"@id": "rs", "name": "rs.tsv", "field": fields}],
    }


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    calls = []

    def fake_script(name, *args):
        calls.append(name)
        if name == "apply_plan.py":
            args[2].write_text("{}")
        failed = name == "check.py" and workspace_state["check_fails"]
        return SimpleNamespace(returncode=int(failed), stderr="")

    workspace_state = {"check_fails": False}
    monkeypatch.setattr(semantics, "_script", fake_script)
    source = tmp_path / "in" / "acme"
    source.mkdir(parents=True)
    (source / "croissant.json").write_text(json.dumps(croissant("a", "b")))
    tools = semantics._load_plan_tools()
    plan = {"layouts": [{"id": "x", "match": {"layout": tools.layout_id(["a", "b"])}}]}
    bundle = tmp_path / "out" / "acme"
    bundle.mkdir(parents=True)
    (bundle / semantics.PLAN).write_text(json.dumps(plan))
    return SimpleNamespace(
        input=tmp_path / "in",
        output=tmp_path / "out",
        source=source,
        bundle=bundle,
        calls=calls,
        state=workspace_state,
    )


def statuses(workspace):
    report = json.loads((workspace.output / "metadata_report.json").read_text())
    return {r["source"]: r for r in report["sources"]}


def test_unchanged_source_runs_nothing(workspace):
    assert semantics.run(workspace.input, workspace.output, None) == 0
    assert workspace.calls == ["apply_plan.py", "check.py"]

    workspace.calls.clear()
    assert semantics.run(workspace.input, workspace.output, None) == 0
    assert workspace.calls == []
    assert statuses(workspace)["acme"]["status"] == "unchanged"


def test_force_runs_an_unchanged_source(workspace):
    semantics.run(workspace.input, workspace.output, None)
    workspace.calls.clear()
    semantics.run(workspace.input, workspace.output, None, force=True)
    assert workspace.calls == ["apply_plan.py", "check.py"]


@pytest.mark.parametrize("changed", ["croissant", "plan", "answers", "skill"])
def test_each_input_triggers_a_rerun(workspace, monkeypatch, changed):
    semantics.run(workspace.input, workspace.output, None)
    workspace.calls.clear()
    if changed == "croissant":
        document = croissant("a", "b", extra="a new checksum")
        (workspace.source / "croissant.json").write_text(json.dumps(document))
    elif changed == "plan":
        plan = json.loads((workspace.bundle / semantics.PLAN).read_text())
        (workspace.bundle / semantics.PLAN).write_text(json.dumps({**plan, "note": "edited"}))
    elif changed == "answers":
        (workspace.source / semantics.ANSWERS).write_text("{}")
    else:
        monkeypatch.setattr(semantics, "skill_digest", lambda: "a new skill")

    semantics.run(workspace.input, workspace.output, None)
    assert workspace.calls == ["apply_plan.py", "check.py"]
    assert statuses(workspace)["acme"]["status"] == "passed"


def test_changed_columns_need_an_agent_and_leave_no_passing_state(workspace):
    semantics.run(workspace.input, workspace.output, None)
    workspace.calls.clear()
    (workspace.source / "croissant.json").write_text(json.dumps(croissant("a", "renamed")))

    assert semantics.run(workspace.input, workspace.output, None) == 1
    assert workspace.calls == []
    result = statuses(workspace)["acme"]
    assert result["status"] == "needs_agent"
    assert "no longer fits" in result["reason"]
    assert not (workspace.bundle / semantics.STATE).exists()


def test_a_new_layout_is_flagged_without_stopping(workspace):
    document = croissant("a", "b")
    document["recordSet"].append({"@id": "new", "name": "new.tsv", "field": [{"name": "c"}]})
    (workspace.source / "croissant.json").write_text(json.dumps(document))

    assert semantics.run(workspace.input, workspace.output, None) == 0
    assert len(statuses(workspace)["acme"]["unplanned_layouts"]) == 1

    semantics.run(workspace.input, workspace.output, None)
    assert statuses(workspace)["acme"]["status"] == "unchanged"
    assert len(statuses(workspace)["acme"]["unplanned_layouts"]) == 1


def test_a_failed_check_keeps_the_bundle_and_runs_again(workspace):
    workspace.state["check_fails"] = True
    semantics.run(workspace.input, workspace.output, None)
    assert statuses(workspace)["acme"]["status"] == "check_errors"
    assert (workspace.bundle / "croissant.semantic.json").exists()

    workspace.calls.clear()
    workspace.state["check_fails"] = False
    semantics.run(workspace.input, workspace.output, None)
    assert workspace.calls == ["apply_plan.py", "check.py"]


def test_plan_comes_from_earlier_bundles_only_when_the_source_has_none(workspace, tmp_path):
    prior = tmp_path / "prior" / "acme"
    prior.mkdir(parents=True)
    shutil.move(workspace.bundle / semantics.PLAN, prior / semantics.PLAN)
    assert semantics.run(workspace.input, workspace.output, tmp_path / "prior") == 0
    assert (workspace.bundle / semantics.PLAN).read_text() == (prior / semantics.PLAN).read_text()

    (workspace.bundle / semantics.PLAN).write_text(
        (prior / semantics.PLAN).read_text().replace("}", ', "edited": true}', 1)
    )
    edited = (workspace.bundle / semantics.PLAN).read_text()
    semantics.run(workspace.input, workspace.output, tmp_path / "prior")
    assert (workspace.bundle / semantics.PLAN).read_text() == edited


def test_no_plan_needs_an_agent(workspace):
    (workspace.bundle / semantics.PLAN).unlink()
    assert semantics.run(workspace.input, workspace.output, None) == 1
    assert statuses(workspace)["acme"]["status"] == "needs_agent"
    assert workspace.calls == []


def test_a_bundle_missing_from_the_input_is_reported(workspace):
    (workspace.output / "retired").mkdir()
    semantics.run(workspace.input, workspace.output, None)
    report = json.loads((workspace.output / "metadata_report.json").read_text())
    assert report["not_in_input"] == ["retired"]


def test_cli_reports_no_input(tmp_path, capsys):
    assert cli_main(["--input", str(tmp_path / "none"), "--output", str(tmp_path / "out")]) == 1
    assert "no croissants found" in capsys.readouterr().out
