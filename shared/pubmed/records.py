"""What the PubMed client, cache and pair search take and give back.

`Search` and `Abstract` are what the cache stores. `Entity` and `Pair` are the pair search's
inputs, and `PubmedRecord` its answer for one pair. They are pydantic models so that an agent can
read and write them as JSON lines, or subclass them with fields of its own. PMIDs are bare digits
('12345') throughout.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field


@dataclass
class Search:
    """An ESearch result: the query, the date it ran ('2026-11-02'), PubMed's total hit count, and
    the PMIDs it returned, most relevant first."""

    query: str
    retrieved: str
    total: int
    pmids: list[str]


@dataclass
class Abstract:
    """A PMID's title and abstract (`""` when PubMed has none) and its PubMed publication types
    (`["Journal Article", "Review"]`). A PMID that EFetch doesn't return is kept with an empty
    title and abstract and no publication types, so it isn't asked for again;
    `publication_types` is None only for an abstract cached before they were recorded, which is
    fetched again.

    `abstract` is one paragraph per line, a structured abstract's as 'LABEL: text', each with its
    whitespace collapsed (see `parse_efetch`). Quotes checked against the text and caches keyed by
    it depend on this format, so it must not change."""

    pmid: str
    title: str
    abstract: str = ""
    year: int | None = None
    retrieved: str = ""
    publication_types: list[str] | None = None

    @property
    def has_text(self) -> bool:
        return bool(self.abstract.strip())


class Entity(BaseModel):
    """A node to search PubMed for. The search uses its name and synonyms, then NodeNorm's labels
    for its CURIE; MeSH headings come from the `MESH:D...` CURIEs among its id, its
    `equivalent_identifiers` and NodeNorm's, by their NodeNorm labels."""

    id: str
    name: str | None = None
    equivalent_identifiers: list[str] = Field(default_factory=list)
    synonyms: list[str] = Field(default_factory=list)


class Pair(BaseModel):
    """Two nodes to search PubMed for together, with the PMIDs their edges already cite."""

    pair_id: str
    nodes: tuple[str, str] = Field(description="The pair's node ids, sorted")
    source_pmids: list[str] = Field(
        default_factory=list,
        description="PMIDs the pair's edges cite, bare digits: fetched with the search's hits, "
        "and kept for a pair that can't be searched",
    )


class FetchedPmid(BaseModel):
    pmid: str
    origin: Literal["search", "source", "source+search"]
    rank: int | None = Field(default=None, description="1-based relevance rank in the search")


class PubmedRecord(BaseModel):
    """What the pair search found for one pair. Titles and abstracts are in the PubMed cache."""

    pair_id: str
    query: str
    retrieved: str = Field(description="ISO date of the search, e.g. '2026-11-02'")
    total: int = Field(description="ESearch's total hit count")
    synonyms: dict[str, list[str]] = Field(
        default_factory=dict, description="Node id -> the [tiab] terms used in the query"
    )
    mesh: dict[str, list[str]] = Field(
        default_factory=dict, description="Node id -> the [mh] headings used in the query"
    )
    pmids: list[FetchedPmid] = Field(
        default_factory=list, description="Fetched PMIDs that have an abstract, search ones first"
    )
    no_abstract: list[str] = Field(
        default_factory=list, description="Fetched PMIDs PubMed has no abstract for"
    )
