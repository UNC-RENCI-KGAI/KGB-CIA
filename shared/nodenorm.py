"""Thin client for the RENCI-hosted NodeNorm API.

Every node in the graph goes through here. Default URL is the public Translator instance; set
NODENORM_URL to point at the RENCI-hosted one.
"""

from __future__ import annotations

import os

import httpx

DEFAULT_URL = os.environ.get(
    "NODENORM_URL", "https://nodenormalization-sri.renci.org/get_normalized_nodes"
)


def normalize(curies: list[str], url: str = DEFAULT_URL, timeout: float = 60.0) -> dict:
    """Return the raw NodeNorm response for a batch of CURIEs."""
    if not curies:
        return {}
    resp = httpx.post(url, json={"curies": curies, "conflate": True}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def preferred_id(curie: str, response: dict) -> str | None:
    """Preferred identifier for `curie` from a `normalize` response, or None if unknown."""
    entry = response.get(curie)
    if not entry:
        return None
    return entry.get("id", {}).get("identifier")
