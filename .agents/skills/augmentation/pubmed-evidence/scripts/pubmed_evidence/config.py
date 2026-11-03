"""Defaults for every step, in one place.

`kgb-augment` must run with no arguments (the pipeline calls `main([])`), so every path, model
and cap has a default here. The CLI overrides any of them; a few also read an environment
variable so a shared setup (the RENCI drive, an NCBI key) needs no flags.
"""

from __future__ import annotations

import os
from pathlib import Path

# The augmentation agent's infores ID. Every new edge gets it as `primary_knowledge_source`;
# `--infores` overrides it.
INFORES = "infores:kgbcia-augmentor"
NEW_EDGE_KNOWLEDGE_LEVEL = "knowledge_assertion"
NEW_EDGE_AGENT_TYPE = "text_mining_agent"

# The filter stage's output and this stage's output. Paths are relative to where the command
# runs; the pipeline runs from the repository root.
DEFAULT_INPUT = Path(os.environ.get("KGB_AUGMENT_INPUT", "data/filter"))
DEFAULT_OUTPUT = Path(os.environ.get("KGB_AUGMENT_OUTPUT", "data/augmentation"))
# Intermediate files of each step: `<output>/work` unless given.
WORK_SUBDIR = "work"
# The PubMed and label caches. Point this at the shared RENCI drive to share them.
DEFAULT_CACHE_DIR = Path(os.environ.get("KGB_AUGMENT_CACHE_DIR", "data/augmentation/cache"))
PUBMED_CACHE = "pubmed.sqlite"
LABEL_CACHE = "labels.sqlite"

# Step 2: pairs kept per capped rule (gene-gene, gene-phenotype), best-scored first.
CAPPED_LIMIT = 10_000

# Step 3: abstracts per pair from the search, by relevance. Source PMIDs come on top.
MAX_ABSTRACTS = 20
MAX_SYNONYMS = 8
NCBI_API_KEY = "NCBI_API_KEY"
NCBI_EMAIL = "NCBI_EMAIL"
NCBI_TOOL = "kgb-cia"
EUTILS_URL = os.environ.get("EUTILS_URL", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils")

# Step 5: models. The gold set decides these; until then, the cheapest candidates.
BULK_MODEL = os.environ.get("KGB_AUGMENT_MODEL", "gpt-5.6-luna")
REVIEW_MODEL = os.environ.get("KGB_AUGMENT_REVIEW_MODEL", "gpt-5.6-terra")
LLM_API_KEY = "RENCI_AZURE_API_KEY"
LLM_PROVIDER = "renci-azure"
LLM_BASE_URL = "https://renci-analytics.openai.azure.com/openai/v1"
ABSTRACTS_PER_CALL = 20
WORKERS = 8

SKILL_DIR = Path(__file__).resolve().parents[2]
PROMPTS_DIR = SKILL_DIR / "prompts"
LABEL_PROMPT = PROMPTS_DIR / "label.md"
REVIEW_PROMPT = PROMPTS_DIR / "review.md"

# `.pi/models.json` at the repository root lists the RENCI models with their costs. The skill
# lives in the repository, under `.agents/skills/augmentation/`.
REPO_ROOT = SKILL_DIR.parents[3]
PI_MODELS = [REPO_ROOT / ".pi" / "models.json", Path.home() / ".pi" / "agent" / "models.json"]


def work_dir(output: Path, work: Path | None = None) -> Path:
    return work if work is not None else output / WORK_SUBDIR
