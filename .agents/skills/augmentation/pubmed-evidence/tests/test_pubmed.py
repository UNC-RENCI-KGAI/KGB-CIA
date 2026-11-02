"""Step 3 (pubmed) without the network: E-utilities through `httpx.MockTransport`, NodeNorm as a
fake function, and the work files written in code."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import pytest
from pubmed_evidence import config, pubmed
from pubmed_evidence.cache import PubMedCache
from pubmed_evidence.graphio import pair_id
from pubmed_evidence.records import (
    ENTITIES,
    PAIRS,
    PUBMED,
    Entity,
    Pair,
    PubmedRecord,
    read_records,
    write_records,
)
from toy import ABL1, CML, IMATINIB

# --- Fakes ------------------------------------------------------------------------------------

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
        <AbstractText Label="RESULTS" NlmCategory="RESULTS">STI571 induced complete
          hematologic responses in 53 of 54 patients.</AbstractText>
        <CopyrightInformation>Copyright 2001 Massachusetts Medical Society.</CopyrightInformation>
      </Abstract>
    </Article>
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


ENTITY_RECORDS = [
    Entity(
        id=IMATINIB,
        name="imatinib",
        category=["biolink:SmallMolecule"],
        equivalent_identifiers=[IMATINIB, "MESH:D000068877", "DRUGBANK:DB00619"],
        synonyms=["Gleevec", "STI571", "STI-571"],
    ),
    Entity(
        id=CML,
        name="chronic myelogenous leukemia, BCR-ABL1 positive",
        category=["biolink:Disease"],
        equivalent_identifiers=[CML, "MESH:D015464", "DOID:8552"],
        synonyms=["chronic myeloid leukemia", "CML"],
    ),
    Entity(
        id=ABL1,
        name="ABL1",
        category=["biolink:Gene"],
        equivalent_identifiers=[ABL1, "HGNC:76"],
        synonyms=["ABL1", "ABL proto-oncogene 1, non-receptor tyrosine kinase"],
    ),
]

DRUG_DISEASE = pair_id(IMATINIB, CML)
DRUG_GENE = pair_id(IMATINIB, ABL1)


def write_work(work: Path, pairs: list[Pair] | None = None, entities=None) -> Path:
    if pairs is None:
        pairs = [
            Pair(
                pair_id=DRUG_DISEASE,
                nodes=(IMATINIB, CML),
                statement_ids=["s1"],
                source_pmids=["11287972"],
                rules=["chemical-treats"],
            ),
            Pair(
                pair_id=DRUG_GENE,
                nodes=(IMATINIB, ABL1),
                statement_ids=["s2"],
                source_pmids=["10592173", "999"],
                rules=["chemical-gene"],
            ),
        ]
    write_records(work / PAIRS, pairs)
    write_records(work / ENTITIES, ENTITY_RECORDS if entities is None else entities)
    return work


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


def by_pair(work: Path) -> dict[str, PubmedRecord]:
    return {r.pair_id: r for r in read_records(work / PUBMED, PubmedRecord)}


# --- Terms and query --------------------------------------------------------------------------


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
        f"term{i}" for i in range(config.MAX_SYNONYMS)
    ]


def test_entity_terms_adds_nodenorm_labels_and_mesh():
    synonyms, mesh = pubmed.entity_terms(ENTITY_RECORDS[0], NODENORM)
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

    synonyms, mesh = pubmed.entity_terms(ENTITY_RECORDS[2], NODENORM)
    assert "Tyrosine-protein kinase ABL1" in synonyms  # UniProt mnemonic and (sprot) dropped
    assert mesh == []  # MESH:D016125 has no label

    # Without NodeNorm: the node's own names, and no MeSH (its MESH ids have no labels).
    assert pubmed.entity_terms(ENTITY_RECORDS[0], {}) == (
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


def test_parse_esearch():
    data = {"esearchresult": {"count": "7", "retmax": "2", "idlist": ["1", "2"]}}
    assert pubmed.parse_esearch(data) == (7, ["1", "2"])
    for bad in ({"error": "API rate limit exceeded"}, {"esearchresult": {"ERROR": "backend"}}):
        with pytest.raises(pubmed._Transient):
            pubmed.parse_esearch(bad)


def test_parse_efetch():
    xml = article_set(STRUCTURED, MEDLINE_DATE, NO_ABSTRACT, BOOK)
    got = {a.pmid: a for a in pubmed.parse_efetch(xml)}
    first = got["11287972"]
    assert first.title == (
        "Efficacy and safety of a specific inhibitor of the BCR-ABL tyrosine kinase in chronic "
        "myeloid leukemia."
    )
    assert first.abstract == (
        "BACKGROUND: BCR-ABL is a constitutively activated tyrosine kinase that causes chronic "
        "myeloid leukemia.\nRESULTS: STI571 induced complete hematologic responses in 53 of 54 "
        "patients."
    )
    assert first.year == 2001
    assert got["111"].year == 1998
    assert got["333"].abstract == "" and not got["333"].has_text
    assert got["333"].title == "Letter: imatinib and CML."
    assert got["20301295"].title == "ABL1-related disorder"
    assert got["20301295"].year == 1993 and got["20301295"].has_text
    with pytest.raises(ValueError):
        pubmed.parse_efetch("<eFetchResult><ERROR>Empty id list</ERROR></eFetchResult>")


# --- E-utilities client -----------------------------------------------------------------------


def test_esearch_request_parameters(monkeypatch):
    monkeypatch.setenv(config.NCBI_API_KEY, "secret")
    fake = FakeNCBI(searches={"x": (3, ["1", "2", "3"])})
    client = eutils(fake)
    assert client.esearch('("x"[tiab]) AND ("y"[tiab]) AND hasabstract', 2) == (3, ["1", "2"])
    form = fake.endpoint_calls("esearch.fcgi")[0]
    assert form["db"] == "pubmed" and form["sort"] == "relevance" and form["retmode"] == "json"
    assert form["retmax"] == "2" and form["tool"] == config.NCBI_TOOL
    assert form["email"] == "test@example.org" and form["api_key"] == "secret"
    with httpx.Client() as http:
        assert pubmed.EUtils(http).limiter.interval == pytest.approx(0.1)  # 10/s with a key


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
    assert client.esearch('("x"[tiab]) AND ("y"[tiab])') == (1, ["1"])
    assert client.requests == 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_retry_gives_up_and_stops_after_consecutive_failures(monkeypatch):
    monkeypatch.setattr(pubmed, "GIVE_UP_AFTER", 2)
    fake = FakeNCBI(fail=[httpx.Response(500)] * 100)
    client = eutils(fake, tries=3)
    for _ in range(2):
        with pytest.raises(pubmed.EUtilsError, match="after 3 tries"):
            client.esearch("q")
    with pytest.raises(pubmed.EUtilsError, match="not sent"):
        client.esearch("q")
    assert client.requests == 6


def test_client_error_is_not_retried():
    fake = FakeNCBI(fail=[httpx.Response(400, text="bad request")])
    client = eutils(fake)
    with pytest.raises(pubmed.EUtilsError, match="HTTP 400"):
        client.efetch(["1"])
    assert client.requests == 1


def test_efetch_batches_of_200_and_marks_absent_pmids():
    pmids = [str(i) for i in range(1, 451)]
    fake = FakeNCBI(absent={"7", "300"})
    got = eutils(fake).efetch(pmids)
    batches = [form["id"].split(",") for form in fake.endpoint_calls("efetch.fcgi")]
    assert [len(b) for b in batches] == [200, 200, 50]
    assert sum(batches, []) == pmids
    form = fake.endpoint_calls("efetch.fcgi")[0]
    assert form["rettype"] == "abstract" and form["retmode"] == "xml"
    assert [a.pmid for a in got] == pmids
    assert {a.pmid for a in got if not a.has_text} == {"7", "300"}


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


# --- The step ---------------------------------------------------------------------------------


def test_search_pairs_records(tmp_path):
    work = write_work(tmp_path / "work")
    fake = default_ncbi()
    summary = pubmed.search_pairs(
        work, tmp_path / "cache", eutils=eutils(fake), nodenorm=FakeNodeNorm()
    )
    records = by_pair(work)

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

    # The source PMID outside the search is fetched and comes after the search results; the
    # one PubMed doesn't return is recorded as having no abstract.
    drug_gene = records[DRUG_GENE]
    assert [(p.pmid, p.origin, p.rank) for p in drug_gene.pmids] == [
        ("444", "search", 1),
        ("10592173", "source", None),
    ]
    assert drug_gene.no_abstract == ["999"]

    # Titles and abstracts are in the cache; the shared PMIDs were fetched in one request.
    with PubMedCache(tmp_path / "cache" / config.PUBMED_CACHE) as cache:
        assert cache.get_abstracts(["11287972"])["11287972"].abstract.startswith("BACKGROUND:")
        assert cache.get_abstracts(["999"])["999"].abstract == ""
    assert len(fake.endpoint_calls("esearch.fcgi")) == 2
    assert len(fake.endpoint_calls("efetch.fcgi")) == 1
    assert summary["searched"] == 2 and summary["cached"] == 0 and summary["failed"] == 0
    assert summary["requests"] == 3 and summary["fetched"] == 7
    assert summary["abstracts"] == 5 and summary["no_abstract"] == 2
    assert summary["nodenorm_known"] == 3


def test_second_run_makes_no_requests(tmp_path):
    work = write_work(tmp_path / "work")
    pubmed.search_pairs(
        work, tmp_path / "cache", eutils=eutils(default_ncbi()), nodenorm=FakeNodeNorm()
    )
    first = (work / PUBMED).read_text()

    fake, nodenorm = default_ncbi(), FakeNodeNorm()
    summary = pubmed.search_pairs(work, tmp_path / "cache", eutils=eutils(fake), nodenorm=nodenorm)
    assert fake.calls == [] and nodenorm.calls == []
    assert summary["requests"] == 0 and summary["cached"] == 2 and summary["searched"] == 0
    assert (work / PUBMED).read_text() == first

    # --refresh reruns the searches but still takes the abstracts from the cache.
    fake = default_ncbi()
    pubmed.search_pairs(
        work, tmp_path / "cache", eutils=eutils(fake), refresh=True, nodenorm=nodenorm
    )
    assert len(fake.endpoint_calls("esearch.fcgi")) == 2
    assert fake.endpoint_calls("efetch.fcgi") == []


def test_more_abstracts_than_cached_reruns_the_search(tmp_path):
    work = write_work(tmp_path / "work")
    cache_dir = tmp_path / "cache"

    def searches(max_abstracts: int) -> int:
        fake = default_ncbi()
        pubmed.search_pairs(
            work,
            cache_dir,
            max_abstracts=max_abstracts,
            eutils=eutils(fake),
            nodenorm=FakeNodeNorm(),
        )
        return len(fake.endpoint_calls("esearch.fcgi"))

    assert searches(1) == 2
    assert [p.pmid for p in by_pair(work)[DRUG_DISEASE].pmids] == ["111", "11287972"]
    # The drug-disease search had more hits than it kept; the drug-gene search had them all.
    assert searches(3) == 1
    assert [p.rank for p in by_pair(work)[DRUG_DISEASE].pmids] == [1, 2, 3]
    assert searches(2) == 0  # fewer than cached: served from the cache
    assert [p.pmid for p in by_pair(work)[DRUG_DISEASE].pmids] == ["111", "11287972"]
    assert searches(20) == 1
    assert searches(20) == 0  # all 4 hits are cached: nothing more to find


def test_nodenorm_failure_falls_back_to_own_names(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(pubmed, "NODENORM_BATCH", 1)
    work = write_work(tmp_path / "work")
    nodenorm = FakeNodeNorm(error=httpx.ConnectError("no route to host"))
    fake = default_ncbi()
    summary = pubmed.search_pairs(work, tmp_path / "cache", eutils=eutils(fake), nodenorm=nodenorm)
    assert "NodeNorm failed" in capsys.readouterr().err
    assert len(nodenorm.calls) == 1  # warned once, not retried per batch
    record = by_pair(work)[DRUG_DISEASE]
    assert record.query == (
        '("imatinib"[tiab] OR "Gleevec"[tiab] OR "STI571"[tiab] OR "STI-571"[tiab]) AND '
        '("chronic myelogenous leukemia, BCR-ABL1 positive"[tiab] OR '
        '"chronic myeloid leukemia"[tiab] OR "CML"[tiab]) AND hasabstract'
    )
    assert summary["failed"] == 0 and summary["nodenorm_known"] == 0
    # Nothing was cached for NodeNorm, so the next run asks again.
    with PubMedCache(tmp_path / "cache" / config.PUBMED_CACHE) as cache:
        assert cache.get_nodenorm([IMATINIB]) == {}


def test_nodenorm_is_batched_cached_and_optional(tmp_path, monkeypatch):
    monkeypatch.setattr(pubmed, "NODENORM_BATCH", 2)
    work = write_work(tmp_path / "work")
    nodenorm = FakeNodeNorm(entries={IMATINIB: NODENORM[IMATINIB]})  # CML, ABL1 unknown
    pubmed.search_pairs(work, tmp_path / "cache", eutils=eutils(default_ncbi()), nodenorm=nodenorm)
    assert [len(c) for c in nodenorm.calls] == [2, 1]
    with PubMedCache(tmp_path / "cache" / config.PUBMED_CACHE) as cache:
        assert cache.get_nodenorm([CML, IMATINIB]) == {CML: None, IMATINIB: NODENORM[IMATINIB]}

    nodenorm = FakeNodeNorm()
    pubmed.search_pairs(
        work,
        tmp_path / "cache2",
        eutils=eutils(default_ncbi()),
        nodenorm=nodenorm,
        use_nodenorm=False,
    )
    assert nodenorm.calls == []
    assert by_pair(work)[DRUG_DISEASE].mesh == {IMATINIB: [], CML: []}


def test_pair_without_usable_name(tmp_path, capsys):
    unnamed = "CHEMBL.COMPOUND:CHEMBL999"
    pairs = [
        Pair(
            pair_id=pair_id(unnamed, ABL1),
            nodes=tuple(sorted((unnamed, ABL1))),
            statement_ids=["s3"],
            source_pmids=["555"],
        ),
        Pair(
            pair_id=pair_id("XX:1", ABL1), nodes=tuple(sorted(("XX:1", ABL1))), statement_ids=["s4"]
        ),
    ]
    work = write_work(tmp_path / "work", pairs)
    fake = default_ncbi()
    summary = pubmed.search_pairs(
        work, tmp_path / "cache", eutils=eutils(fake), nodenorm=FakeNodeNorm()
    )
    assert summary["skipped"] == 2 and summary["skipped_kept"] == 1
    assert fake.endpoint_calls("esearch.fcgi") == []
    # The pair with a source PMID keeps it, unsearched.
    record = by_pair(work)[pairs[0].pair_id]
    assert record.query == "" and record.total == 0
    assert [(p.pmid, p.origin, p.rank) for p in record.pmids] == [("555", "source", None)]
    assert pairs[1].pair_id not in by_pair(work)
    err = capsys.readouterr().err
    assert f"pair {pairs[0].pair_id} not searched: no usable name for {unnamed}" in err


def test_failed_pair_is_skipped_and_reported(tmp_path, capsys):
    work = write_work(tmp_path / "work")
    # The first search fails all its tries; the second pair goes through.
    fake = default_ncbi(fail=[httpx.Response(502)] * 4)
    summary = pubmed.search_pairs(
        work, tmp_path / "cache", eutils=eutils(fake), nodenorm=FakeNodeNorm()
    )
    assert summary["failed"] == 1 and summary["failed_pairs"] == [DRUG_DISEASE]
    assert set(by_pair(work)) == {DRUG_GENE}
    assert f"pair {DRUG_DISEASE} failed" in capsys.readouterr().err


def test_limit(tmp_path):
    work = write_work(tmp_path / "work")
    summary = pubmed.search_pairs(
        work, tmp_path / "cache", limit=1, eutils=eutils(default_ncbi()), nodenorm=FakeNodeNorm()
    )
    assert summary["pairs"] == 1
    assert set(by_pair(work)) == {DRUG_DISEASE}


def _args(tmp_path: Path, *argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    pubmed.add_arguments(parser)
    args = parser.parse_args(list(argv))
    args.work, args.cache_dir = tmp_path / "work", tmp_path / "cache"
    args.input, args.output = tmp_path / "filter", tmp_path / "out"
    return args


def test_cli_defaults_and_missing_inputs(tmp_path, capsys):
    args = _args(tmp_path)
    assert args.max_abstracts == config.MAX_ABSTRACTS and args.limit is None
    assert not args.refresh and not args.no_nodenorm
    assert pubmed.run(args) == 1
    assert "run `kgb-augment select` first" in capsys.readouterr().err


def test_run_returns_1_when_a_pair_fails(tmp_path, monkeypatch, capsys):
    write_work(tmp_path / "work")
    fake = default_ncbi(fail=[httpx.Response(502)] * 4)
    real = pubmed.EUtils

    def patched(*a, **kw):
        return real(httpx.Client(transport=httpx.MockTransport(fake)), rate=0, sleep=lambda s: None)

    monkeypatch.setattr(pubmed, "EUtils", patched)
    monkeypatch.setenv(config.NCBI_EMAIL, "")
    args = _args(tmp_path, "--no-nodenorm")
    assert pubmed.run(args) == 1
    out, err = capsys.readouterr()
    assert "pubmed: 2 pairs: 1 searched" in out and "1 failed" in out
    assert "rerun to retry; cached results are kept" in err
    assert f"{config.NCBI_EMAIL} is not set" in err

    # The rerun retries the failed pair and serves the other from the cache.
    fake.fail = []
    assert pubmed.run(args) == 0
    assert set(by_pair(tmp_path / "work")) == {DRUG_DISEASE, DRUG_GENE}


def test_run_tolerates_a_small_share_of_failed_pairs(tmp_path, monkeypatch, capsys):
    write_work(tmp_path / "work")
    fake = default_ncbi(fail=[httpx.Response(502)] * 4)
    real = pubmed.EUtils

    def patched(*a, **kw):
        return real(httpx.Client(transport=httpx.MockTransport(fake)), rate=0, sleep=lambda s: None)

    monkeypatch.setattr(pubmed, "EUtils", patched)
    monkeypatch.setattr(pubmed, "MAX_FAILED_SHARE", 0.5)
    assert pubmed.run(_args(tmp_path, "--no-nodenorm")) == 0
    assert "rerun to retry" in capsys.readouterr().err


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
