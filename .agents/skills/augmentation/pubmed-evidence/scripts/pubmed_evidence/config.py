"""Defaults for every step, in one place.

`kgb-augment` must run with no arguments (the pipeline calls `main([])`), so every path, model
and cap has a default here. README.md, "Options" and "Environment", lists the flags and
variables that override them. PubMed's settings (the cache file, the NCBI key and email) are
`shared.pubmed`'s.
"""

from __future__ import annotations

import functools
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from shared.paths import DEFAULT_CACHE_ROOT, REPO_ROOT, cache_dir

# The provenance of every edge the agent proposes (DESIGN.md, "What step 6 writes").
# `supporting_data_source` is written as a one-item list: the Biolink slot is multivalued.
INFORES = "infores:kgbcia-augmentor"
SUPPORTING_DATA_SOURCE = "infores:pubmed"
NEW_EDGE_KNOWLEDGE_LEVEL = "knowledge_assertion"
NEW_EDGE_AGENT_TYPE = "text_mining_agent"

# The filter stage's output and this stage's, relative to where the command runs (the pipeline
# runs from the repository root).
DEFAULT_INPUT = Path(os.environ.get("KGB_AUGMENT_INPUT", "data/filter"))
DEFAULT_OUTPUT = Path(os.environ.get("KGB_AUGMENT_OUTPUT", "data/augmentation"))
# Intermediate files of each step: `<output>/work` unless given.
WORK_SUBDIR = "work"
# The variable naming the directory of the label cache and the UBERON release
# (`default_cache_dir`; reference/work-files.md, "Caches").
CACHE_DIR_ENV = "KGB_AUGMENT_CACHE_DIR"
# The default with KGB_CACHE_DIR unset.
DEFAULT_CACHE_DIR = DEFAULT_CACHE_ROOT / "augmentation"
# The old default, relative to the working directory and to the repository root. One that holds
# a label cache is used while the default holds none, so its claims aren't paid for again.
LEGACY_CACHE_DIRS = (Path("data/augmentation/cache"), REPO_ROOT / "data" / "augmentation" / "cache")
LABEL_CACHE = "labels.sqlite"


def default_cache_dir() -> Path:
    """KGB_AUGMENT_CACHE_DIR if set (`~` expanded), else augmentation/ under the cache root
    (`shared.paths.cache_dir`); but one of LEGACY_CACHE_DIRS, with a warning to move it, while
    it holds a label cache and the default doesn't."""
    if value := os.environ.get(CACHE_DIR_ENV):
        return Path(value).expanduser()
    path = cache_dir("augmentation")
    if not (path / LABEL_CACHE).exists():
        for old in LEGACY_CACHE_DIRS:
            if (old / LABEL_CACHE).exists():
                _warn_legacy(old.resolve(), path)
                return old
    return path


@functools.cache
def _warn_legacy(old: Path, new: Path) -> None:
    """Once a process for each old cache directory: where it should go."""
    print(
        f"[augmentation] warning: using the label cache at its old default, {old}; move its "
        f"files to the new one (mkdir -p {new} && mv {old}/* {new}/), or set {CACHE_DIR_ENV} "
        "to it",
        file=sys.stderr,
    )


# The UBERON release the ground step pins, and its file name in the cache directory (DESIGN.md,
# "Step 4: Grounding anatomy").
UBERON_FILE = "uberon-basic.obo"
UBERON_URL = "http://purl.obolibrary.org/obo/uberon/releases/2026-11-05/uberon-basic.obo"

# Step 1: pairs kept per capped rule, best-scored first (DESIGN.md, "Step 1: Which edges to
# query").
CAPPED_LIMIT = 10_000

# Step 3's source screen: a PMID a pair has only as a source citation isn't read when at least
# this many pairs of the selection cite it (`Pair.source_pmid_pairs`), unless its title and
# abstract name both nodes. None turns the screen off. See DESIGN.md, "Source papers many pairs
# cite". A GWAS catalog paper is cited by 30,000-116,000 pairs of the Nov 13 2026 search.
SOURCE_SCREEN_MIN_PAIRS: int | None = 100

# Step 4: the RENCI Name Resolver, which grounds the anatomy claims name.
NAMERES_URL = os.environ.get("NAMERES_URL", "https://name-resolution-sri.renci.org")

# Steps 3 and 5: models. Each must be in MODELS, below.
BULK_MODEL = os.environ.get("KGB_AUGMENT_MODEL", "gpt-6-luna")
# Step 5's reviewer, at REVIEW_EFFORT (DESIGN.md, "How the agent runs", "Second pass").
REVIEW_MODEL = os.environ.get("KGB_AUGMENT_REVIEW_MODEL", "gpt-6-luna")
# The effort review sends when no --reasoning-effort is given, whatever the model (None: each
# model's own in MODELS). As an override it follows `llm.run_effort`: a model that takes no
# effort is sent none, and one whose `efforts` don't list it is an error before any call.
# medium is gpt-5.6-terra's own in MODELS, so terra's reviews have the same prompt version
# with or without it.
REVIEW_EFFORT: str | None = "medium"
# The model that repairs the quotes code can't place (DESIGN.md, "Quotes and repair"). Repairs
# are cached under their repairer, so changing it starts the model's repairs afresh.
REPAIR_MODEL = os.environ.get("KGB_AUGMENT_REPAIR_MODEL", "gpt-5.6-terra")
# The most abstracts and review items one call sends; a pair with more is split across calls.
ABSTRACTS_PER_CALL = 20
REVIEW_ITEMS_PER_CALL = 40
# Parallel calls for label (its quote repair too) and review (DESIGN.md, "How the agent runs",
# "Parallel calls"). Azure counts each call's prompt plus its output reservation against the
# tokens-a-minute limit, about 27k tokens a label call. gpt-5.6-terra's limits weren't measured.
WORKERS = 32
# The full run labels only as many pairs as this cap on step 3's estimated cost, in US dollars,
# allows. Pairs already labeled cost nothing.
FULL_RUN_LABEL_BUDGET = 20.0


@dataclass(frozen=True)
class Price:
    """USD per million tokens: uncached input, output, and the prompt cache's reads and
    writes."""

    input: float
    output: float
    cache_read: float
    cache_write: float


# The reasoning effort that sends no effort setting at all, so the service's default applies
# (`llm.sent_effort`). It isn't 'none', a level some models take and are sent as such
# (DESIGN.md, "How the agent runs", "Reasoning effort").
OMIT_EFFORT = "omit"

# The efforts each API takes, where known. gpt-6-luna's were probed on the RENCI endpoint on
# 2026-11-16 (it rejects 'minimal'). Claude's `output_config.effort` has no 'none'.
GPT_6_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class Model:
    """A model the steps can call: how to reach it, what it costs, and the settings a call
    sends unless the step's flags override them."""

    id: str
    # "openai": the OpenAI v1 Responses API; "anthropic": Anthropic's Messages API
    backend: Literal["openai", "anthropic"]
    base_url: str | None  # None: the SDK's own (for Claude, Anthropic's API)
    api_key: str  # the environment variable holding the key
    price: Price
    price_source: str  # where and when the price was copied from
    reasoning: bool = True  # takes a reasoning effort
    effort: str | None = None  # the effort sent by default; None sends none
    max_output_tokens: int = 16000  # per call, reasoning included
    # (prompt tokens, price): a call with a longer prompt is billed at this price instead.
    long_prompt: tuple[int, Price] | None = None
    fallbacks: bool = False  # the service may answer with another model when this one refuses
    # The efforts its API takes, where known: any other is an error before a call is sent.
    # None: not known, so an effort is sent as given and the service judges it.
    efforts: tuple[str, ...] | None = None


# Azure OpenAI deployments, under Pi's standard variable names. The ids and prices are those
# `.pi/models.json` lists for the project's own endpoint, the default URL; a price change there
# has to be copied here by hand. AZURE_OPENAI_BASE_URL names another resource, with models
# deployed under these ids.
AZURE_KEY_VAR = "AZURE_OPENAI_API_KEY"
AZURE_URL_VAR = "AZURE_OPENAI_BASE_URL"
AZURE_DEFAULT_URL = "https://renci-analytics.openai.azure.com/openai/v1"
# `or`: an empty value, as a copied .env.example leaves it, means the default
AZURE_URL = os.environ.get(AZURE_URL_VAR) or AZURE_DEFAULT_URL
_PI = ".pi/models.json, 2026-11-03"


def _azure(
    id: str,
    price: Price,
    effort: str | None = "medium",
    reasoning: bool = True,
    efforts: tuple[str, ...] | None = None,
) -> Model:
    return Model(
        id, "openai", AZURE_URL, AZURE_KEY_VAR, price, _PI, reasoning, effort, efforts=efforts
    )


# Claude models, on Anthropic's own API. The SDK reads the key, or an `ant auth login` profile.
ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"
_ANTHROPIC = "platform.claude.com/docs/en/about-claude/pricing, 2026-11-03"


def _claude(
    id: str,
    price: Price,
    effort: str,
    fallbacks: bool = False,
    long_prompt: tuple[int, Price] | None = None,
) -> Model:
    return Model(
        id,
        "anthropic",
        None,
        ANTHROPIC_API_KEY,
        price,
        _ANTHROPIC,
        effort=effort,
        long_prompt=long_prompt,
        fallbacks=fallbacks,
        efforts=CLAUDE_EFFORTS,
    )


MODELS: dict[str, Model] = {
    m.id: m
    for m in (
        # Bulk labeling candidates: a low effort, since step 3 sends many calls. gpt-6-luna
        # also reviews, at REVIEW_EFFORT.
        _azure("gpt-6-luna", Price(0.1, 0.5, 0.01, 0.125), "low", efforts=GPT_6_EFFORTS),
        _azure("gpt-5.6-luna", Price(0.2, 1.2, 0.02, 0.25), "low"),
        _azure("DeepSeek-V4-Flash", Price(0.19, 0.51, 0.028, 0), "low"),
        _azure("gpt-oss-120b", Price(0.15, 0.6, 0, 0), None, reasoning=False),
        # Other review candidates; gpt-5.6-terra also repairs quotes (REPAIR_MODEL).
        _azure("gpt-5.6-terra", Price(2, 12, 0.2, 2.5)),
        _azure("gpt-6.1-sol", Price(2, 10, 0.1, 2.5)),
        # The endpoint's other deployments.
        _azure("gpt-6-astra", Price(10, 50, 1, 12.5)),
        _azure("gpt-5.6-sol", Price(4, 20, 0.4, 5)),
        _azure("gpt-5.5", Price(5, 30, 0.5, 0)),
        _azure("gpt-5.4", Price(2.5, 15, 0.25, 0)),
        _azure("gpt-5.4-mini", Price(0.75, 4.5, 0.075, 0)),
        _azure("gpt-5.4-nano", Price(0.2, 1.25, 0.02, 0), None, reasoning=False),
        _azure("gpt-5.3-codex", Price(1.75, 14, 0.175, 0)),
        _azure("gpt-5.2-codex", Price(1.75, 14, 0.175, 0)),
        _azure("Kimi-K2.6", Price(0.95, 4, 0.16, 0)),
        # Claude candidates, bulk then review; each effort is Anthropic's default for the model,
        # until tuned. Haiku's price rises for a prompt over 100,000 tokens (reads and writes
        # count). Sonnet and Opus ask for the server-side fallback; Haiku has none.
        _claude(
            "claude-haiku-5-5",
            Price(0.1, 0.5, 0.01, 0.125),
            "medium",
            long_prompt=(100_000, Price(0.5, 2.5, 0.05, 0.625)),
        ),
        _claude("claude-sonnet-5-5", Price(2, 10, 0.1, 2.5), "high", fallbacks=True),
        _claude("claude-opus-5-5", Price(4, 20, 0.2, 5), "medium", fallbacks=True),
        # The models Anthropic falls back to from those, so that their attempts have a price.
        _claude("claude-sonnet-5", Price(2, 10, 0.2, 2.5), "high"),
        _claude("claude-opus-5", Price(5, 25, 0.5, 6.25), "high"),
        _claude("claude-opus-4-8", Price(5, 25, 0.5, 6.25), "high"),
    )
}

# The study design each PubMed publication type names; a type not listed names none, so the
# extractor's answer stands (DESIGN.md, "Study design and publication data").
PUBMED_DESIGNS = {
    "Meta-Analysis": "meta_analysis",
    "Network Meta-Analysis": "meta_analysis",
    "Systematic Review": "systematic_review",
    "Randomized Controlled Trial": "rct",
    "Equivalence Trial": "rct",
    "Pragmatic Clinical Trial": "rct",
    "Observational Study": "cohort",
    "Clinical Trial": "cohort",
    "Clinical Trial, Phase I": "cohort",
    "Clinical Trial, Phase II": "cohort",
    "Clinical Trial, Phase III": "cohort",
    "Clinical Trial, Phase IV": "cohort",
    "Controlled Clinical Trial": "cohort",
    "Case Reports": "case_report",
    "Review": "narrative_review",
    "Editorial": "narrative_review",
    "Comment": "narrative_review",
    "Letter": "narrative_review",
}

# Step 3: the species a claim may name (the reply schema's enum, in this order) and the NCBITaxon
# term each stands for. `other` has no term: its claim names no species, and its notes name the
# organism. "macaque" is the genus, since abstracts often don't say which; "yeast" is baker's
# yeast.
HUMAN = "NCBITaxon:9606"
SPECIES_TAXA: dict[str, str | None] = {
    "human": HUMAN,
    "mouse": "NCBITaxon:10090",
    "rat": "NCBITaxon:10116",
    "zebrafish": "NCBITaxon:7955",
    "fly": "NCBITaxon:7227",
    "worm": "NCBITaxon:6239",
    "yeast": "NCBITaxon:4932",
    "dog": "NCBITaxon:9615",
    "pig": "NCBITaxon:9823",
    "macaque": "NCBITaxon:9539",
    "rabbit": "NCBITaxon:9986",
    "other": None,
}

# Step 6, rule 7: whether proposed edges keep the species their claims name, such as "E in
# human" (DESIGN.md, "Refinement (`refine.py`)", "Species in proposals").
# `apply --species-in-proposals` turns it on.
SPECIES_IN_PROPOSALS = False

# Steps 5 and 6, rule 11: for each predicate, the narrower predicates that only support its
# edges. A statement with one of them, or one below it, adds its PMID to such an edge but is
# never proposed under it. REVIEW_SUPPORT_ONLY is whether review judges such claims
# (`--review-support-only` on review and apply). See DESIGN.md, "Refinement (`refine.py`)",
# "Support only".
SUPPORT_ONLY: dict[str, tuple[str, ...]] = {
    "biolink:treats": ("biolink:ameliorates_condition",),
}
REVIEW_SUPPORT_ONLY = False

SKILL_DIR = Path(__file__).resolve().parents[2]
PROMPTS_DIR = SKILL_DIR / "prompts"
LABEL_PROMPT = PROMPTS_DIR / "label.md"
REVIEW_PROMPT = PROMPTS_DIR / "review.md"
REPAIR_PROMPT = PROMPTS_DIR / "repair.md"


def work_dir(output: Path, work: Path | None = None) -> Path:
    return work if work is not None else output / WORK_SUBDIR
