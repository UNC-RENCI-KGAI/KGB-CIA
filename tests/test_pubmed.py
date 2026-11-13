"""shared.pubmed without the network: E-utilities through `httpx.MockTransport`, NodeNorm as a
fake function, caches under `tmp_path`."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import pytest

from shared import nodenorm, pubmed
from shared.pubmed import (
    Abstract,
    Entity,
    Pair,
    PairSearchResult,
    PubMedCache,
    PubmedRecord,
    Search,
    cited_pmids,
)
from shared.pubmed import cache as cache_module
from shared.pubmed import eutils as eutils_module
from shared.pubmed import pairs as pairs_module


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path):
    """No test reaches NCBI, NodeNorm or the repository's cache, even by mistake."""
    monkeypatch.setenv(eutils_module.EUTILS_URL, "http://eutils.invalid/entrez/eutils")
    monkeypatch.setenv(cache_module.CACHE_ENV, str(tmp_path / "default" / "pubmed.sqlite"))
    monkeypatch.delenv(eutils_module.NCBI_API_KEY, raising=False)
    monkeypatch.delenv(eutils_module.NCBI_EMAIL, raising=False)

    def no_nodenorm(curies, *a, **kw):
        raise AssertionError("a test called the real NodeNorm")

    monkeypatch.setattr(nodenorm, "normalize", no_nodenorm)


# --- Fakes --------------------------------------------------------------------------------------

STRUCTURED = """
<PubmedArticle>
  <MedlineCitation Status="MEDLINE" Owner="NLM">
    <PMID Version="1">11287972</PMID>
    <Article PubModel="Print">
      <Journal>
        <JournalIssue CitedMedium="Print">
          <Volume>344</Volume>
          <PubDate><Year>2001</Year><Month>Apr</Month><Day>05</Day></PubDate>
        </JournalIssue>
        <Title>The New England journal of medicine</Title>
      </Journal>
      <ArticleTitle>Efficacy and safety of a specific inhibitor of the <i>BCR-ABL</i>
        tyrosine kinase in chronic myeloid leukemia.</ArticleTitle>
      <Abstract>
        <AbstractText Label="BACKGROUND" NlmCategory="BACKGROUND">BCR-ABL is a constitutively
          activated tyrosine kinase that causes chronic myeloid leukemia.</AbstractText>
        <AbstractText Label="METHODS" NlmCategory="METHODS"> </AbstractText>
        <AbstractText Label="RESULTS" NlmCategory="RESULTS">STI571 induced complete
          hematologic responses in 53 of 54 patients.</AbstractText>
        <CopyrightInformation>Copyright 2001 Massachusetts Medical Society.</CopyrightInformation>
      </Abstract>
      <PublicationTypeList>
        <PublicationType UI="D017426">Clinical Trial, Phase I</PublicationType>
        <PublicationType UI="D016428">Journal Article</PublicationType>
      </PublicationTypeList>
    </Article>
    <OtherAbstract Type="Publisher" Language="fre">
      <AbstractText>Un autre resume.</AbstractText>
    </OtherAbstract>
  </MedlineCitation>
</PubmedArticle>
"""

MEDLINE_DATE = """
<PubmedArticle>
  <MedlineCitation>
    <PMID Version="1">111</PMID>
    <Article>
      <Journal><JournalIssue><PubDate><MedlineDate>1998 Dec-1999 Jan</MedlineDate></PubDate>
      </JournalIssue></Journal>
      <ArticleTitle>Imatinib in CML.</ArticleTitle>
      <Abstract><AbstractText>Imatinib treats chronic myeloid leukemia.</AbstractText></Abstract>
    </Article>
  </MedlineCitation>
</PubmedArticle>
"""

NO_ABSTRACT = """
<PubmedArticle>
  <MedlineCitation>
    <PMID Version="1">333</PMID>
    <Article>
      <Journal><JournalIssue><PubDate><Year>2010</Year></PubDate></JournalIssue></Journal>
      <ArticleTitle>Letter: imatinib and CML.</ArticleTitle>
    </Article>
  </MedlineCitation>
</PubmedArticle>
"""

BOOK = """
<PubmedBookArticle>
  <BookDocument>
    <PMID Version="1">20301295</PMID>
    <Book><BookTitle>GeneReviews</BookTitle><PubDate><Year>1993</Year></PubDate></Book>
    <ArticleTitle>ABL1-related disorder</ArticleTitle>
    <PublicationType UI="D016454">Review</PublicationType>
    <Abstract><AbstractText>ABL1 variants cause a congenital disorder.</AbstractText></Abstract>
  </BookDocument>
</PubmedBookArticle>
"""

HEADER = (
    '<?xml version="1.0" ?>\n<!DOCTYPE PubmedArticleSet PUBLIC "-//NLM//DTD PubMedArticle, '
    '1st January 2025//EN" "https://dtd.nlm.nih.gov/ncbi/pubmed/out/pubmed_250101.dtd">\n'
)


def article(pmid: str, title: str = "", abstract: str | None = None) -> str:
    abstract = f"Abstract of {pmid}." if abstract is None else abstract
    body = f"<Abstract><AbstractText>{abstract}</AbstractText></Abstract>" if abstract else ""
    return (
        f"<PubmedArticle><MedlineCitation><PMID>{pmid}</PMID><Article><Journal><JournalIssue>"
        f"<PubDate><Year>2020</Year></PubDate></JournalIssue></Journal>"
        f"<ArticleTitle>{title or 'Title ' + pmid}</ArticleTitle>{body}</Article>"
        "</MedlineCitation></PubmedArticle>"
    )


def article_set(*articles: str) -> str:
    return HEADER + "<PubmedArticleSet>" + "".join(articles) + "</PubmedArticleSet>"


class FakeNCBI:
    """ESearch and EFetch. `searches` maps a substring of the query to (total, PMIDs); EFetch
    returns `articles[pmid]`, a generated article for any other PMID, and nothing for PMIDs in
    `absent`. `fail` holds responses to send before the real ones."""

    def __init__(self, searches=None, articles=None, absent=(), fail=()):
        self.searches: dict[str, tuple[int, list[str]]] = searches or {}
        self.articles: dict[str, str] = articles or {}
        self.absent = set(absent)
        self.fail = list(fail)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        form = dict(parse_qsl(request.content.decode()))
        endpoint = request.url.path.rsplit("/", 1)[-1]
        self.calls.append((endpoint, form))
        if self.fail:
            return self.fail.pop(0)
        if endpoint == "esearch.fcgi":
            total, pmids = next((v for k, v in self.searches.items() if k in form["term"]), (0, []))
            idlist = pmids[: int(form["retmax"])]
            result = {"count": str(total), "retmax": str(len(idlist)), "idlist": idlist}
            return httpx.Response(200, json={"esearchresult": result})
        if endpoint == "efetch.fcgi":
            ids = [p for p in form["id"].split(",") if p not in self.absent]
            return httpx.Response(
                200, text=article_set(*(self.articles.get(p) or article(p) for p in ids))
            )
        return httpx.Response(404)

    def endpoint_calls(self, endpoint: str) -> list[dict[str, str]]:
        return [form for e, form in self.calls if e == endpoint]


def eutils(fake: FakeNCBI, **kw) -> pubmed.EUtils:
    client = httpx.Client(transport=httpx.MockTransport(fake))
    return pubmed.EUtils(client, email="test@example.org", rate=0, sleep=lambda s: None, **kw)


IMATINIB = "CHEBI:45783"
ABL1 = "NCBIGene:25"
CML = "MONDO:0011996"


def pair_id(a: str, b: str) -> str:
    return "|".join(sorted((a, b)))


NODENORM = {
    IMATINIB: {
        "id": {"identifier": IMATINIB, "label": "Imatinib"},
        "equivalent_identifiers": [
            {"identifier": IMATINIB, "label": "imatinib"},
            {"identifier": "MESH:D000068877", "label": "Imatinib Mesylate"},
            {"identifier": "MESH:C097613", "label": "imatinib supplementary"},
            {"identifier": "DRUGBANK:DB00619", "label": "Imatinib"},
            {"identifier": "CAS:152459-95-5"},
        ],
        "type": ["biolink:SmallMolecule"],
    },
    CML: {
        "id": {"identifier": CML, "label": "chronic myelogenous leukemia, BCR-ABL1 positive"},
        "equivalent_identifiers": [
            {
                "identifier": "MESH:D015464",
                "label": "Leukemia, Myelogenous, Chronic, BCR-ABL Positive",
            },
            {"identifier": "DOID:8552", "label": "chronic myeloid leukemia"},
        ],
    },
    ABL1: {
        "id": {"identifier": ABL1, "label": "ABL1"},
        "equivalent_identifiers": [
            {"identifier": ABL1, "label": "ABL1"},
            {
                "identifier": "UniProtKB:P00519",
                "label": "ABL1_HUMAN Tyrosine-protein kinase ABL1 (sprot)",
            },
            {"identifier": "MESH:D016125"},  # a MeSH id without a label: skipped
        ],
    },
}


class FakeNodeNorm:
    def __init__(self, entries=None, error: Exception | None = None):
        self.entries = NODENORM if entries is None else entries
        self.error = error
        self.calls: list[list[str]] = []

    def __call__(self, curies: list[str]) -> dict:
        self.calls.append(list(curies))
        if self.error:
            raise self.error
        return {c: self.entries.get(c) for c in curies}


ENTITIES = [
    Entity(
        id=IMATINIB,
        name="imatinib",
        equivalent_identifiers=[IMATINIB, "MESH:D000068877", "DRUGBANK:DB00619"],
        synonyms=["Gleevec", "STI571", "STI-571"],
    ),
    Entity(
        id=CML,
        name="chronic myelogenous leukemia, BCR-ABL1 positive",
        equivalent_identifiers=[CML, "MESH:D015464", "DOID:8552"],
        synonyms=["chronic myeloid leukemia", "CML"],
    ),
    Entity(
        id=ABL1,
        name="ABL1",
        equivalent_identifiers=[ABL1, "HGNC:76"],
        synonyms=["ABL1", "ABL proto-oncogene 1, non-receptor tyrosine kinase"],
    ),
]

DRUG_DISEASE = pair_id(IMATINIB, CML)
DRUG_GENE = pair_id(IMATINIB, ABL1)
PAIRS = [
    Pair(pair_id=DRUG_DISEASE, nodes=(IMATINIB, CML), source_pmids=["11287972"]),
    Pair(pair_id=DRUG_GENE, nodes=(IMATINIB, ABL1), source_pmids=["10592173", "999"]),
]


def default_ncbi(**kw) -> FakeNCBI:
    return FakeNCBI(
        searches={
            '"CML"[tiab]': (4, ["111", "11287972", "222", "333"]),
            '"ABL1"[tiab]': (1, ["444"]),
        },
        articles={"11287972": STRUCTURED, "111": MEDLINE_DATE, "333": NO_ABSTRACT},
        absent={"999"},
        **kw,
    )


def search(path: Path, fake: FakeNCBI, pairs: list[Pair] | None = None, **kw) -> PairSearchResult:
    """`search_pairs` over PAIRS (or `pairs`) with the cache at `path`."""
    kw.setdefault("nodenorm", FakeNodeNorm())
    with PubMedCache(path) as cache:
        return pubmed.search_pairs(
            PAIRS if pairs is None else pairs, ENTITIES, cache=cache, client=eutils(fake), **kw
        )


def by_pair(result: PairSearchResult) -> dict[str, PubmedRecord]:
    return {r.pair_id: r for r in result.records}


def lines(result: PairSearchResult) -> list[str]:
    return [r.model_dump_json(exclude_none=True) for r in result.records]


# --- Terms and query ----------------------------------------------------------------------------


def test_clean_terms():
    terms = [
        "imatinib",
        None,
        "Imatinib",  # case-insensitive duplicate
        "ST",  # too short
        "12345",  # no letter
        "152459-95-5",  # no letter
        'STI"571*',  # breaks a quoted phrase
        "x" * 101,  # too long
        "  Gleevec   tablets ",
        "[imatinib]",  # duplicate once cleaned
        "abc",
        "def",
    ]
    assert pubmed.clean_terms(terms) == ["imatinib", "STI 571", "Gleevec tablets", "abc", "def"]
    assert pubmed.clean_terms(terms, limit=2) == ["imatinib", "STI 571"]
    assert pubmed.clean_terms([f"term{i}" for i in range(20)]) == [
        f"term{i}" for i in range(pubmed.MAX_SYNONYMS)
    ]


def test_entity_terms_adds_nodenorm_labels_and_mesh():
    synonyms, mesh = pubmed.entity_terms(ENTITIES[0], NODENORM)
    assert synonyms == [
        "imatinib",
        "Gleevec",
        "STI571",
        "STI-571",
        "Imatinib Mesylate",
        "imatinib supplementary",
    ]
    # The supplementary concept (MESH:C...) is not a heading [mh] can search.
    assert mesh == ["Imatinib Mesylate"]

    synonyms, mesh = pubmed.entity_terms(ENTITIES[2], NODENORM)
    assert "Tyrosine-protein kinase ABL1" in synonyms  # UniProt mnemonic and (sprot) dropped
    assert mesh == []  # MESH:D016125 has no label

    # Without NodeNorm: the node's own names, and no MeSH (its MESH ids have no labels).
    assert pubmed.entity_terms(ENTITIES[0], {}) == (
        ["imatinib", "Gleevec", "STI571", "STI-571"],
        [],
    )
    assert pubmed.entity_terms(Entity(id="CHEMBL.COMPOUND:CHEMBL1"), {}) == ([], [])


def test_build_query_shape():
    query = pubmed.build_query(["imatinib", "Gleevec"], ["Imatinib Mesylate"], ["CML"], [])
    assert query == (
        '("imatinib"[tiab] OR "Gleevec"[tiab] OR "Imatinib Mesylate"[mh]) '
        'AND ("CML"[tiab]) AND hasabstract'
    )
    with pytest.raises(ValueError):
        pubmed.build_query(["imatinib"], [], [], [])


@pytest.mark.parametrize(
    "term, forms",
    [
        ("VWF gene", ["VWF"]),
        ("SLC26A4 protein, human", ["SLC26A4"]),
        ("endoglin (human)", ["endoglin"]),
        ("obsolete DICER1 syndrome", ["DICER1 syndrome"]),
        ("cDNA FLJ58696, highly similar to Endoglin", []),
        ("NEB protein (Fragment)", []),
        ("rdh10.L", ["rdh10", "rdh10.L"]),
        ("pou5f3.3.S", ["pou5f3.3", "pou5f3.3.S"]),
        ("Pendred syndrome", ["Pendred syndrome"]),
        ("HLA-B", ["HLA-B"]),
    ],
)
def test_search_forms(term, forms):
    assert pubmed.search_forms(term) == forms


# --- Responses ----------------------------------------------------------------------------------


def test_parse_esearch():
    data = {"esearchresult": {"count": "7", "retmax": "2", "idlist": ["1", "2"]}}
    assert pubmed.parse_esearch(data) == (7, ["1", "2"])
    for bad in ({"error": "API rate limit exceeded"}, {"esearchresult": {"ERROR": "backend"}}):
        with pytest.raises(eutils_module._Transient):
            pubmed.parse_esearch(bad)


def test_parse_efetch():
    xml = article_set(STRUCTURED, MEDLINE_DATE, NO_ABSTRACT, BOOK)
    got = {a.pmid: a for a in pubmed.parse_efetch(xml)}
    first = got["11287972"]
    assert first.title == (
        "Efficacy and safety of a specific inhibitor of the BCR-ABL tyrosine kinase in chronic "
        "myeloid leukemia."
    )
    # One paragraph per line, labeled, whitespace collapsed; the empty METHODS paragraph and the
    # OtherAbstract are left out. Quotes and caches depend on this exact text.
    assert first.abstract == (
        "BACKGROUND: BCR-ABL is a constitutively activated tyrosine kinase that causes chronic "
        "myeloid leukemia.\nRESULTS: STI571 induced complete hematologic responses in 53 of 54 "
        "patients."
    )
    assert first.year == 2001
    assert first.publication_types == ["Clinical Trial, Phase I", "Journal Article"]
    assert got["111"].year == 1998
    assert got["333"].abstract == "" and not got["333"].has_text
    assert got["333"].title == "Letter: imatinib and CML."
    assert got["333"].publication_types == []  # fetched, and PubMed gave none
    assert got["20301295"].title == "ABL1-related disorder"
    assert got["20301295"].year == 1993 and got["20301295"].has_text
    assert got["20301295"].publication_types == ["Review"]
    with pytest.raises(ValueError):
        pubmed.parse_efetch("<eFetchResult><ERROR>Empty id list</ERROR></eFetchResult>")


# --- E-utilities client -------------------------------------------------------------------------


def test_search_request_parameters(monkeypatch):
    monkeypatch.setenv(eutils_module.NCBI_API_KEY, "secret")
    fake = FakeNCBI(searches={"x": (3, ["1", "2", "3"])})
    client = eutils(fake)
    assert client.search('("x"[tiab]) AND ("y"[tiab]) AND hasabstract', 2) == (3, ["1", "2"])
    form = fake.endpoint_calls("esearch.fcgi")[0]
    assert form["db"] == "pubmed" and form["sort"] == "relevance" and form["retmode"] == "json"
    assert form["retmax"] == "2" and form["tool"] == eutils_module.NCBI_TOOL
    assert form["email"] == "test@example.org" and form["api_key"] == "secret"
    with httpx.Client() as http:
        assert pubmed.EUtils(http).limiter.interval == pytest.approx(0.1)  # 10/s with a key
        monkeypatch.delenv(eutils_module.NCBI_API_KEY)
        assert pubmed.EUtils(http).limiter.interval == pytest.approx(1 / 3)


def test_base_url_from_the_environment(monkeypatch):
    fake = FakeNCBI(searches={"x": (1, ["1"])})
    monkeypatch.setenv(eutils_module.EUTILS_URL, "http://mirror.test/eutils/")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return fake(request)

    client = pubmed.EUtils(httpx.Client(transport=httpx.MockTransport(handler)), rate=0)
    client.search("x")
    assert seen == ["http://mirror.test/eutils/esearch.fcgi"]
    monkeypatch.delenv(eutils_module.EUTILS_URL)
    assert eutils_module.eutils_url() == eutils_module.DEFAULT_EUTILS_URL


def test_retry_on_429_and_rate_limit_json_then_success():
    sleeps: list[float] = []
    fake = FakeNCBI(
        searches={"x": (1, ["1"])},
        fail=[
            httpx.Response(429, json={"error": "API rate limit exceeded"}),
            httpx.Response(200, json={"error": "API rate limit exceeded"}),
            httpx.Response(503),
        ],
    )
    client = pubmed.EUtils(
        httpx.Client(transport=httpx.MockTransport(fake)), rate=0, sleep=sleeps.append
    )
    assert client.search('("x"[tiab]) AND ("y"[tiab])') == (1, ["1"])
    assert client.requests == 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_retry_gives_up_and_stops_after_consecutive_failures():
    fake = FakeNCBI(fail=[httpx.Response(500)] * 100)
    client = eutils(fake, tries=3, give_up_after=2)
    for _ in range(2):
        with pytest.raises(pubmed.EUtilsError, match="after 3 tries"):
            client.search("q")
    with pytest.raises(pubmed.EUtilsError, match="not sent"):
        client.search("q")
    assert client.requests == 6


def test_client_error_is_not_retried():
    fake = FakeNCBI(fail=[httpx.Response(400, text="bad request")])
    client = eutils(fake)
    with pytest.raises(pubmed.EUtilsError, match="HTTP 400"):
        client.fetch(["1"])
    assert client.requests == 1


def test_fetch_marks_absent_pmids_and_sends_200_a_request():
    fake = FakeNCBI(absent={"7"})
    got = eutils(fake).fetch(["5", "7", "5", "9"])
    [form] = fake.endpoint_calls("efetch.fcgi")
    assert form["id"] == "5,7,9"
    assert form["rettype"] == "abstract" and form["retmode"] == "xml"
    assert [a.pmid for a in got] == ["5", "7", "9"]
    assert {a.pmid for a in got if not a.has_text} == {"7"}
    assert got[1].publication_types == []  # absent: not asked for again
    assert eutils(fake).fetch([]) == [] and len(fake.calls) == 1

    fake = FakeNCBI(absent={"7", "300"})
    pmids = [str(i) for i in range(1, 451)]
    got = eutils(fake).fetch(pmids)
    batches = [form["id"].split(",") for form in fake.endpoint_calls("efetch.fcgi")]
    assert [len(b) for b in batches] == [200, 200, 50]
    assert sum(batches, []) == pmids == [a.pmid for a in got]


def test_rate_limiter_spaces_calls():
    now = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    limiter = pubmed.RateLimiter(4, clock=lambda: now[0], sleep=sleep)
    for _ in range(3):
        limiter.wait()
    assert sleeps == [0.25, 0.25]
    now[0] += 10  # idle: no wait
    limiter.wait()
    assert sleeps == [0.25, 0.25]
    pubmed.RateLimiter(0, sleep=sleep).wait()
    assert len(sleeps) == 2


def test_is_peak():
    try:
        ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError:
        pytest.skip("no time zone database")
    # Mon 2026-11-02 15:00 UTC is 10am ET; 03:00 UTC is 10pm ET Sunday; Saturday noon ET.
    assert pubmed.is_peak(datetime(2026, 11, 2, 15, tzinfo=UTC))
    assert not pubmed.is_peak(datetime(2026, 11, 3, 3, tzinfo=UTC))
    assert not pubmed.is_peak(datetime(2026, 11, 7, 17, tzinfo=UTC))


# --- Cache --------------------------------------------------------------------------------------

# The abstracts table as the augmentation agent's first release created it.
OLD_ABSTRACTS = """
CREATE TABLE abstracts (
    pmid TEXT PRIMARY KEY, title TEXT NOT NULL, abstract TEXT NOT NULL,
    year INTEGER, retrieved TEXT NOT NULL);
INSERT INTO abstracts VALUES ('1', 'Old title', 'Old text.', 2001, '2026-11-01T00:00:00+00:00');
"""

# The augmentation agent's pubmed.sqlite as it is now, with its groundings table, a row in each.
AUGMENTATION_CACHE = """
CREATE TABLE searches (
    query TEXT PRIMARY KEY, retrieved TEXT NOT NULL, total INTEGER NOT NULL,
    pmids TEXT NOT NULL);
CREATE TABLE abstracts (
    pmid TEXT PRIMARY KEY, title TEXT NOT NULL, abstract TEXT NOT NULL,
    year INTEGER, retrieved TEXT NOT NULL, publication_types TEXT);
CREATE TABLE nodenorm (
    curie TEXT PRIMARY KEY, entry TEXT, retrieved TEXT NOT NULL);
CREATE TABLE groundings (
    text TEXT NOT NULL, kind TEXT NOT NULL, curie TEXT, label TEXT, source TEXT,
    created TEXT NOT NULL,
    PRIMARY KEY (text, kind));
INSERT INTO searches VALUES ('("a"[tiab]) AND ("b"[tiab]) AND hasabstract', '2026-11-02', 3,
    '["1", "2"]');
INSERT INTO abstracts VALUES ('1', 'Title', 'BACKGROUND: Text.
RESULTS: More.', 2001, 't', '["Review"]');
INSERT INTO abstracts VALUES ('2', '', '', NULL, 't', '[]');
INSERT INTO nodenorm VALUES ('UBERON:0002107', '{"id": {"identifier": "UBERON:0002107"}}', 't');
INSERT INTO groundings VALUES ('liver', 'anatomy', 'UBERON:0002107', 'liver', 'nameres', 't');
"""


def snapshot(path: Path) -> dict[str, tuple[str, list[tuple]]]:
    """Every table's definition and rows, in the order they were written."""
    db = sqlite3.connect(path)
    try:
        tables = db.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'").fetchall()
        return {
            name: (sql, db.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall())
            for name, sql in tables
        }
    finally:
        db.close()


def test_old_cache_gains_publication_types(tmp_path: Path):
    path = tmp_path / "pubmed.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(OLD_ABSTRACTS)
    with PubMedCache(path) as cache:
        [old] = cache.get_abstracts(["1"]).values()
        assert (old.title, old.year, old.publication_types) == ("Old title", 2001, None)
        assert cache.missing_abstracts(["1", "2"]) == ["1", "2"]
        cache.put_abstracts(
            [
                Abstract("1", "Old title", "Old text.", 2001, publication_types=["Review"]),
                Abstract("2", "", publication_types=[]),
            ]
        )
    with PubMedCache(path) as cache:  # opening it again adds nothing twice
        got = cache.get_abstracts(["1", "2"])
        assert got["1"].publication_types == ["Review"] and got["1"].abstract == "Old text."
        assert got["2"].publication_types == [] and not got["2"].has_text
        assert cache.missing_abstracts(["1", "2"]) == []
        assert cache.articles(["1", "3"]) == {"1": (2001, ["Review"])}


def test_augmentation_cache_opens_as_it_is(tmp_path: Path):
    path = tmp_path / "pubmed.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(AUGMENTATION_CACHE)
    before = snapshot(path)
    with PubMedCache(path) as cache:
        assert snapshot(path) == before  # opening it changes nothing
        search = cache.get_search('("a"[tiab]) AND ("b"[tiab]) AND hasabstract')
        assert search == Search(search.query, "2026-11-02", 3, ["1", "2"])
        got = cache.get_abstracts(["1", "2", "3"])
        assert got["1"] == Abstract(
            "1", "Title", "BACKGROUND: Text.\nRESULTS: More.", 2001, "t", ["Review"]
        )
        assert got.keys() == {"1", "2"} and not got["2"].has_text
        assert cache.get_nodenorm(["UBERON:0002107"]) == {
            "UBERON:0002107": {"id": {"identifier": "UBERON:0002107"}}
        }
        cache.put_abstracts([Abstract("3", "New", "Text.", 2020, publication_types=[])])
        cache.put_search(Search("q", "2026-11-10", 0, []))
    after = snapshot(path)
    for table in ("nodenorm", "groundings"):
        assert after[table] == before[table]
    for table in ("searches", "abstracts"):
        assert after[table][0] == before[table][0]  # same definition
        assert after[table][1][: len(before[table][1])] == before[table][1]  # rows kept


def test_cache_path_from_the_environment(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(cache_module.CACHE_ENV, str(tmp_path / "shared" / "x.sqlite"))
    with PubMedCache() as cache:
        assert cache.path == tmp_path / "shared" / "x.sqlite" and cache.path.exists()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv(cache_module.CACHE_ENV, "~/team/pubmed.sqlite")
    assert pubmed.default_cache_path() == tmp_path / "home" / "team" / "pubmed.sqlite"
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(cache_module.CACHE_ENV)
    assert pubmed.default_cache_path() == tmp_path / "home" / ".cache" / "pubmed" / "pubmed.sqlite"


def test_an_old_cache_is_used_until_it_is_moved(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv(cache_module.CACHE_ENV)
    monkeypatch.chdir(tmp_path)
    cache_module._warn_legacy.cache_clear()
    new = tmp_path / "home" / ".cache" / "pubmed" / "pubmed.sqlite"
    old = tmp_path / "data" / "cache" / "pubmed.sqlite"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"")
    assert pubmed.default_cache_path() == Path("data/cache/pubmed.sqlite")
    assert pubmed.default_cache_path() == Path("data/cache/pubmed.sqlite")
    err = capsys.readouterr().err
    assert err.count("old default") == 1 and f"mv {old} {new}" in err  # once a process
    new.parent.mkdir(parents=True)
    new.write_bytes(b"")
    assert pubmed.default_cache_path() == new


# --- Through the cache --------------------------------------------------------------------------


def test_fetch_abstracts_fetches_only_what_the_cache_lacks(tmp_path: Path):
    fake = FakeNCBI(articles={"11287972": STRUCTURED, "333": NO_ABSTRACT}, absent={"999"})
    with PubMedCache(tmp_path / "pubmed.sqlite") as cache:
        cache.put_abstracts([Abstract("111", "Cached", "Cached text.", 1999, publication_types=[])])
        got = pubmed.fetch_abstracts(
            ["999", "11287972", "111", "333", "PMID:5", "11287972"],
            cache=cache,
            client=eutils(fake),
        )
        assert [form["id"] for form in fake.endpoint_calls("efetch.fcgi")] == ["999,11287972,333"]
        assert list(got) == ["999", "11287972", "111", "333"]  # in order; not-digits left out
        assert got["111"].abstract == "Cached text."
        assert got["11287972"].abstract.startswith("BACKGROUND: BCR-ABL")
        # A PMID PubMed doesn't return is cached empty, so it isn't asked for again.
        assert (got["999"].title, got["999"].abstract, got["999"].publication_types) == ("", "", [])
        assert not got["333"].has_text and got["333"].title

        fake.calls.clear()
        assert pubmed.fetch_abstracts(["999", "11287972", "111"], cache=cache, client=eutils(fake))
        assert fake.calls == []

        # Refreshed, everything is fetched again; 111, which PubMed no longer returns, keeps
        # its text.
        fake.absent.add("111")
        got = pubmed.fetch_abstracts(
            ["111", "11287972"], cache=cache, client=eutils(fake), refresh=True
        )
        assert [form["id"] for form in fake.endpoint_calls("efetch.fcgi")] == ["111,11287972"]
        assert got["111"].abstract == "Cached text." and got["111"].publication_types == []


def test_fetch_abstracts_refetches_those_cached_without_types(tmp_path: Path):
    fake = FakeNCBI()
    with PubMedCache(tmp_path / "pubmed.sqlite") as cache:
        cache.put_abstracts(
            [
                Abstract("1", "Old", "Old text."),
                Abstract("2", "T", "Text.", publication_types=["Review"]),
            ]
        )
        got = pubmed.fetch_abstracts(["1", "2"], cache=cache, client=eutils(fake))
        assert [form["id"] for form in fake.endpoint_calls("efetch.fcgi")] == ["1"]
        assert got["1"].abstract == "Abstract of 1." and got["1"].publication_types == []


def test_fetch_abstracts_leaves_out_a_failed_batch(tmp_path: Path, capsys):
    pmids = [str(i) for i in range(1, 251)]
    fake = FakeNCBI(fail=[httpx.Response(400, text="bad")])  # the first batch fails at once
    with PubMedCache(tmp_path / "pubmed.sqlite") as cache:
        got = pubmed.fetch_abstracts(pmids, cache=cache, client=eutils(fake))
        assert list(got) == pmids[200:]
        assert cache.missing_abstracts(pmids) == pmids[:200]  # a rerun fetches them
    assert "EFetch of 200 PMIDs failed" in capsys.readouterr().err


def test_fetch_abstracts_opens_no_client_when_all_are_cached(tmp_path: Path, monkeypatch):
    def no_client(*a, **kw):
        raise AssertionError("opened a client")

    monkeypatch.setattr(cache_module, "EUtils", no_client)
    with PubMedCache() as cache:  # the default path, under tmp_path here
        cache.put_abstracts([Abstract("1", "T", "Text.", publication_types=[])])
    assert pubmed.fetch_abstracts(["1"])["1"].abstract == "Text."
    assert pubmed.fetch_abstracts([]) == {}


def test_cached_search(tmp_path: Path):
    fake = FakeNCBI(searches={"x": (5, ["1", "2", "3", "4", "5"])})
    with PubMedCache(tmp_path / "pubmed.sqlite") as cache:
        got = pubmed.cached_search("x", 2, cache=cache, client=eutils(fake))
        assert (got.total, got.pmids) == (5, ["1", "2"])
        assert got.retrieved == datetime.now(UTC).date().isoformat()
        assert pubmed.cached_search("x", 1, cache=cache, client=eutils(fake)).pmids == ["1"]
        assert len(fake.endpoint_calls("esearch.fcgi")) == 1  # fewer than cached: no request
        # More than cached, and PubMed has more: searched again.
        assert pubmed.cached_search("x", 4, cache=cache, client=eutils(fake)).pmids == [
            "1",
            "2",
            "3",
            "4",
        ]
        assert pubmed.cached_search("x", 9, cache=cache, client=eutils(fake)).pmids == [
            "1",
            "2",
            "3",
            "4",
            "5",
        ]
        assert pubmed.cached_search("x", 20, cache=cache, client=eutils(fake)).total == 5
        assert len(fake.endpoint_calls("esearch.fcgi")) == 3  # all 5 hits cached: none for 20
        pubmed.cached_search("x", 2, cache=cache, client=eutils(fake), refresh=True)
        assert len(fake.endpoint_calls("esearch.fcgi")) == 4


# --- The pair search ----------------------------------------------------------------------------


def test_search_pairs_records(tmp_path: Path):
    fake = default_ncbi()
    result = search(tmp_path / "pubmed.sqlite", fake)
    records = by_pair(result)

    drug_disease = records[DRUG_DISEASE]
    assert drug_disease.query.startswith('("imatinib"[tiab] OR "Gleevec"[tiab]')
    assert '"Imatinib Mesylate"[mh]) AND (' in drug_disease.query
    assert '"Leukemia, Myelogenous, Chronic, BCR-ABL Positive"[mh]) AND hasabstract' in (
        drug_disease.query
    )
    assert drug_disease.total == 4
    assert drug_disease.retrieved == datetime.now(UTC).date().isoformat()
    assert [(p.pmid, p.origin, p.rank) for p in drug_disease.pmids] == [
        ("111", "search", 1),
        ("11287972", "source+search", 2),
        ("222", "search", 3),
    ]
    assert drug_disease.no_abstract == ["333"]
    assert drug_disease.synonyms[CML][:3] == [
        "chronic myelogenous leukemia, BCR-ABL1 positive",
        "chronic myeloid leukemia",
        "CML",
    ]
    assert drug_disease.mesh == {
        IMATINIB: ["Imatinib Mesylate"],
        CML: ["Leukemia, Myelogenous, Chronic, BCR-ABL Positive"],
    }

    # The cited PMID outside the search is fetched and comes after the search results; the one
    # PubMed doesn't return is recorded as having no abstract.
    drug_gene = records[DRUG_GENE]
    assert [(p.pmid, p.origin, p.rank) for p in drug_gene.pmids] == [
        ("444", "search", 1),
        ("10592173", "source", None),
    ]
    assert drug_gene.no_abstract == ["999"]

    # Titles and abstracts are in the cache; the shared PMIDs were fetched in one request.
    with PubMedCache(tmp_path / "pubmed.sqlite") as cache:
        assert cache.get_abstracts(["11287972"])["11287972"].abstract.startswith("BACKGROUND:")
        assert cache.get_abstracts(["999"])["999"].abstract == ""
    assert len(fake.endpoint_calls("esearch.fcgi")) == 2
    assert len(fake.endpoint_calls("efetch.fcgi")) == 1
    summary = result.summary()
    assert summary["searched"] == 2 and summary["cached"] == 0 and summary["failed"] == 0
    assert summary["requests"] == 3 and summary["fetched"] == 7
    assert summary["abstracts"] == 5 and summary["no_abstract"] == 2
    assert summary["nodenorm_known"] == 3 and summary["pairs"] == summary["records"] == 2


def test_second_run_makes_no_requests(tmp_path: Path):
    path = tmp_path / "pubmed.sqlite"
    first = lines(search(path, default_ncbi()))

    fake, nodenorm = default_ncbi(), FakeNodeNorm()
    result = search(path, fake, nodenorm=nodenorm)
    assert fake.calls == [] and nodenorm.calls == []
    assert (result.requests, result.cached, result.searched) == (0, 2, 0)
    assert lines(result) == first

    # refresh reruns the searches but still takes the abstracts from the cache.
    fake = default_ncbi()
    search(path, fake, refresh=True, nodenorm=nodenorm)
    assert len(fake.endpoint_calls("esearch.fcgi")) == 2
    assert fake.endpoint_calls("efetch.fcgi") == []


def test_abstracts_cached_without_types_are_fetched_again(tmp_path: Path):
    path = tmp_path / "pubmed.sqlite"
    first = lines(search(path, default_ncbi()))
    # As a cache from before publication types were recorded has them.
    with PubMedCache(path) as cache:
        cache.db.execute(
            "UPDATE abstracts SET publication_types = NULL WHERE pmid IN ('111', '999')"
        )
        cache.db.commit()

    fake = default_ncbi()
    fake.absent.add("111")  # PubMed no longer returns it: the cached text stays
    assert lines(search(path, fake)) == first
    assert fake.endpoint_calls("esearch.fcgi") == []
    assert [form["id"] for form in fake.endpoint_calls("efetch.fcgi")] == ["111,999"]
    with PubMedCache(path) as cache:
        assert cache.missing_abstracts(["111", "999", "222"]) == []
        kept = cache.get_abstracts(["111"])["111"]
        assert (kept.title, kept.publication_types) == ("Imatinib in CML.", [])

    fake = default_ncbi()
    search(path, fake)
    assert fake.calls == []


def test_more_abstracts_than_cached_reruns_the_search(tmp_path: Path):
    path = tmp_path / "pubmed.sqlite"

    def searches(max_abstracts: int) -> tuple[int, PairSearchResult]:
        fake = default_ncbi()
        result = search(path, fake, max_abstracts=max_abstracts)
        return len(fake.endpoint_calls("esearch.fcgi")), result

    n, result = searches(1)
    assert n == 2
    assert [p.pmid for p in by_pair(result)[DRUG_DISEASE].pmids] == ["111", "11287972"]
    # The drug-disease search had more hits than it kept; the drug-gene search had them all.
    n, result = searches(3)
    assert n == 1
    assert [p.rank for p in by_pair(result)[DRUG_DISEASE].pmids] == [1, 2, 3]
    n, result = searches(2)
    assert n == 0  # fewer than cached: served from the cache
    assert [p.pmid for p in by_pair(result)[DRUG_DISEASE].pmids] == ["111", "11287972"]
    assert searches(20)[0] == 1
    assert searches(20)[0] == 0  # all 4 hits are cached: nothing more to find


def test_cited_pmids_are_fetched_200_a_request(tmp_path: Path):
    pmids = [str(i) for i in range(1, 451)]
    pair = Pair(pair_id=DRUG_GENE, nodes=(IMATINIB, ABL1), source_pmids=pmids)
    fake = FakeNCBI(absent={"7", "300"})
    result = search(tmp_path / "pubmed.sqlite", fake, [pair])
    batches = [form["id"].split(",") for form in fake.endpoint_calls("efetch.fcgi")]
    assert [len(b) for b in batches] == [200, 200, 50]
    assert sum(batches, []) == pmids
    assert by_pair(result)[DRUG_GENE].no_abstract == ["7", "300"]


def test_nodenorm_failure_falls_back_to_own_names(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.setattr(pairs_module, "NODENORM_BATCH", 1)
    nodenorm = FakeNodeNorm(error=httpx.ConnectError("no route to host"))
    result = search(tmp_path / "pubmed.sqlite", default_ncbi(), nodenorm=nodenorm)
    assert "NodeNorm failed" in capsys.readouterr().err
    assert len(nodenorm.calls) == 1  # warned once, not retried per batch
    record = by_pair(result)[DRUG_DISEASE]
    assert record.query == (
        '("imatinib"[tiab] OR "Gleevec"[tiab] OR "STI571"[tiab] OR "STI-571"[tiab]) AND '
        '("chronic myelogenous leukemia, BCR-ABL1 positive"[tiab] OR '
        '"chronic myeloid leukemia"[tiab] OR "CML"[tiab]) AND hasabstract'
    )
    assert result.failed == 0 and result.nodenorm_known == 0
    # Nothing was cached for NodeNorm, so the next run asks again.
    with PubMedCache(tmp_path / "pubmed.sqlite") as cache:
        assert cache.get_nodenorm([IMATINIB]) == {}
        # The raising form, for callers that would rather stop.
        with pytest.raises(httpx.ConnectError):
            pubmed.nodenorm_entries([IMATINIB], cache, nodenorm)


def test_nodenorm_is_batched_cached_and_optional(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(pairs_module, "NODENORM_BATCH", 2)
    nodenorm = FakeNodeNorm(entries={IMATINIB: NODENORM[IMATINIB]})  # CML, ABL1 unknown
    search(tmp_path / "pubmed.sqlite", default_ncbi(), nodenorm=nodenorm)
    assert [len(c) for c in nodenorm.calls] == [2, 1]
    with PubMedCache(tmp_path / "pubmed.sqlite") as cache:
        assert cache.get_nodenorm([CML, IMATINIB]) == {CML: None, IMATINIB: NODENORM[IMATINIB]}

    nodenorm = FakeNodeNorm()
    result = search(
        tmp_path / "other.sqlite", default_ncbi(), nodenorm=nodenorm, use_nodenorm=False
    )
    assert nodenorm.calls == []
    assert by_pair(result)[DRUG_DISEASE].mesh == {IMATINIB: [], CML: []}


def test_pair_without_usable_name(tmp_path: Path, capsys):
    unnamed = "CHEMBL.COMPOUND:CHEMBL999"
    pairs = [
        Pair(
            pair_id=pair_id(unnamed, ABL1),
            nodes=tuple(sorted((unnamed, ABL1))),
            source_pmids=["555"],
        ),
        Pair(pair_id=pair_id("XX:1", ABL1), nodes=tuple(sorted(("XX:1", ABL1)))),
    ]
    fake = default_ncbi()
    result = search(tmp_path / "pubmed.sqlite", fake, pairs)
    assert (result.skipped, result.skipped_kept) == (2, 1)
    assert fake.endpoint_calls("esearch.fcgi") == []
    # The pair with a cited PMID keeps it, unsearched.
    record = by_pair(result)[pairs[0].pair_id]
    assert record.query == "" and record.total == 0
    assert [(p.pmid, p.origin, p.rank) for p in record.pmids] == [("555", "source", None)]
    assert pairs[1].pair_id not in by_pair(result)
    err = capsys.readouterr().err
    assert f"pair {pairs[0].pair_id} not searched: no usable name for {unnamed}" in err


def test_failed_pair_is_left_out_and_reported(tmp_path: Path, capsys, monkeypatch):
    path = tmp_path / "pubmed.sqlite"
    # The first search fails all its tries; the second pair goes through.
    result = search(path, default_ncbi(fail=[httpx.Response(502)] * 4))
    assert result.failed == 1 and result.failed_pairs == [DRUG_DISEASE]
    assert set(by_pair(result)) == {DRUG_GENE}
    assert f"pair {DRUG_DISEASE} failed" in capsys.readouterr().err
    assert result.too_many_failed  # 1 of 2
    monkeypatch.setattr(pairs_module, "MAX_FAILED_SHARE", 0.5)
    assert not result.too_many_failed

    # The rerun retries the failed pair and serves the other from the cache.
    fake = default_ncbi()
    result = search(path, fake)
    assert set(by_pair(result)) == {DRUG_DISEASE, DRUG_GENE}
    assert (result.searched, result.cached) == (1, 1)


# --- Citations ----------------------------------------------------------------------------------


def test_cited_pmids_from_curies_and_urls():
    edge = {
        "publications": ["PMID:5", "DOI:x", "pmid: 10", "PMID:12a", "PMC:9"],
        "supporting_documents": "https://pubmed.ncbi.nlm.nih.gov/7/",
        "xref": ["http://www.ncbi.nlm.nih.gov/pubmed/111", "PMID:5"],
        "p_value": 0.01,
        "negated": False,
        "sources": [{"publications": ["PMID:999"]}],  # nested records are not read
    }
    assert cited_pmids(edge) == ["5", "7", "10", "111"]
    assert cited_pmids({"subject": "X:1", "publications": []}) == []
    assert pubmed.pmid_of("PMID:12345") == "12345" and pubmed.pmid_of("PMID 12345") is None
    # A value that is a PubMed article URL names its PMID; one with other text around it doesn't.
    assert pubmed.pmid_of("https://pubmed.ncbi.nlm.nih.gov/19522465") == "19522465"
    assert pubmed.pmid_of("http://www.ncbi.nlm.nih.gov/pubmed/12345/") == "12345"
    assert pubmed.pmid_of("HTTPS://PubMed.ncbi.nlm.nih.gov/12345/") == "12345"
    assert pubmed.pmid_of("see https://pubmed.ncbi.nlm.nih.gov/12345") is None
    assert pubmed.pmid_of("https://example.org/pubmed.ncbi.nlm.nih.gov/12345") is None


def test_cited_pmids_of_edges_the_augmentation_agent_wrote():
    # A checked input edge: its source cited 100 (and 200 by URL); the agent found 7 supporting
    # it and appended it. 300 was read for the pair, and contradicts the edge.
    evidence = [
        {"publication": "PMID:7", "claims": [{"polarity": "positive", "supports": True}]},
        {"publication": "PMID:300", "claims": [{"polarity": "negative", "supports": False}]},
    ]
    checked = {
        "id": "kgbcia-edge:abc",
        "publications": ["PMID:100", "PMID:7"],
        "supporting_documents": "https://pubmed.ncbi.nlm.nih.gov/200/",
        "augmentation": "checked",
        "original_edge_ids": ["kgbcia-edge:abc"],
        "lit_support_score": 1,
        "lit_supporting_publications": ["PMID:7"],
        "lit_added_publications": ["PMID:7"],
        "lit_evidence": evidence,
    }
    assert cited_pmids(checked) == ["7", "100", "200"]
    proposed = {
        "id": "kgbcia-augmentation:def",
        "publications": ["PMID:7"],
        "original_edge_ids": ["kgbcia-edge:abc"],
        "augmentation": "proposed",
        "lit_support_score": 1,
        "lit_supporting_publications": ["PMID:7"],
        "lit_evidence": evidence,
    }
    assert cited_pmids(proposed) == ["7"]
