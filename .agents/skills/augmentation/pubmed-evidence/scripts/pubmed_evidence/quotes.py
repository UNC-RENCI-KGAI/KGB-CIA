"""Checking that an LLM's quote really is in the abstract.

`quote_found` checks a claim's evidence sentence, or a repair of one, against its PMID's title
and abstract, which catches made-up evidence cheaply. reference/formats.md, "Quotes", says what
the comparison ignores.
"""

from __future__ import annotations

import re
import unicodedata

# Each part of a quote must be at least this long once normalized (about four words), and a
# quote has at most this many parts.
MIN_PART_CHARS = 20
MAX_PARTS = 3
# "[...]" first: it contains "...".
_ELLIPSIS = re.compile(r"\s*(?:\[\.\.\.\]|\.\.\.|\u2026)\s*")

_DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"), "-")
_QUOTES = dict.fromkeys(map(ord, "\u2018\u2019\u201a\u201b\u2032"), "'") | dict.fromkeys(
    map(ord, "\u201c\u201d\u201e\u201f\u2033"), '"'
)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_DASHES).translate(_QUOTES)
    # Brackets change no wording, and models drop them when quoting "deficiency (GAMT-D) is".
    text = re.sub(r"[()\[\]{}]", " ", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text.strip(" .;:,\"'")


def quote_found(quote: str | None, *texts: str) -> bool:
    """True if `quote` appears in any of `texts`.

    An ellipsis splits a quote into at most MAX_PARTS parts that must each appear, in order, in
    the same text, and each part must be at least MIN_PART_CHARS long.
    """
    if not quote:
        return False
    parts = [p for p in (normalize(p) for p in _ELLIPSIS.split(quote)) if p]
    if not parts or len(parts) > MAX_PARTS or any(len(p) < MIN_PART_CHARS for p in parts):
        return False
    for text in texts:
        haystack = normalize(text or "")
        start = 0
        for part in parts:
            i = haystack.find(part, start)
            if i < 0:
                break
            start = i + len(part)
        else:
            return True
    return False
