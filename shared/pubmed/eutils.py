"""NCBI E-utilities client: ESearch for PMIDs, EFetch for their abstracts.

NCBI allows 3 requests a second without an API key and 10 with one (set NCBI_API_KEY), and asks
every client to send an email (NCBI_EMAIL). Requests are spaced to stay under the limit and
retried on transient errors. Set EUTILS_URL to send them somewhere other than NCBI.
"""

from __future__ import annotations

import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from typing import Any

import httpx

from shared.pubmed._util import chunks, warn
from shared.pubmed.records import Abstract

DEFAULT_EUTILS_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
# Environment variables: the base URL, and what NCBI asks a client to send.
EUTILS_URL = "EUTILS_URL"
NCBI_API_KEY = "NCBI_API_KEY"
NCBI_EMAIL = "NCBI_EMAIL"
NCBI_TOOL = "kgb-cia"

ESEARCH_MAX = 10_000  # ESearch returns at most this many ids per query
EFETCH_BATCH = 200  # PMIDs per EFetch request
GIVE_UP_AFTER = 10  # consecutive failed calls (after retries) before NCBI counts as down

_YEAR = re.compile(r"\d{4}")


def eutils_url() -> str:
    """The E-utilities base URL: EUTILS_URL if set, else NCBI's."""
    return os.environ.get(EUTILS_URL) or DEFAULT_EUTILS_URL


class EUtilsError(RuntimeError):
    """A request that failed after retries, or was refused."""


class _Transient(Exception):
    """A response worth retrying: NCBI's rate-limit JSON, a backend error, a cut-off body."""


class RateLimiter:
    """At most `rate` calls a second, spaced evenly and shared by threads; `rate <= 0` disables
    it. Each caller reserves the next slot under the lock and sleeps outside it."""

    def __init__(
        self,
        rate: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.interval = 1.0 / rate if rate > 0 else 0.0
        self.clock = clock
        self.sleep = sleep
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if not self.interval:
            return
        with self._lock:
            now = self.clock()
            delay = self._next - now
            self._next = max(now, self._next) + self.interval
        if delay > 0:
            self.sleep(delay)


# --- Responses ----------------------------------------------------------------------------------


def parse_esearch(data: Any) -> tuple[int, list[str]]:
    """(total hits, PMIDs) from ESearch's JSON. Raises `_Transient` for NCBI's error responses
    (`{"error": "API rate limit exceeded"}`, `{"esearchresult": {"ERROR": ...}}`)."""
    if not isinstance(data, dict) or "error" in data:
        raise _Transient(str(data.get("error") if isinstance(data, dict) else data)[:200])
    result = data.get("esearchresult")
    if not isinstance(result, dict):
        raise _Transient("no esearchresult in response")
    if "ERROR" in result:
        raise _Transient(str(result["ERROR"])[:200])
    return int(result.get("count", 0)), [str(p) for p in result.get("idlist", [])]


def _text(element: ET.Element | None) -> str:
    """All text under `element` (titles and abstracts hold <i>, <sup> and the like), on one
    line."""
    return " ".join("".join(element.itertext()).split()) if element is not None else ""


def _year(pub_date: ET.Element | None) -> int | None:
    if pub_date is None:
        return None
    match = _YEAR.search(pub_date.findtext("Year") or pub_date.findtext("MedlineDate") or "")
    return int(match.group()) if match else None


def _abstract_text(abstract: ET.Element | None) -> str:
    """Paragraphs of the abstract, one per line; a structured abstract's as 'LABEL: text'."""
    if abstract is None:
        return ""
    paragraphs = []
    for part in abstract.findall("AbstractText"):
        text = _text(part)
        if text:
            label = (part.get("Label") or "").strip()
            paragraphs.append(f"{label}: {text}" if label else text)
    return "\n".join(paragraphs)


def _types(elements: list[ET.Element]) -> list[str]:
    """PubMed's publication types, in its order, without repeats."""
    return list(dict.fromkeys(t for t in map(_text, elements) if t))


def parse_efetch(xml_text: str) -> list[Abstract]:
    """Abstracts in an EFetch `PubmedArticleSet` (journal and book articles), with their
    publication types.

    The title and each `AbstractText` paragraph have their whitespace collapsed to single
    spaces; paragraphs are joined by newlines, each a structured abstract's as 'LABEL: text',
    and empty ones are left out. Only the article's own abstract is read, not an
    `OtherAbstract`. An article without abstract text comes back with `abstract=''`. Raises
    ET.ParseError on malformed XML and ValueError on any other document, such as NCBI's
    `<eFetchResult><ERROR>`."""
    root = ET.fromstring(xml_text)
    if root.tag != "PubmedArticleSet":
        raise ValueError(f"unexpected EFetch response <{root.tag}>: {_text(root)[:200]}")
    out = []
    for article in root.findall("PubmedArticle"):
        citation = article.find("MedlineCitation")
        pmid = (citation.findtext("PMID") or "").strip() if citation is not None else ""
        if not pmid:
            continue
        art = citation.find("Article")
        if art is None:
            out.append(Abstract(pmid, "", publication_types=[]))
            continue
        year = _year(art.find("Journal/JournalIssue/PubDate")) or _year(art.find("ArticleDate"))
        title = _text(art.find("ArticleTitle"))
        types = _types(art.findall("PublicationTypeList/PublicationType"))
        out.append(Abstract(pmid, title, _abstract_text(art.find("Abstract")), year, "", types))
    for book in root.findall("PubmedBookArticle/BookDocument"):
        pmid = (book.findtext("PMID") or "").strip()
        if not pmid:
            continue
        title = _text(book.find("ArticleTitle")) or _text(book.find("Book/BookTitle"))
        year = _year(book.find("Book/PubDate"))
        types = _types(book.findall("PublicationType"))
        out.append(Abstract(pmid, title, _abstract_text(book.find("Abstract")), year, "", types))
    return out


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as e:
        raise _Transient(f"response is not JSON: {response.text[:200]!r}") from e


def _efetch_abstracts(response: httpx.Response) -> list[Abstract]:
    text = response.text
    if text.lstrip().startswith("{"):  # NCBI's JSON error, e.g. the rate limit
        raise _Transient(text[:200])
    try:
        return parse_efetch(text)
    except (ET.ParseError, ValueError) as e:
        raise _Transient(str(e)[:200]) from e


# --- Client -------------------------------------------------------------------------------------


class EUtils:
    """NCBI E-utilities client for ESearch and EFetch, by POST (long queries exceed URL limits).

    Sends `tool`, and `email` and `api_key` from NCBI_EMAIL and NCBI_API_KEY when set. Retries
    timeouts, connection errors, 429, 5xx and NCBI's error-in-a-200 responses with exponential
    backoff, honoring Retry-After; other 4xx responses fail at once. After `give_up_after` calls
    in a row fail, it stops sending requests and fails every later call at once, so a run against
    an unreachable NCBI ends instead of retrying each item. `rate` (requests a second) defaults
    to 10 with an API key and 3 without; 0 disables the limiter. `requests` counts the requests
    sent. Tests pass a client with `httpx.MockTransport`, `rate=0` and a no-op `sleep`.
    """

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        email: str | None = None,
        rate: float | None = None,
        tries: int = 4,
        backoff: float = 1.0,
        give_up_after: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 60.0,
    ):
        self.api_key = api_key or os.environ.get(NCBI_API_KEY) or None
        self.email = email or os.environ.get(NCBI_EMAIL) or None
        self.base_url = (base_url or eutils_url()).rstrip("/")
        self._own_client = client is None
        self.client = client if client is not None else httpx.Client(timeout=timeout)
        if rate is None:
            rate = 10.0 if self.api_key else 3.0
        self.limiter = RateLimiter(rate, sleep=sleep)
        self.tries = max(1, tries)
        self.backoff = backoff
        self.give_up_after = GIVE_UP_AFTER if give_up_after is None else give_up_after
        self.sleep = sleep
        self.requests = 0
        self.consecutive_failures = 0
        self._warned_email = False

    def close(self) -> None:
        if self._own_client:
            self.client.close()

    def __enter__(self) -> EUtils:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _post(self, endpoint: str, params: dict[str, str], parse: Callable[[httpx.Response], Any]):
        if self.consecutive_failures >= self.give_up_after:
            raise EUtilsError(f"not sent: the last {self.give_up_after} E-utilities calls failed")
        if not self.email and not self._warned_email:
            self._warned_email = True
            warn(f"{NCBI_EMAIL} is not set; NCBI asks every client to send an email")
        data = {**params, "tool": NCBI_TOOL}
        if self.email:
            data["email"] = self.email
        if self.api_key:
            data["api_key"] = self.api_key
        url = f"{self.base_url}/{endpoint}"
        last = ""
        delay = 0.0
        for attempt in range(self.tries):
            if attempt:
                self.sleep(max(delay, self.backoff * 2 ** (attempt - 1)))
            delay = 0.0
            self.limiter.wait()
            self.requests += 1
            try:
                response = self.client.post(url, data=data)
            except httpx.TransportError as e:  # timeouts, refused and reset connections
                last = f"{type(e).__name__}: {e}"
                continue
            if response.status_code == 429 or response.status_code >= 500:
                last = f"HTTP {response.status_code}"
                retry_after = response.headers.get("Retry-After", "")
                delay = float(retry_after) if retry_after.isdigit() else 0.0
                continue
            if response.status_code >= 400:
                self.consecutive_failures += 1
                raise EUtilsError(f"{endpoint}: HTTP {response.status_code}: {response.text[:200]}")
            try:
                result = parse(response)
            except _Transient as e:
                last = str(e)
                continue
            self.consecutive_failures = 0
            return result
        self.consecutive_failures += 1
        raise EUtilsError(f"{endpoint} failed after {self.tries} tries: {last}")

    def search(self, query: str, retmax: int = 20) -> tuple[int, list[str]]:
        """(total hits, up to `retmax` PMIDs by relevance) for a PubMed query."""
        params = {
            "db": "pubmed",
            "term": query,
            "sort": "relevance",
            "retmax": str(max(0, min(retmax, ESEARCH_MAX))),
            "retmode": "json",
        }
        return self._post("esearch.fcgi", params, lambda r: parse_esearch(_json(r)))

    def fetch(self, pmids: Iterable[str]) -> list[Abstract]:
        """An `Abstract` for every PMID, in their order without repeats, EFETCH_BATCH to a
        request. A PMID PubMed doesn't return comes back with an empty title and abstract and no
        publication types, so a cache stops asking for it. Raises EUtilsError if any request
        fails; `fetch_abstracts` keeps the batches before it."""
        wanted = list(dict.fromkeys(pmids))
        out: list[Abstract] = []
        for batch in chunks(wanted, EFETCH_BATCH):
            params = {
                "db": "pubmed",
                "id": ",".join(batch),
                "rettype": "abstract",
                "retmode": "xml",
            }
            got = {a.pmid: a for a in self._post("efetch.fcgi", params, _efetch_abstracts)}
            out += [got.get(p) or Abstract(p, "", publication_types=[]) for p in batch]
        return out
