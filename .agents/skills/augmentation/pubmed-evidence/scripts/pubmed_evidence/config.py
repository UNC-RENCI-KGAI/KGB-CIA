"""Defaults for every step, in one place.

`kgb-augment` must run with no arguments (the pipeline calls `main([])`), so every path, model
and cap has a default here. The CLI overrides any of them; a few also read an environment
variable so a shared setup (a shared cache directory, another model endpoint) needs no flags.
PubMed's settings (the cache file, the NCBI key and email) are `shared.pubmed`'s.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# The augmentation agent's infores ID. Every edge it proposes gets it as
# `primary_knowledge_source`, with PubMed, where its claims come from, as the one
# `supporting_data_source` (a list: the Biolink slot is multivalued); and is a knowledge
# assertion that text mining made.
INFORES = "infores:kgbcia-augmentor"
SUPPORTING_DATA_SOURCE = "infores:pubmed"
NEW_EDGE_KNOWLEDGE_LEVEL = "knowledge_assertion"
NEW_EDGE_AGENT_TYPE = "text_mining_agent"

# The filter stage's output and this stage's output. Paths are relative to where the command
# runs; the pipeline runs from the repository root.
DEFAULT_INPUT = Path(os.environ.get("KGB_AUGMENT_INPUT", "data/filter"))
DEFAULT_OUTPUT = Path(os.environ.get("KGB_AUGMENT_OUTPUT", "data/augmentation"))
# Intermediate files of each step: `<output>/work` unless given.
WORK_SUBDIR = "work"
# The label cache and the UBERON release. Point this at a shared directory to share them across
# a team. The PubMed cache is shared.pubmed's: KGB_PUBMED_CACHE, or data/cache/pubmed.sqlite.
DEFAULT_CACHE_DIR = Path(os.environ.get("KGB_AUGMENT_CACHE_DIR", "data/augmentation/cache"))
LABEL_CACHE = "labels.sqlite"
# UBERON's release, whose is_a/part_of closure decides whether one anatomical context is at or
# below another. The ground step fetches the release UBERON_URL pins into the cache directory
# once; delete the file there to fetch it again after changing the pin.
UBERON_FILE = "uberon-basic.obo"
UBERON_URL = "http://purl.obolibrary.org/obo/uberon/releases/2026-11-05/uberon-basic.obo"

# Step 1: pairs kept per capped rule (gene-gene, gene-phenotype; text-co-occurrence and
# text-mined, for edges mined from the literature), best-scored first.
CAPPED_LIMIT = 10_000

# Step 4: the RENCI Name Resolver, which grounds the anatomy claims name.
NAMERES_URL = os.environ.get("NAMERES_URL", "https://name-resolution-sri.renci.org")

# Step 3: models. The gold set decides these; until then, the cheapest candidates. Each must
# be in MODELS, below.
BULK_MODEL = os.environ.get("KGB_AUGMENT_MODEL", "gpt-6-luna")
REVIEW_MODEL = os.environ.get("KGB_AUGMENT_REVIEW_MODEL", "gpt-5.6-terra")
# The end of step 3 repairs quotes that weren't found with the review model. Its repairs are
# the ones review, evaluate and step 6 read, so changing it starts the repairs afresh.
REPAIR_MODEL = REVIEW_MODEL
# The most abstracts and review items one call sends; a pair with more is split across calls.
ABSTRACTS_PER_CALL = 20
REVIEW_ITEMS_PER_CALL = 40
WORKERS = 8
# The full run (`kgb-augment` with no step, as the pipeline runs it) labels only as many pairs
# as step 3's estimated cost, in US dollars, allows. Pairs already labeled cost nothing.
FULL_RUN_LABEL_BUDGET = 20.0


@dataclass(frozen=True)
class Price:
    """USD per million tokens: uncached input, output, and the prompt cache's reads and
    writes."""

    input: float
    output: float
    cache_read: float
    cache_write: float


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
    effort: str | None = None  # the effort sent by default
    max_output_tokens: int = 16000  # per call, reasoning included
    # (prompt tokens, price): a call with a longer prompt is billed at this price instead.
    long_prompt: tuple[int, Price] | None = None
    fallbacks: bool = False  # the service may answer with another model when this one refuses


# Azure OpenAI deployments, under Pi's standard variable names. The ids and prices below are the
# deployments `.pi/models.json` lists for Pi (the project's own endpoint, the default URL). Pi
# reads that file itself, so a price change there has to be copied here by hand. To use your
# own Azure OpenAI resource, set AZURE_OPENAI_BASE_URL and deploy models under these ids, or
# use a Claude model instead.
AZURE_KEY_VAR = "AZURE_OPENAI_API_KEY"
AZURE_URL_VAR = "AZURE_OPENAI_BASE_URL"
AZURE_DEFAULT_URL = "https://renci-analytics.openai.azure.com/openai/v1"
# `or`: an empty value, as a copied .env.example leaves it, means the default
AZURE_URL = os.environ.get(AZURE_URL_VAR) or AZURE_DEFAULT_URL
_PI = ".pi/models.json, 2026-11-03"


def _azure(id: str, price: Price, effort: str | None = "medium", reasoning: bool = True) -> Model:
    return Model(id, "openai", AZURE_URL, AZURE_KEY_VAR, price, _PI, reasoning, effort)


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
    )


MODELS: dict[str, Model] = {
    m.id: m
    for m in (
        # Bulk labeling candidates: a low effort, since step 3 sends many calls.
        _azure("gpt-6-luna", Price(0.1, 0.5, 0.01, 0.125), "low"),
        _azure("gpt-5.6-luna", Price(0.2, 1.2, 0.02, 0.25), "low"),
        _azure("DeepSeek-V4-Flash", Price(0.19, 0.51, 0.028, 0), "low"),
        _azure("gpt-oss-120b", Price(0.15, 0.6, 0, 0), None, reasoning=False),
        # Review candidates.
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
        # count). Sonnet and Opus fall back server-side when a safety classifier declines;
        # Haiku has no fallback, so its refusals are failed calls.
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

# The study design PubMed's publication types name. Where an abstract's types name one, it
# overrides the extractor's answer; where they name several, the one StudyDesign lists first
# wins. The types not here name none, so the extractor's answer stands: among them the
# veterinary trials and observational studies (animal studies), trial protocols, and scoping
# reviews.
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
# term each stands for: a claim's organism and its species_context_qualifier. `other` has no
# term: its claim names no species, and its notes name the organism. "macaque" is the genus,
# since abstracts often don't say which; "yeast" is baker's yeast.
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

# Step 6 (rule 7 in DESIGN.md): whether proposed edges keep the species their claims name, such
# as "E in human". Off, step 6 takes species_context_qualifier out of every proposal, except one
# that refines an original with that same species, and merges those that become the same
# statement; the originals' support and PMIDs are the same either way.
# `apply --species-in-proposals` turns it on.
SPECIES_IN_PROPOSALS = False

# Steps 5 and 6: narrower predicates that only support an edge of the key's predicate. A claim
# statement whose predicate is one listed here, or below one, adds its PMID to such an edge
# like any narrower statement, but is never proposed as a more specific edge than that edge:
# label may say "ameliorates", but a `treats` edge isn't refined to it. Unless
# REVIEW_SUPPORT_ONLY, step 5 doesn't review a claim for such a statement, and step 6 counts it
# without a verdict; a rejected claim counts for nothing either way. With REVIEW_SUPPORT_ONLY,
# review judges such a claim and only an upheld one counts, as for any other statement.
# `review --review-support-only` and `apply --review-support-only` turn it on for a run.
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
