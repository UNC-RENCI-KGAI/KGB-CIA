"""The evidence-check skill end to end, with PubMed and Jev replaced by fakes."""

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parents[1] / ".agents/skills/cleaning/evidence-check/scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


question = _load("question")
gather = _load("gather_evidence")
judge = _load("judge_evidence")
build = _load("build_drop_list")

QUESTION = {
    "name": "drug_target",
    "select": {"predicate": "biolink:interacts_with"},
    "instructions": "Does `text` report `subject` acting on `object` as `claim` says?",
    "options": [
        {"name": "no_activity", "role": "contradicts", "what": "tested, no effect"},
        {"name": "not_reported", "role": "silent", "what": "not about this pair"},
        {"name": "acts_as_claimed", "role": "supports", "what": "acts as claimed"},
    ],
}

PUBMED_XML = """<PubmedArticleSet>
<PubmedArticle><MedlineCitation><PMID>111</PMID><Article>
<ArticleTitle>Drug A inhibits P1</ArticleTitle>
<Abstract><AbstractText Label="RESULTS">A inhibited P1 (IC50 5 nM).</AbstractText></Abstract>
</Article></MedlineCitation></PubmedArticle>
<PubmedArticle><MedlineCitation><PMID>222</PMID><Article><ArticleTitle>No effect of B</ArticleTitle>
<Abstract><AbstractText>B did not inhibit P2.</AbstractText></Abstract></Article></MedlineCitation>
</PubmedArticle>
<PubmedArticle><MedlineCitation><PMID>333</PMID><Article><ArticleTitle>Letter</ArticleTitle>
</Article></MedlineCitation></PubmedArticle>
</PubmedArticleSet>"""


def _edge(subject, obj, **kw):
    return {
        "subject": subject,
        "predicate": "biolink:interacts_with",
        "object": obj,
        "primary_knowledge_source": "infores:dc",
        "causal_mechanism_qualifier": "biolink:inhibition",
        **kw,
    }


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A tiny graph, a question file, and PubMed answering from PUBMED_XML."""
    monkeypatch.chdir(tmp_path)
    graph = tmp_path / "graph"
    graph.mkdir()
    nodes = [{"id": i, "name": n} for i, n in [("C:A", "drug A"), ("C:B", "drug B"), ("P:1", "P1"),
                                               ("P:2", "P2"), ("P:3", "P3")]]  # fmt: skip
    edges = [
        _edge("C:A", "P:1", publications=["PMID:111"]),
        _edge("C:A", "P:1", supporting_documents="http://www.ncbi.nlm.nih.gov/pubmed/111"),
        _edge("C:B", "P:2", supporting_documents="https://pubmed.ncbi.nlm.nih.gov/222/"),
        _edge("C:B", "P:3", publications=["PMID:333"]),
        _edge("C:A", "P:2"),  # cites nothing
        {**_edge("C:A", "P:3", publications=["PMID:111"]), "predicate": "biolink:related_to"},
    ]
    (graph / "nodes.jsonl").write_text("".join(json.dumps(n) + "\n" for n in nodes))
    (graph / "edges.jsonl").write_text("".join(json.dumps(e) + "\n" for e in edges))
    qpath = tmp_path / "question.json"
    qpath.write_text(json.dumps(QUESTION))

    class Response:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body.encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(gather.urllib.request, "urlopen", lambda *a, **k: Response(PUBMED_XML))
    monkeypatch.setattr(gather, "PAUSE", 0)
    return graph, qpath


def test_question_problems_are_all_reported(tmp_path):
    path = tmp_path / "q.json"
    path.write_text(json.dumps({"name": "Bad Name", "options": [{"name": "a", "role": "maybe"}]}))
    with pytest.raises(question.QuestionError) as e:
        question.load(path)
    message = str(e.value)
    for problem in ("name:", "instructions:", "at least two", "role must be", "select:"):
        assert problem in message


@pytest.mark.parametrize(
    "based_on_text,articles,verdict",
    [
        (False, [{"supports": 0.9, "contradicts": 0.0}], "keep"),
        (False, [{"supports": 0.1, "contradicts": 0.8}], "drop"),
        (False, [{"supports": 0.1, "contradicts": 0.1}], "unconfirmed"),
        (False, [{"supports": 0.8, "contradicts": 0.0}, {"supports": 0.0, "contradicts": 0.9}],
         "keep"),
        (True, [{"supports": 0.1, "contradicts": 0.1}], "drop"),
        (True, [{"supports": 0.5, "contradicts": 0.1}], "unconfirmed"),
    ],
)  # fmt: skip
def test_edge_verdict(based_on_text, articles, verdict):
    q = {"threshold": 0.7}
    assert question.edge_verdict(q, based_on_text, articles) == verdict


def test_pmids_from_curies_and_urls():
    edge = {
        "publications": ["PMID:5", "DOI:x"],
        "supporting_documents": "https://pubmed.ncbi.nlm.nih.gov/7/",
    }
    assert gather.pmids(edge) == ["5", "7"]


def test_gather_dedupes_edges_and_skips_missing_abstracts(workspace):
    graph, qpath = workspace
    assert gather.main([str(qpath), str(graph)]) == 0
    rows = [json.loads(line) for line in open("data/cleaning/evidence/drug_target/evidence.jsonl")]
    assert sorted((r["key"][0], r["key"][2], r["pmid"]) for r in rows) == [
        ("C:A", "P:1", "111"),
        ("C:B", "P:2", "222"),
    ]  # the repeated A-P1 edge is one; PMID 333 has no abstract; related_to isn't selected
    a_p1 = next(r for r in rows if r["key"][2] == "P:1")
    assert a_p1["claim"] == "drug A interacts with (inhibition) P1"
    assert a_p1["subject"] == "drug A [C:A]"
    assert "RESULTS: A inhibited P1" in a_p1["text"]


def test_limit_takes_the_same_sample_every_run(workspace):
    graph, qpath = workspace
    q = question.load(qpath)
    first = gather.select_edges(q, graph / "edges.jsonl", 2)
    again = gather.select_edges(q, graph / "edges.jsonl", 2)
    assert len(first) == 2 and [r["key"] for r in first] == [r["key"] for r in again]


def test_without_a_key_requests_are_written_not_sent(workspace, monkeypatch, capsys):
    graph, qpath = workspace
    gather.main([str(qpath), str(graph)])
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert judge.main([str(qpath)]) == 0
    assert "export TYPESAFE_API_KEY" in capsys.readouterr().out
    requests = [
        json.loads(line) for line in open("data/cleaning/evidence/drug_target/requests.jsonl")
    ]
    assert len(requests) == 2
    body = requests[0]
    assert body["model"] == "jev-1.13.0"
    assert list(body["questions"]["q"]["criteria"]) == [
        "no_activity",
        "not_reported",
        "acts_as_claimed",
    ]
    assert set(body["state"]) == {"text", "subject", "object", "claim"}
    assert not Path("data/cleaning/evidence/drug_target/answers.jsonl").exists()


@pytest.fixture
def fake_sdk(monkeypatch):
    """A stand-in typesafe_sdk module, so the judge's calls can be tested without the service."""
    module = types.ModuleType("typesafe_sdk")

    class TypeSafeError(Exception):
        pass

    class Choice:
        def __init__(self, instructions, criteria):
            self.instructions, self.criteria = instructions, criteria

    module.Choice, module.TypeSafeError = Choice, TypeSafeError
    monkeypatch.setitem(sys.modules, "typesafe_sdk", module)
    return module


class FakeJev:
    """Says the drug acts as claimed when the abstract says 'inhibited', no activity otherwise."""

    def __init__(self):
        self.calls = 0

    def system_one(self, state, questions):
        self.calls += 1
        assert list(questions["q"].criteria) == ["no_activity", "not_reported", "acts_as_claimed"]
        if "inhibited" in state["text"]:
            p = {"no_activity": 0.05, "not_reported": 0.05, "acts_as_claimed": 0.9}
        else:
            p = {"no_activity": 0.85, "not_reported": 0.1, "acts_as_claimed": 0.05}
        answer = types.SimpleNamespace(choice=max(p, key=p.get), probabilities=p)
        return types.SimpleNamespace(
            answers={"q": answer}, model="jev-1.13.0", usage=types.SimpleNamespace(input_tokens=100)
        )


def test_judge_then_build_writes_the_drop_list_cleaning_reads(workspace, fake_sdk, capsys):
    graph, qpath = workspace
    gather.main([str(qpath), str(graph)])
    jev = FakeJev()
    assert judge.main([str(qpath)], client=jev) == 0
    assert jev.calls == 2
    assert judge.main([str(qpath)], client=jev) == 0  # resumable: nothing asked twice
    assert jev.calls == 2

    assert build.main([str(qpath)]) == 0
    drops = [json.loads(line) for line in open("data/cleaning/drop_lists/drug_target.jsonl")]
    assert [(d["subject"], d["object"]) for d in drops] == [("C:B", "P:2")]
    assert drops[0]["pmids"] == ["222"] and drops[0]["primary_knowledge_source"] == "infores:dc"
    verdicts = {
        tuple(v["key"][::2]): v["verdict"]
        for v in map(json.loads, open("data/cleaning/evidence/drug_target/verdicts.jsonl"))
    }
    assert verdicts == {("C:A", "P:1"): "keep", ("C:B", "P:2"): "drop"}

    from agents.cleaning import garbage

    keys = garbage.load_drop_list(Path("data/cleaning/drop_lists/drug_target.jsonl"))
    assert keys == {("C:B", "biolink:interacts_with", "P:2", "infores:dc")}
