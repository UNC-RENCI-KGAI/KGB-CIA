"""PubMed for every agent: an E-utilities client, a shared SQLite cache, a pair search, and the
PMIDs an edge cites.

- `eutils`: `EUtils.search` (ESearch) and `EUtils.fetch` (EFetch, 200 PMIDs a request), rate
  limited and retried. Environment: NCBI_API_KEY (10 requests a second instead of 3),
  NCBI_EMAIL (NCBI asks for one), EUTILS_URL (another server).
- `cache`: `PubMedCache`, searches by query, abstracts by PMID (misses too) and NodeNorm entries
  by CURIE, at KGB_PUBMED_CACHE or data/cache/pubmed/pubmed.sqlite (`shared.paths`);
  `fetch_abstracts` and `cached_search` go through it, so only what it lacks goes over the
  network.
- `pairs`: `search_pairs`, one query per node pair from the nodes' names, synonyms, MeSH
  headings and NodeNorm labels, with the abstracts of its hits and of the PMIDs its edges cite.
- `citations`: `cited_pmids(edge)`, the PMIDs an edge cites, from PMID CURIEs and PubMed URLs.

Every agent that points at the same cache file shares what the others fetched, so the cleaning
and augmentation agents can run in either order.
"""

from shared.pubmed.cache import (
    CACHE_ENV,
    DEFAULT_CACHE_PATH,
    PubMedCache,
    cached_search,
    default_cache_path,
    fetch_abstracts,
)
from shared.pubmed.citations import cited_pmids, pmid_of
from shared.pubmed.eutils import (
    EFETCH_BATCH,
    EUtils,
    EUtilsError,
    RateLimiter,
    parse_efetch,
    parse_esearch,
)
from shared.pubmed.pairs import (
    MAX_ABSTRACTS,
    MAX_FAILED_SHARE,
    MAX_SYNONYMS,
    NODENORM_BATCH,
    OFF_PEAK_SEARCHES,
    PairSearchResult,
    build_query,
    clean_terms,
    entity_terms,
    is_peak,
    load_nodenorm,
    nodenorm_entries,
    search_forms,
    search_pairs,
)
from shared.pubmed.records import Abstract, Entity, FetchedPmid, Pair, PubmedRecord, Search

__all__ = [
    "CACHE_ENV",
    "DEFAULT_CACHE_PATH",
    "EFETCH_BATCH",
    "MAX_ABSTRACTS",
    "MAX_FAILED_SHARE",
    "MAX_SYNONYMS",
    "NODENORM_BATCH",
    "OFF_PEAK_SEARCHES",
    "Abstract",
    "EUtils",
    "EUtilsError",
    "Entity",
    "FetchedPmid",
    "Pair",
    "PairSearchResult",
    "PubMedCache",
    "PubmedRecord",
    "RateLimiter",
    "Search",
    "build_query",
    "cached_search",
    "cited_pmids",
    "clean_terms",
    "default_cache_path",
    "entity_terms",
    "fetch_abstracts",
    "is_peak",
    "load_nodenorm",
    "nodenorm_entries",
    "parse_efetch",
    "parse_esearch",
    "pmid_of",
    "search_forms",
    "search_pairs",
]
