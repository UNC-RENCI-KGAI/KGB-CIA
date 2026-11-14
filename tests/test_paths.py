"""The cache root (`shared.paths`) and the croissant-semantics skill's copy of it."""

import importlib.util
import shutil
from pathlib import Path

from shared import paths

SKILL_COMMON = paths.REPO_ROOT / ".agents/skills/metadata/croissant-semantics/scripts/common.py"


def _load_common(path: Path):
    spec = importlib.util.spec_from_file_location("croissant_common", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_root_is_the_repositorys_data_cache_unless_the_environment_names_one(
    monkeypatch, tmp_path
):
    monkeypatch.delenv(paths.CACHE_ROOT_ENV, raising=False)
    monkeypatch.chdir(tmp_path)  # not where the command runs
    assert paths.cache_root() == paths.REPO_ROOT / "data" / "cache"
    assert paths.cache_dir("pubmed") == paths.REPO_ROOT / "data" / "cache" / "pubmed"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(paths.CACHE_ROOT_ENV, "~/caches")
    assert paths.cache_dir("augmentation") == tmp_path / "home" / "caches" / "augmentation"


def test_croissant_semantics_finds_the_same_root(monkeypatch, tmp_path):
    monkeypatch.delenv(paths.CACHE_ROOT_ENV, raising=False)
    common = _load_common(SKILL_COMMON)
    assert common.CACHE_ROOT_ENV == paths.CACHE_ROOT_ENV
    assert common.cache_dir() == paths.cache_dir("croissant-semantics")
    monkeypatch.setenv(paths.CACHE_ROOT_ENV, str(tmp_path / "root"))
    assert common.cache_dir() == paths.cache_dir("croissant-semantics")


def test_croissant_semantics_on_its_own_caches_in_the_home_directory(monkeypatch, tmp_path):
    monkeypatch.delenv(paths.CACHE_ROOT_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    copy = tmp_path / "elsewhere" / "common.py"
    copy.parent.mkdir()
    shutil.copy(SKILL_COMMON, copy)
    assert _load_common(copy).cache_dir() == tmp_path / "home" / ".cache" / "croissant-semantics"
