"""The whole stage through `cli.main`, as `shared/pipeline.py` runs it: `main([])` from the
repository root, every step with its defaults. PubMed, NodeNorm and the models are fakes; the
Biolink lookups are real.

The fake literature: PMID 100 says imatinib treats CML and PMID 101 that it doesn't; PMID 200
says imatinib inhibits ABL1 kinase activity, which confirms both imatinib-ABL1 edges and
supports the binding edge's mechanism. The fake labeler says exactly that, and the fake reviewer
accepts everything. No API keys: CI has none.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import pytest
from pubmed_evidence import cli, config, graphio, llm, pubmed, records
from toy import ABL1, CML, EDGES, IMATINIB, NODES

from agents.augmentation import cli as stage
from shared import nodenorm
from shared.schema import Edge

ABSTRACTS = {
    "100": "Imatinib induced durable complete cytogenetic responses in patients with chronic "
    "myeloid leukemia.",
    "101": "We conclude that imatinib is not an effective treatment for chronic myeloid leukemia.",
    "200": "Imatinib potently inhibited ABL1 kinase activity in vitro.",
}


def fake_ncbi(calls: list[str]):
    def handle(request: httpx.Request) -> httpx.Response:
        form = dict(parse_qsl(request.content.decode()))
        endpoint = request.url.path.rsplit("/", 1)[-1]
        calls.append(endpoint)
        if endpoint == "esearch.fcgi":
            term = form["term"]
            if '"imatinib"' in term and "myeloid" in term:
                ids = ["100", "101"]
            elif '"imatinib"' in term and '"ABL1"' in term:
                ids = ["200"]
            else:
                ids = []
            result = {"count": str(len(ids)), "retmax": str(len(ids)), "idlist": ids}
            return httpx.Response(200, json={"esearchresult": result})
        articles = "".join(
            f"<PubmedArticle><MedlineCitation><PMID>{p}</PMID><Article>"
            f"<ArticleTitle>Title {p}</ArticleTitle><Abstract><AbstractText>"
            f"{ABSTRACTS.get(p, f'Abstract of {p}.')}</AbstractText></Abstract></Article>"
            "</MedlineCitation></PubmedArticle>"
            for p in form["id"].split(",")
        )
        return httpx.Response(200, text=f"<PubmedArticleSet>{articles}</PubmedArticleSet>")

    return handle


class FakeModel:
    """Answers label requests (statements S1.., abstracts by PMID) and review requests (items
    R1..), from the request text alone."""

    def __init__(self, calls: list[str]):
        self.calls = calls

    def complete_json(self, system, user, *, max_output_tokens=16000, reasoning_effort=None):
        items = re.findall(r"^### (R\d+)", user, re.M)
        self.calls.append("review" if items else "label")
        usage = llm.Usage(1000, 100, 0, 0.001, 0.1)
        if items:
            return {
                "reviews": [{"item": i, "verdict": "accept", "reason": "ok"} for i in items]
            }, usage
        statements = dict(re.findall(r"^(S\d+): (.+)$", user, re.M))
        pmids = set(re.findall(r"^## PMID:(\d+)", user, re.M))
        labels, proposals = [], []
        for sid, sentence in statements.items():
            if sentence.startswith("imatinib treats") and "100" in pmids:
                quote = "imatinib induced durable complete cytogenetic responses"
                labels.append(
                    {"statement": sid, "pmid": "100", "label": "confirmed", "quote": quote}
                )
            if sentence.startswith("imatinib treats") and "101" in pmids:
                quote = "imatinib is not an effective treatment for chronic myeloid leukemia"
                labels.append(
                    {"statement": sid, "pmid": "101", "label": "contradicted", "quote": quote}
                )
            if sentence.startswith("imatinib") and "ABL1" in sentence and "200" in pmids:
                quote = "Imatinib potently inhibited ABL1 kinase activity"
                labels.append(
                    {"statement": sid, "pmid": "200", "label": "confirmed", "quote": quote}
                )
                if "interacts" in sentence:
                    proposals.append(
                        {
                            "statement": sid,
                            "qualifiers": {"causal_mechanism_qualifier": "inhibition"},
                            "claim": "causal",
                            "evidence": [{"pmid": "200", "quote": quote}],
                        }
                    )
        return {"labels": labels, "proposals": proposals}, usage


@pytest.fixture
def fakes(monkeypatch):
    """Route NCBI, NodeNorm and the models to fakes; return the list of calls they saw."""
    calls: list[str] = []
    real = pubmed.EUtils

    def eutils(*args, **kw):
        client = httpx.Client(transport=httpx.MockTransport(fake_ncbi(calls)))
        return real(client, email="test@example.org", rate=0, sleep=lambda s: None)

    monkeypatch.setattr(pubmed, "EUtils", eutils)
    monkeypatch.setattr(nodenorm, "normalize", lambda curies, **kw: {})
    monkeypatch.setattr(llm, "Client", lambda model, **kw: FakeModel(calls))
    for name in (config.LLM_API_KEY, config.NCBI_API_KEY, config.NCBI_EMAIL):
        monkeypatch.delenv(name, raising=False)
    return calls


def edges_by_statement(path: Path) -> dict[tuple, list[Edge]]:
    out: dict[tuple, list[Edge]] = {}
    for e in graphio.read_edges(path):
        key = (e.subject, e.predicate, e.object, graphio.is_negated(e))
        out.setdefault(key, []).append(e)
    return out


def test_main_with_no_arguments_runs_every_step(tmp_path, monkeypatch, fakes):
    graphio.write_jsonl(tmp_path / "data" / "filter" / "nodes.jsonl", NODES)
    graphio.write_jsonl(tmp_path / "data" / "filter" / "edges.jsonl", EDGES)
    monkeypatch.chdir(tmp_path)

    # Through the stage's entry point, exactly as the pipeline calls it.
    assert stage.main([]) == 0

    out = tmp_path / config.DEFAULT_OUTPUT
    assert (tmp_path / config.DEFAULT_CACHE_DIR / config.LABEL_CACHE).exists()
    edges = edges_by_statement(out)
    assert len(list(graphio.read_edges(out))) == len(EDGES) + 2

    # Supporting PMIDs are added to every edge of a confirmed statement, after its own.
    treats = edges[(IMATINIB, "biolink:treats", CML, False)]
    assert [e.publications for e in treats] == [["PMID:11287972", "PMID:100"], ["PMID:100"]]
    binds = edges[(IMATINIB, "biolink:directly_physically_interacts_with", ABL1, False)]
    assert binds[0].publications == ["PMID:10592173", "PMID:200"]
    assert binds[0].kgbcia_source_records == EDGES[2].kgbcia_source_records
    affects = edges[(IMATINIB, "biolink:affects", ABL1, False)]
    assert affects[0].publications == ["PMID:200"]

    # The contradiction becomes one negated edge from the text miner.
    [negated] = edges[(IMATINIB, "biolink:treats", CML, True)]
    assert negated.publications == ["PMID:101"]
    assert negated.primary_knowledge_source == config.INFORES
    assert (negated.knowledge_level, negated.agent_type) == (
        "knowledge_assertion",
        "text_mining_agent",
    )

    # The proposal becomes a qualified edge. A mechanism alone gets no qualified_predicate.
    qualified = [e for e in binds if graphio.qualifiers_of(e)]
    assert len(qualified) == 1
    assert graphio.qualifiers_of(qualified[0]) == {"causal_mechanism_qualifier": "inhibition"}
    assert qualified[0].publications == ["PMID:200"]
    assert qualified[0].primary_knowledge_source == config.INFORES

    # Everything else is untouched, byte for byte.
    lines = set((out / "edges.jsonl").read_text().splitlines())
    for line in (tmp_path / "data" / "filter" / "edges.jsonl").read_text().splitlines():
        if "participates_in" in line or "has_phenotype" in line or '"negated"' in line:
            assert line in lines
    untouched = [
        e for e in EDGES if e.predicate in {"biolink:participates_in", "biolink:has_phenotype"}
    ]
    for e in untouched:
        [after] = edges[(e.subject, e.predicate, e.object, False)]
        assert after == e

    # A rerun is served from the caches and writes the same graph.
    first = (out / "edges.jsonl").read_bytes()
    seen = len(fakes)
    assert cli.main([]) == 0
    assert len(fakes) == seen
    assert (out / "edges.jsonl").read_bytes() == first

    report = json.loads((out / "work" / "apply_report.json").read_text())
    assert report["negated_edges_added"] == 1 and report["qualified_edges_added"] == 1


def test_skip_review_adds_no_new_edges(tmp_path, fakes):
    graph = tmp_path / "filter"
    graphio.write_jsonl(graph / "nodes.jsonl", NODES)
    graphio.write_jsonl(graph / "edges.jsonl", EDGES)
    argv = ["--input", str(graph), "--output", str(tmp_path / "out")]
    argv += ["--cache-dir", str(tmp_path / "cache"), "--skip-review"]

    assert cli.main(argv) == 0
    assert "review" not in fakes
    edges = list(graphio.read_edges(tmp_path / "out"))
    assert len(edges) == len(EDGES)
    assert sum("PMID:100" in e.publications for e in edges) == 2


def test_steps_run_alone_with_options_before_or_after(tmp_path, fakes):
    graph = tmp_path / "filter"
    graphio.write_jsonl(graph / "nodes.jsonl", NODES)
    graphio.write_jsonl(graph / "edges.jsonl", EDGES)
    common = ["--input", str(graph), "--output", str(tmp_path / "out")]
    common += ["--cache-dir", str(tmp_path / "cache")]
    assert cli.main([*common, "select"]) == 0
    assert cli.main(["pubmed", *common, "--no-nodenorm"]) == 0
    assert (tmp_path / "out" / "work" / "pubmed.jsonl").exists()
    assert (tmp_path / "cache" / config.PUBMED_CACHE).exists()
    assert "esearch.fcgi" in fakes


def test_main_fails_cleanly_without_input(tmp_path, monkeypatch, capsys, fakes):
    monkeypatch.chdir(tmp_path)
    assert cli.main([]) == 1
    assert "select" in capsys.readouterr().err
    assert fakes == []


def common_args(tmp_path: Path) -> list[str]:
    graph = tmp_path / "filter"
    if not graph.exists():
        graphio.write_jsonl(graph / "nodes.jsonl", NODES)
        graphio.write_jsonl(graph / "edges.jsonl", EDGES)
    return [
        *("--input", str(graph), "--output", str(tmp_path / "out")),
        *("--cache-dir", str(tmp_path / "cache")),
    ]


def test_full_run_options_before_a_step_are_rejected(tmp_path, fakes):
    for argv in (["--model", "gpt-6-luna", "label"], ["--limit", "3", "pubmed"]):
        with pytest.raises(SystemExit) as exit:
            cli.main([*common_args(tmp_path), *argv])
        assert exit.value.code == 2
    assert fakes == []


def test_full_run_continues_a_tuned_run(tmp_path, fakes):
    common = common_args(tmp_path)
    assert cli.main(common) == 0
    # The skill tunes the prompt and switches model.
    tuned = tmp_path / "out" / "work" / "prompts" / "label.md"
    tuned.parent.mkdir(parents=True)
    tuned.write_text(config.LABEL_PROMPT.read_text() + "\nBe strict.\n")
    assert cli.main([*common, "label", "--prompt", str(tuned), "--model", "gpt-oss-120b"]) == 0
    run = records.read_label_run(tmp_path / "out" / "work")
    assert (run.model, run.prompt) == ("gpt-oss-120b", str(tuned))

    # The pipeline's full run reuses that run's labels instead of relabeling with the defaults;
    # only its review is new. After that, a rerun makes no calls at all.
    labels, reviews = fakes.count("label"), fakes.count("review")
    assert cli.main(common) == 0
    assert fakes.count("label") == labels and fakes.count("review") > reviews
    assert records.read_label_run(tmp_path / "out" / "work").model == "gpt-oss-120b"
    seen = len(fakes)
    assert cli.main(common) == 0
    assert len(fakes) == seen
