"""Tests for the shared-skill Claude symlink bridge."""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync_claude_skills.py"


def make_skill(root: Path, relative: str) -> Path:
    skill = root / ".agents" / "skills" / relative
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: testing\ndescription: Test skill\n---\n")
    return skill


def run_sync(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--project-root", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_flat_links_are_relative_and_idempotent(tmp_path):
    skill = make_skill(tmp_path, "transformation/testing")
    bundled = skill / "references" / "example"
    bundled.mkdir(parents=True)
    (bundled / "SKILL.md").write_text("Not a separate skill")
    assert run_sync(tmp_path).returncode == 0
    link = tmp_path / ".claude" / "skills" / "testing"
    assert link.is_symlink()
    assert link.resolve() == skill
    assert not link.readlink().is_absolute()
    assert not (link.parent / "example").exists()
    result = run_sync(tmp_path)
    assert result.returncode == 0
    assert "0 new link(s)" in result.stdout


def test_duplicate_names_fail_before_writing(tmp_path):
    make_skill(tmp_path, "cleaning/testing")
    make_skill(tmp_path, "filter/testing")
    result = run_sync(tmp_path)
    assert result.returncode == 1
    assert "Duplicate skill name" in result.stderr
    assert not (tmp_path / ".claude").exists()


def test_existing_content_is_preserved(tmp_path):
    make_skill(tmp_path, "filter/testing")
    make_skill(tmp_path, "filter/another")
    destination = tmp_path / ".claude" / "skills"
    destination.mkdir(parents=True)
    existing = destination / "testing"
    existing.write_text("Keep me")
    assert run_sync(tmp_path).returncode == 1
    assert existing.read_text() == "Keep me"
    assert not (destination / "another").exists()


def test_dry_run_does_not_create_destination(tmp_path):
    make_skill(tmp_path, "metadata/testing")
    result = run_sync(tmp_path, "--dry-run")
    assert result.returncode == 0
    assert "Would link" in result.stdout
    assert not (tmp_path / ".claude").exists()


def test_symlinked_destination_is_rejected(tmp_path):
    make_skill(tmp_path, "metadata/testing")
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "skills").symlink_to(tmp_path / ".agents" / "skills", target_is_directory=True)
    result = run_sync(tmp_path)
    assert result.returncode == 1
    assert "Destination must be a real directory" in result.stderr


def test_broken_symlink_is_not_overwritten(tmp_path):
    make_skill(tmp_path, "metadata/testing")
    destination = tmp_path / ".claude" / "skills"
    destination.mkdir(parents=True)
    link = destination / "testing"
    link.symlink_to("missing")
    assert run_sync(tmp_path).returncode == 1
    assert link.readlink() == Path("missing")


def test_prune_removes_only_broken_internal_links(tmp_path):
    make_skill(tmp_path, "metadata/testing")
    assert run_sync(tmp_path).returncode == 0
    destination = tmp_path / ".claude" / "skills"
    stale = destination / "deleted"
    stale.symlink_to("../../.agents/skills/metadata/deleted")
    unrelated = destination / "unrelated"
    unrelated.symlink_to(tmp_path / "missing")
    regular = destination / "notes.md"
    regular.write_text("Keep me")
    directory = destination / "manual"
    directory.mkdir()
    result = run_sync(tmp_path, "--prune")
    assert result.returncode == 0
    assert "1 link(s) pruned" in result.stdout
    assert not stale.is_symlink()
    assert unrelated.is_symlink()
    assert regular.read_text() == "Keep me"
    assert directory.is_dir()
    assert (destination / "testing").is_symlink()


def test_prune_dry_run_preserves_stale_links(tmp_path):
    make_skill(tmp_path, "metadata/testing")
    assert run_sync(tmp_path).returncode == 0
    stale = tmp_path / ".claude" / "skills" / "deleted"
    stale.symlink_to("../../.agents/skills/metadata/deleted")
    result = run_sync(tmp_path, "--prune", "--dry-run")
    assert result.returncode == 0
    assert "Would prune" in result.stdout
    assert stale.is_symlink()


def test_prune_replaces_broken_link_after_skill_move(tmp_path):
    old = make_skill(tmp_path, "metadata/testing")
    assert run_sync(tmp_path).returncode == 0
    new = tmp_path / ".agents" / "skills" / "cleaning" / "testing"
    new.parent.mkdir()
    old.rename(new)
    assert run_sync(tmp_path).returncode == 1  # Pruning is opt-in.
    result = run_sync(tmp_path, "--prune")
    assert result.returncode == 0
    assert (tmp_path / ".claude" / "skills" / "testing").resolve() == new


def test_prune_validates_collisions_before_deleting(tmp_path):
    make_skill(tmp_path, "metadata/testing")
    destination = tmp_path / ".claude" / "skills"
    destination.mkdir(parents=True)
    (destination / "testing").write_text("Keep me")
    stale = destination / "deleted"
    stale.symlink_to("../../.agents/skills/metadata/deleted")
    assert run_sync(tmp_path, "--prune").returncode == 1
    assert stale.is_symlink()


def test_prune_does_not_follow_escape_symlink(tmp_path):
    make_skill(tmp_path, "metadata/testing")
    assert run_sync(tmp_path).returncode == 0
    (tmp_path / ".agents" / "skills" / "escape").symlink_to(tmp_path / "outside")
    link = tmp_path / ".claude" / "skills" / "external"
    link.symlink_to("../../.agents/skills/escape/missing")
    assert run_sync(tmp_path, "--prune").returncode == 0
    assert link.is_symlink()
