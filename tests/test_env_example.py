"""`.env.example` is the one list of environment variables the pipeline reads.

A key referenced anywhere in the code or docs but missing from the example is one nobody will
know to set; a value filled in there is a leaked secret. Both fail here.
"""

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
EXAMPLE = ROOT / ".env.example"
# every *_API_KEY, *_TOKEN or *_SECRET name, plus the few non-secret settings scripts read
PATTERN = re.compile(
    r"\b([A-Z][A-Z0-9_]*_(?:API_KEY|TOKEN|SECRET)|AZURE_OPENAI_BASE_URL|NCBI_EMAIL|KGB_PUBMED_CACHE)\b"
)
SKIP_DIRS = {".venv", ".git", "node_modules", "data", "__pycache__", ".pytest_cache", ".ruff_cache"}
SUFFIXES = {".py", ".md", ".json", ".toml", ".yml", ".yaml", ".sh"}
# names that aren't ours: a harness's own, or an example placeholder in a vendored doc
NOT_OURS = {"ANTHROPIC_IDENTITY_TOKEN", "ANTHROPIC_AUTH_TOKEN", "GITHUB_TOKEN", "ASHE_TOKEN"}


def example_names() -> set[str]:
    names = set()
    for line in EXAMPLE.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            names.add(line.split("=", 1)[0].strip())
    return names


def referenced_names() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in ROOT.rglob("*"):
        if path.suffix not in SUFFIXES or SKIP_DIRS & set(path.relative_to(ROOT).parts):
            continue
        if path == EXAMPLE or path == Path(__file__):
            continue
        for name in set(PATTERN.findall(path.read_text(errors="ignore"))):
            if name not in NOT_OURS:
                found.setdefault(name, set()).add(str(path.relative_to(ROOT)))
    return found


def test_example_exists_and_is_gitignored_exception():
    assert EXAMPLE.is_file()
    ignore = (ROOT / ".gitignore").read_text()
    assert ".env.*" in ignore and "!.env.example" in ignore, (
        ".env.example must survive the .env.* rule"
    )


def test_every_referenced_key_is_in_the_example():
    missing = {
        name: sorted(files)
        for name, files in referenced_names().items()
        if name not in example_names()
    }
    assert not missing, (
        "variables read somewhere but not listed in .env.example (add them with the step that "
        f"needs them): {missing}"
    )


def test_example_holds_no_secret_values():
    for line in EXAMPLE.read_text().splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip().endswith(("_API_KEY", "_TOKEN", "_SECRET")):
            assert value.strip() == "", f"{name.strip()} has a value in .env.example"


def test_old_renci_key_name_is_gone():
    old_name = "RENCI_AZURE" + "_API_KEY"  # spelled apart so this file doesn't trip the scan
    assert old_name not in referenced_names(), "use AZURE_OPENAI_API_KEY"
