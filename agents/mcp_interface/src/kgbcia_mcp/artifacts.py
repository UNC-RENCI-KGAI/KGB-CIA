"""Load public lineage artifacts from a directory or a verified tarball URL."""

from __future__ import annotations

import hashlib
import json
import os
import tarfile
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, Field

from .report_models import TransformReport


class Manifest(BaseModel):
    """Contents and graph release represented by an artifact bundle."""

    graph_version: str
    files: list[str] = Field(min_length=1)


@dataclass(frozen=True)
class ArtifactBundle:
    root: Path
    manifest: Manifest
    graph_metadata: dict[str, Any]
    hubs: Any
    reports: tuple[TransformReport, ...]
    description: dict[str, Any] | None = None


def resolve_artifacts(
    source: str | os.PathLike[str],
    checksum: str | None = None,
    cache_dir: Path | None = None,
) -> Path:
    """Resolve a local directory or download and safely unpack an HTTP(S) tarball once."""

    source_text = os.fspath(source)
    parsed = urllib.parse.urlparse(source_text)
    if parsed.scheme not in {"http", "https"}:
        path = Path(source_text).expanduser()
        if not path.is_dir():
            raise ValueError(f"artifact directory does not exist: {path}")
        return path.resolve()

    if not checksum:
        raise ValueError("KGB_MCP_ARTIFACTS_SHA256 is required for artifact URLs")
    expected = checksum.lower()
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise ValueError("artifact checksum must be 64 lowercase or uppercase hexadecimal digits")

    if cache_dir is not None:
        cache_root = cache_dir
    elif root := os.environ.get("KGB_CACHE_DIR"):
        cache_root = Path(root).expanduser() / "mcp-artifacts"
    else:
        cache_root = Path.home() / ".cache" / "kgbcia-mcp" / "artifacts"
    destination = cache_root / expected
    if (destination / "manifest.json").is_file():
        return destination

    cache_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="download-", dir=cache_root) as temporary:
        temp_root = Path(temporary)
        archive = temp_root / "artifacts.tar"
        digest = hashlib.sha256()
        with urllib.request.urlopen(source_text) as response, archive.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
        actual = digest.hexdigest()
        if actual != expected:
            raise ValueError(f"artifact checksum mismatch: expected {expected}, got {actual}")

        extracted = temp_root / "extracted"
        extracted.mkdir()
        _safe_extract(archive, extracted)
        if not (extracted / "manifest.json").is_file():
            raise ValueError("artifact tarball has no manifest.json at its root")
        try:
            extracted.rename(destination)
        except FileExistsError:
            pass
    return destination


def load_artifacts(
    source: str | os.PathLike[str],
    checksum: str | None = None,
    cache_dir: Path | None = None,
) -> ArtifactBundle:
    """Load and validate the manifest, metadata, hubs and transformation reports."""

    root = resolve_artifacts(source, checksum, cache_dir)
    manifest = Manifest.model_validate_json((root / "manifest.json").read_text())
    listed = {_validate_relative_path(item) for item in manifest.files}
    required = {"graph_metadata.json", "hubs.json"}
    missing_names = sorted(required - listed)
    if missing_names:
        raise ValueError(f"manifest is missing required files: {', '.join(missing_names)}")
    missing_files = sorted(name for name in listed if not (root / name).is_file())
    if missing_files:
        raise ValueError(f"artifact files are missing: {', '.join(missing_files)}")

    report_files = sorted((root / "reports").glob("**/transform_report.json"))
    unlisted_reports = [str(path.relative_to(root)) for path in report_files]
    unlisted_reports = sorted(name for name in unlisted_reports if name not in listed)
    if unlisted_reports:
        raise ValueError(f"reports are absent from manifest: {', '.join(unlisted_reports)}")

    graph_metadata = _read_json_object(root / "graph_metadata.json")
    hubs = json.loads((root / "hubs.json").read_text())
    reports = tuple(TransformReport.model_validate_json(path.read_text()) for path in report_files)
    description_path = root / "description.json"
    description = _read_json_object(description_path) if description_path.is_file() else None
    return ArtifactBundle(root, manifest, graph_metadata, hubs, reports, description)


def _validate_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value in {"", "."}:
        raise ValueError(f"manifest contains unsafe path: {value!r}")
    return path.as_posix()


def _read_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _safe_extract(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, mode="r:*") as tar:
        for member in tar.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
                raise ValueError(f"unsafe path in artifact tarball: {member.name!r}")
        tar.extractall(destination, filter="data")
