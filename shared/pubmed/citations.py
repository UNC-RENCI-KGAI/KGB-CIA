"""The PubMed articles an edge cites."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# 'PMID:12345', the prefix in any case, spaces allowed around the parts.
_PMID_CURIE = re.compile(r"\s*PMID\s*:\s*([0-9]+)\s*", re.IGNORECASE)
# PubMed article URLs, old and new: ncbi.nlm.nih.gov/pubmed/12345, pubmed.ncbi.nlm.nih.gov/12345/
PUBMED_URL = re.compile(r"(?:ncbi\.nlm\.nih\.gov/pubmed/|pubmed\.ncbi\.nlm\.nih\.gov/)([0-9]+)")
# A value that is one PubMed article URL and nothing else, as some sources write `publications`.
_PUBMED_URL_ONLY = re.compile(
    r"\s*(?:https?://)?(?:www\.)?" + PUBMED_URL.pattern + r"/?\s*", re.IGNORECASE
)


def pmid_of(value: str) -> str | None:
    """The PMID a citation names: 'PMID:12345' -> '12345' (the prefix in any case, 'pmid: 12345'
    too), and a PubMed article URL -> its id ('https://pubmed.ncbi.nlm.nih.gov/12345/'), as
    DrugCentral and GtoPdb write `publications`. Anything else -> None."""
    match = _PMID_CURIE.fullmatch(value) or _PUBMED_URL_ONLY.fullmatch(value)
    return match.group(1) if match else None


def cited_pmids(edge: Mapping[str, Any]) -> list[str]:
    """The PubMed articles an edge cites, as bare PMIDs, unique and sorted by number.

    Reads every top-level field that is a string or a list of strings: a value that is a PMID
    CURIE ('PMID:12345') gives its number, and any other gives the ids of the PubMed URLs in it
    (`supporting_documents` often holds them). So it returns the source's `publications`, and on
    an edge the augmentation agent wrote, the PMIDs it appended to them (also listed in
    `lit_added_publications`) or, on an edge it proposed, those that make it. Nested records are
    not read: `lit_evidence` lists every abstract read for the edge's node pair, supporting it
    or not."""
    found: set[str] = set()
    for value in edge.values():
        for item in value if isinstance(value, list | tuple) else (value,):
            if not isinstance(item, str):
                continue
            pmid = pmid_of(item)
            found.update([pmid] if pmid else PUBMED_URL.findall(item))
    return sorted(found, key=int)
