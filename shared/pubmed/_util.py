"""Small helpers the PubMed modules share."""

from __future__ import annotations

import sys
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime


def warn(message: str) -> None:
    print(f"pubmed: {message}", file=sys.stderr)


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now(UTC).date().isoformat()


def chunks[T](items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    """`items` in consecutive slices of at most `size` (at least 1) each."""
    size = max(1, size)
    for i in range(0, len(items), size):
        yield items[i : i + size]
