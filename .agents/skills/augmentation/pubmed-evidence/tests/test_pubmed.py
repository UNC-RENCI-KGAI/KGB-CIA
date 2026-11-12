"""Step 2 (pubmed) without the network: what it reads from and writes to the work directory,
and its command line, with E-utilities through `httpx.MockTransport` and NodeNorm as a fake
function. The search itself is `shared.pubmed.search_pairs`, tested in the repository's
`tests/test_pubmed.py`. Also what publication types make a paper: its article type, retraction
and study design."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args
from urllib.parse import parse_qsl

import httpx
import pytest
from pubmed_evidence import config, pubmed
from pubmed_evidence.cache import PubMedCache
from pubmed_evidence.graphio import pair_id, write_jsonl
from pubmed_evidence.records import ENTITIES, PAIRS, PUBMED, Entity, Pair, read_records
from toy import ABL1, CML, IMATINIB

from shared.pubmed import MAX_ABSTRACTS, EUtils, PubmedRecord
from shared.pubmed import eutils as eutils_module
from shared.pubmed import pairs as pairs_module
from shared.schema import StudyDesign

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
      <PublicationTypeList>
        <PublicationType UI="D017426">Clinical Trial, Phase I</PublicationType>
        <PublicationType UI="D016428">Journal Article</PublicationType>
      </PublicationTypeList>
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


def eutils(fake: FakeNCBI) -> EUtils:
    client = httpx.Client(transport=httpx.MockTransport(fake))
    return EUtils(client, email="test@example.org", rate=0, sleep=lambda s: None)


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
    write_jsonl(work / PAIRS, pairs)
    write_jsonl(work / ENTITIES, ENTITY_RECORDS if entities is None else entities)
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


# --- The step ---------------------------------------------------------------------------------


def test_search_writes_a_record_per_pair(tmp_path):
    work = write_work(tmp_path / "work")
    fake = default_ncbi()
    summary = pubmed.search(work, client=eutils(fake), nodenorm=FakeNodeNorm()).summary()
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
    with PubMedCache() as cache:
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
    pubmed.search(work, client=eutils(default_ncbi()), nodenorm=FakeNodeNorm())
    first = (work / PUBMED).read_text()

    fake, nodenorm = default_ncbi(), FakeNodeNorm()
    summary = pubmed.search(work, client=eutils(fake), nodenorm=nodenorm).summary()
    assert fake.calls == [] and nodenorm.calls == []
    assert summary["requests"] == 0 and summary["cached"] == 2 and summary["searched"] == 0
    assert (work / PUBMED).read_text() == first

    # --refresh reruns the searches but still takes the abstracts from the cache.
    fake = default_ncbi()
    pubmed.search(work, client=eutils(fake), refresh=True, nodenorm=nodenorm)
    assert len(fake.endpoint_calls("esearch.fcgi")) == 2
    assert fake.endpoint_calls("efetch.fcgi") == []


def test_failed_pair_is_skipped_and_reported(tmp_path, capsys):
    work = write_work(tmp_path / "work")
    # The first search fails all its tries; the second pair goes through.
    fake = default_ncbi(fail=[httpx.Response(502)] * 4)
    summary = pubmed.search(work, client=eutils(fake), nodenorm=FakeNodeNorm()).summary()
    assert summary["failed"] == 1 and summary["failed_pairs"] == [DRUG_DISEASE]
    assert set(by_pair(work)) == {DRUG_GENE}
    assert f"pair {DRUG_DISEASE} failed" in capsys.readouterr().err


def test_limit(tmp_path):
    work = write_work(tmp_path / "work")
    result = pubmed.search(work, limit=1, client=eutils(default_ncbi()), nodenorm=FakeNodeNorm())
    assert result.pairs == 1
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
    assert args.max_abstracts == MAX_ABSTRACTS and args.limit is None
    assert not args.refresh and not args.no_nodenorm
    assert pubmed.run(args) == 1
    assert "run `kgb-augment select` first" in capsys.readouterr().err


def default_client(monkeypatch, fake: FakeNCBI) -> None:
    """The client the search opens when given none: one that sends to `fake`."""

    def client(*a, **kw):
        return EUtils(
            httpx.Client(transport=httpx.MockTransport(fake)), rate=0, sleep=lambda s: None
        )

    monkeypatch.setattr(pairs_module, "EUtils", client)


def test_run_returns_1_when_a_pair_fails(tmp_path, monkeypatch, capsys):
    write_work(tmp_path / "work")
    fake = default_ncbi(fail=[httpx.Response(502)] * 4)
    default_client(monkeypatch, fake)
    monkeypatch.setenv(eutils_module.NCBI_EMAIL, "")
    args = _args(tmp_path, "--no-nodenorm")
    assert pubmed.run(args) == 1
    out, err = capsys.readouterr()
    assert "pubmed: 2 pairs: 1 searched" in out and "1 failed" in out
    assert "rerun to retry; cached results are kept" in err
    assert f"{eutils_module.NCBI_EMAIL} is not set" in err

    # The rerun retries the failed pair and serves the other from the cache.
    fake.fail = []
    assert pubmed.run(args) == 0
    assert set(by_pair(tmp_path / "work")) == {DRUG_DISEASE, DRUG_GENE}


def test_run_tolerates_a_small_share_of_failed_pairs(tmp_path, monkeypatch, capsys):
    write_work(tmp_path / "work")
    default_client(monkeypatch, default_ncbi(fail=[httpx.Response(502)] * 4))
    monkeypatch.setattr(pairs_module, "MAX_FAILED_SHARE", 0.5)
    assert pubmed.run(_args(tmp_path, "--no-nodenorm")) == 0
    assert "rerun to retry" in capsys.readouterr().err


# --- Publication types ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "types, expected",
    [
        (["Journal Article"], ("research", False)),
        (["Journal Article", "Review"], ("review", False)),
        (["Review", "Comment"], ("opinion", False)),  # opinion is the most specific
        (["Journal Article", "Practice Guideline"], ("guideline", False)),
        (["Case Reports", "Journal Article"], ("case_report", False)),
        (["Clinical Trial, Phase II"], ("research", False)),
        (["Published Erratum"], ("other", False)),
        ([], ("other", False)),
        (["Journal Article", "Retracted Publication"], ("research", True)),
        (["Retracted Publication"], ("other", True)),
    ],
)
def test_article_type(types, expected):
    assert pubmed.article_type(types) == expected


@pytest.mark.parametrize(
    "types, expected",
    [
        (["Journal Article"], None),
        ([], None),
        (["Journal Article", "Randomized Controlled Trial"], "rct"),
        (["Clinical Trial, Phase III", "Journal Article"], "cohort"),
        (["Network Meta-Analysis"], "meta_analysis"),
        (["Letter"], "narrative_review"),
        # Of several, the one StudyDesign lists first.
        (["Case Reports", "Review"], "case_report"),
        (["Review", "Systematic Review"], "systematic_review"),
        (["Systematic Review", "Meta-Analysis", "Review"], "meta_analysis"),
        (["Randomized Controlled Trial", "Meta-Analysis"], "meta_analysis"),
        (["Observational Study", "Randomized Controlled Trial"], "rct"),
        (["Case Reports", "Clinical Trial"], "cohort"),
        (["Comment", "Clinical Trial"], "cohort"),
        # Veterinary studies and protocols name no design: the extractor's answer stands.
        (["Randomized Controlled Trial, Veterinary"], None),
        (["Clinical Trial Protocol"], None),
        (["Retracted Publication", "Equivalence Trial"], "rct"),
    ],
)
def test_study_design(types, expected):
    assert pubmed.study_design(types) == expected


def test_design_of():
    assert pubmed.design_of(["Meta-Analysis"], "animal") == ("meta_analysis", "pubmed")
    assert pubmed.design_of(["Journal Article"], "animal") == ("animal", "extractor")


def test_every_publication_type_in_the_design_table_names_a_design():
    # DESIGN.md's table (step 3, "Study design and publication data"), with "Clinical Trial and
    # its phases" spelled out.
    assert config.PUBMED_DESIGNS == {
        **dict.fromkeys(["Meta-Analysis", "Network Meta-Analysis"], "meta_analysis"),
        "Systematic Review": "systematic_review",
        **dict.fromkeys(
            ["Randomized Controlled Trial", "Equivalence Trial", "Pragmatic Clinical Trial"], "rct"
        ),
        **dict.fromkeys(
            [
                "Observational Study",
                "Clinical Trial",
                *(f"Clinical Trial, Phase {p}" for p in ("I", "II", "III", "IV")),
                "Controlled Clinical Trial",
            ],
            "cohort",
        ),
        "Case Reports": "case_report",
        **dict.fromkeys(["Review", "Editorial", "Comment", "Letter"], "narrative_review"),
    }
    assert set(config.PUBMED_DESIGNS.values()) <= set(get_args(StudyDesign))
