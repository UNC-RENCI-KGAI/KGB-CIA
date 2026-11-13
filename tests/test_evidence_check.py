"""The evidence-check skill end to end, with PubMed and Jev replaced by fakes."""

import csv
import importlib.util
import json
import sys
import types
from collections import Counter
from pathlib import Path

import pytest

from shared.pubmed import parse_efetch

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
sample = _load("label_sample")

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

    articles = {a.pmid: a for a in parse_efetch(PUBMED_XML)}
    asked = []

    def fake_fetch(pmids):
        asked.append(list(pmids))
        return {p: articles[p] for p in pmids if p in articles}

    monkeypatch.setattr(gather, "fetch_abstracts", fake_fetch)
    monkeypatch.setattr(gather, "other_names", lambda names, ids: {"C:A": ["DA-1"]})
    return graph, qpath


def test_question_problems_are_all_reported(tmp_path):
    path = tmp_path / "q.json"
    path.write_text(json.dumps({"name": "Bad Name", "options": [{"name": "a", "role": "maybe"}]}))
    with pytest.raises(question.QuestionError) as e:
        question.load(path)
    message = str(e.value)
    for problem in ("name:", "instructions:", "at least two", "role must be", "select:"):
        assert problem in message


WRONG = {"wrong_mechanism": 0.8}


@pytest.mark.parametrize(
    "based_on_text,articles,verdict",
    [
        (False, [{"supports": 0.9, "contradicts": 0.0}], ("keep", None)),
        (False, [{"supports": 0.1, "contradicts": 0.8}], ("drop", None)),
        (False, [{"supports": 0.1, "contradicts": 0.1}], ("unconfirmed", None)),
        (False, [{"supports": 0.8, "contradicts": 0.0}, {"supports": 0.0, "contradicts": 0.9}],
         ("keep", None)),
        # a paper showing the drug acting differently flags the edge instead of dropping it
        (False, [{"supports": 0.1, "contradicts": 0.0, "flags": WRONG}],
         ("flag", "wrong_mechanism")),
        (False, [{"supports": 0.0, "contradicts": 0.4, "flags": {"wrong_mechanism": 0.5}}],
         ("flag", "wrong_mechanism")),
        (False, [{"supports": 0.0, "contradicts": 0.5, "flags": {"wrong_mechanism": 0.4}}],
         ("drop", None)),
        (False, [{"supports": 0.8, "contradicts": 0.0}, {"supports": 0.0, "contradicts": 0.0,
                                                          "flags": WRONG}], ("keep", None)),
        # text-mined and unsupported: "not stated in what we read" is flagged, not dropped
        (True, [{"supports": 0.1, "contradicts": 0.1}], ("flag", "unsupported_by_abstract")),
        (True, [{"supports": 0.1, "contradicts": 0.8}], ("drop", None)),
        (True, [{"supports": 0.5, "contradicts": 0.1}], ("unconfirmed", None)),
    ],
)  # fmt: skip
def test_edge_verdict(based_on_text, articles, verdict):
    q = {"threshold": 0.7}
    assert question.edge_verdict(q, based_on_text, articles) == verdict


def test_only_a_contradicting_option_can_flag_and_it_needs_a_reason(tmp_path):
    path = tmp_path / "q.json"
    options = [
        {"name": "fine", "role": "supports", "what": "x", "flag": "odd"},
        {"name": "wrong", "role": "contradicts", "what": "y", "flag": "Wrong Mechanism"},
    ]
    path.write_text(json.dumps({**QUESTION, "options": options}))
    with pytest.raises(question.QuestionError) as e:
        question.load(path)
    message = str(e.value)
    for problem in ("only a contradicts option can flag", "flag: lowercase", "flag_reason"):
        assert problem in message


def test_pmids_from_curies_and_urls():
    edge = {
        "publications": ["PMID:5", "DOI:x"],
        "supporting_documents": "https://pubmed.ncbi.nlm.nih.gov/7/",
    }
    assert gather.cited_pmids(edge) == ["5", "7"]


def test_other_names_from_nodenorm(tmp_path, monkeypatch):
    monkeypatch.setenv("KGB_PUBMED_CACHE", str(tmp_path / "cache.sqlite"))
    labels = {
        "CHEBI:1": ["Topiroxostat", "FYX-051", "TOPIROXOSTAT"],
        "UniProtKB:P1": ["XDH_HUMAN Xanthine dehydrogenase/oxidase (sprot)", "XDH", "XDH gene"],
    }
    entries = {
        c: {
            "id": {"identifier": c, "label": ls[0]},
            "equivalent_identifiers": [
                {"identifier": f"{c}#{i}", "label": x} for i, x in enumerate(ls)
            ],
        }
        for c, ls in labels.items()
    }
    monkeypatch.setattr(gather, "load_nodenorm", lambda ids, cache: entries)
    names = {"CHEBI:1": "Topiroxostat", "UniProtKB:P1": gather.tidy(labels["UniProtKB:P1"][0])}
    others = gather.other_names(names, set(names))
    assert names["UniProtKB:P1"] == "Xanthine dehydrogenase/oxidase"
    assert others == {"CHEBI:1": ["FYX-051"], "UniProtKB:P1": ["XDH"]}
    assert gather.describe("CHEBI:1", names, others) == "Topiroxostat (also: FYX-051) [CHEBI:1]"


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
    assert a_p1["subject"] == "drug A (also: DA-1) [C:A]"
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
    assert "TYPESAFE_API_KEY=<key" in capsys.readouterr().out
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
    assert 0.7 <= drops[0]["confidence"] <= 1
    verdicts = {
        tuple(v["key"][0:3:2]): v["verdict"]
        for v in map(json.loads, open("data/cleaning/evidence/drug_target/verdicts.jsonl"))
    }
    assert verdicts == {("C:A", "P:1"): "keep", ("C:B", "P:2"): "drop"}

    from agents.cleaning import garbage
    from shared.schema import edge_statement_id

    keys = garbage.load_drop_list(Path("data/cleaning/drop_lists/drug_target.jsonl"))
    statement = edge_statement_id(_edge("C:B", "P:2"))
    assert keys == {("C:B", "biolink:interacts_with", "P:2", "infores:dc", statement)}
    assert drops[0]["causal_mechanism_qualifier"] == "biolink:inhibition"


def test_build_writes_the_flag_list_cleaning_reads(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    q = {
        **QUESTION,
        "options": [
            {"name": "different_action", "role": "contradicts", "what": "acts another way",
             "flag": "wrong_mechanism",
             "flag_reason": "a cited paper reports a different action than the edge's"},
            *QUESTION["options"],
        ],
    }  # fmt: skip
    qpath = tmp_path / "q.json"
    qpath.write_text(json.dumps(q))
    work = tmp_path / "data/cleaning/evidence/drug_target"
    work.mkdir(parents=True)
    key = ["C:A", "biolink:interacts_with", "P:1", "infores:dc", "0123456789abcdef"]
    statement = {"causal_mechanism_qualifier": "biolink:inhibition"}
    (work / "evidence.jsonl").write_text(
        json.dumps({"key": key, "statement": statement, "pmid": "111", "based_on_text": False,
                    "claim": "A inhibits P1"}) + "\n"
    )  # fmt: skip
    probabilities = {"different_action": 0.9, "not_reported": 0.1}
    (work / "answers.jsonl").write_text(
        json.dumps({"key": key, "pmid": "111", "choice": "different_action",
                    "probabilities": probabilities}) + "\n"
    )  # fmt: skip
    assert build.main([str(qpath)]) == 0
    [line] = [json.loads(x) for x in open("data/cleaning/flag_lists/drug_target.jsonl")]
    assert line == {
        "subject": "C:A",
        "predicate": "biolink:interacts_with",
        "object": "P:1",
        "primary_knowledge_source": "infores:dc",
        "statement_id": "0123456789abcdef",
        "causal_mechanism_qualifier": "biolink:inhibition",
        "flag": "wrong_mechanism",
        "reason": "a cited paper reports a different action than the edge's (PMID:111)",
    }
    assert open("data/cleaning/drop_lists/drug_target.jsonl").read() == ""


def test_gather_skips_curated_edges_augmentation_checked_but_not_text_mined(workspace, capsys):
    graph, qpath = workspace
    with open(graph / "edges.jsonl", "a") as f:
        curated = _edge("C:B", "P:1", publications=["PMID:111"], augmentation="checked")
        mined = _edge(
            "C:A", "P:3", publications=["PMID:111"], augmentation="checked",
            agent_type="text_mining_agent",
        )  # fmt: skip
        f.write(json.dumps(curated) + "\n" + json.dumps(mined) + "\n")
    gather.main([str(qpath), str(graph)])
    assert "Skipped 1 selected curated edges" in capsys.readouterr().out
    rows = [json.loads(x) for x in open("data/cleaning/evidence/drug_target/evidence.jsonl")]
    keys = {tuple(r["key"][0:3:2]) for r in rows}
    assert ("C:B", "P:1") not in keys
    assert ("C:A", "P:3") in keys
    assert next(r for r in rows if r["key"][2] == "P:3")["based_on_text"]


def test_gather_defaults_to_the_latest_stage_graph(workspace, monkeypatch, capsys):
    graph, qpath = workspace
    with pytest.raises(SystemExit, match="none of data/augmentation"):
        gather.main([str(qpath)])
    cleaning = Path("data/cleaning")
    cleaning.mkdir(parents=True)
    for name in ("nodes.jsonl", "edges.jsonl"):
        (cleaning / name).write_text((graph / name).read_text())
    gather.main([str(qpath)])
    assert "Graph: data/cleaning" in capsys.readouterr().out


def _record(verdict, flag, *articles):
    key = ["C:A", "biolink:interacts_with", "P:1", "infores:dc", "0123456789abcdef"]
    return {"key": key, "verdict": verdict, "flag": flag, "articles": list(articles)}


def _article(pmid, supports=0.0, contradicts=0.0, flags=None):
    return {"pmid": pmid, "supports": supports, "contradicts": contradicts,
            "silent": 1 - supports - contradicts, "flags": flags or {}}  # fmt: skip


@pytest.mark.parametrize(
    "record, verdict, confidence",
    [
        (_record("keep", None, _article("1", 0.3), _article("2", 0.9)), "keep", "0.900"),
        (_record("unconfirmed", None, _article("1", 0.2)), "keep", "0.200"),
        (_record("drop", None, _article("1", contradicts=0.8, flags={"wm": 0.1})), "prune",
         "0.900"),
        (_record("flag", "wm", _article("1", 0.1, flags={"wm": 0.8})), "keep", "0.900"),
        (_record("flag", "unsupported_by_abstract", _article("1", 0.05)), "keep", "0.050"),
    ],
)  # fmt: skip
def test_gold_row_verdict_and_confidence(record, verdict, confidence):
    row = build.gold_row({"name": "q"}, record)
    assert (row["verdict"], row["confidence"]) == (verdict, confidence)
    assert row["evidence_check"] == record["verdict"]
    assert row["publications"] == "|".join(f"PMID:{a['pmid']}" for a in record["articles"])


def test_build_writes_the_gold_set(workspace, fake_sdk):
    graph, qpath = workspace
    gather.main([str(qpath), str(graph)])
    judge.main([str(qpath)], client=FakeJev())
    build.main([str(qpath)])

    with open("data/cleaning/evidence/drug_target/gold_set.tsv") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    assert list(rows[0]) == list(build.GOLD_COLUMNS)
    assert {(r["subject"], r["object"], r["verdict"]) for r in rows} == {
        ("C:A", "P:1", "keep"),
        ("C:B", "P:2", "prune"),
    }


def test_label_sample_takes_every_rare_verdict_and_splits_the_rest():
    verdicts = (
        [{"verdict": "flag"}] * 2
        + [{"verdict": "drop"}]
        + [{"verdict": "keep"}] * 40
        + [{"verdict": "unconfirmed"}] * 5
    )
    drawn = Counter(v["verdict"] for v in sample.draw(verdicts, 20, 0))
    assert drawn == {"flag": 2, "drop": 1, "keep": 12, "unconfirmed": 5}


def test_label_sample_then_score(workspace, fake_sdk, capsys):
    graph, qpath = workspace
    gather.main([str(qpath), str(graph)])
    judge.main([str(qpath)], client=FakeJev())
    build.main([str(qpath)])
    assert sample.main([str(qpath)]) == 0
    path = Path("data/cleaning/evidence/drug_target/label_sample.tsv")
    with open(path) as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    assert "judge_role" not in rows[0]  # blind: the judge's answers are in the key
    with open(path.with_name("label_key.tsv")) as f:
        key = {r["row"]: r for r in csv.DictReader(f, delimiter="\t")}
    assert {k["evidence_check"] for k in key.values()} == {"keep", "drop"}
    for r in rows:  # agree with the judge on the kept edge only
        r["label"] = "supports" if key[r["row"]]["evidence_check"] == "keep" else "silent"
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, sample.COLUMNS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    assert sample.main([str(qpath), "--score"]) == 0
    scored = json.loads(Path("data/cleaning/evidence/drug_target/label_score.json").read_text())
    assert scored["keep"] == {"labelled": 1, "agree": 1}
    assert scored["drop"] == {"labelled": 1, "agree": 0}


GRAPHIO = Path(__file__).parents[1] / ".agents/skills/augmentation/pubmed-evidence/scripts"


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"causal_mechanism_qualifier": "biolink:inhibition"},
        {"qualified_predicate": "causes", "object_aspect_qualifier": "expression"},
        {"object_direction_qualifier": "increased", "negated": "true"},
        {"negated": True, "subject_form_or_variant_qualifier": ""},
    ],
)
def test_statement_id_is_the_augmentation_agents(extra, monkeypatch):
    """Cleaning and augmentation must agree on which edges make the same statement."""
    monkeypatch.syspath_prepend(str(GRAPHIO))
    from pubmed_evidence import graphio

    from shared.schema import Edge, edge_statement_id

    record = {
        **_edge("C:A", "P:1"),
        "knowledge_level": "knowledge_assertion",
        "agent_type": "manual_agent",
        **extra,
    }
    record.pop("causal_mechanism_qualifier")
    record.update(extra)
    assert edge_statement_id(record) == graphio.edge_statement_id(Edge.model_validate(record))


def test_edges_that_differ_only_in_qualifiers_are_judged_and_dropped_apart(workspace, fake_sdk):
    """Two A-P2 edges from one source, one with each mechanism: the abstract contradicts both,
    but each is its own claim, its own verdict and its own drop line, which kgb-clean then
    matches to that edge only."""
    graph, qpath = workspace
    cited = {"supporting_documents": "https://pubmed.ncbi.nlm.nih.gov/222/"}
    with open(graph / "edges.jsonl", "a") as f:
        f.write(json.dumps(_edge("C:A", "P:2", **cited)) + "\n")
        agonist = _edge("C:A", "P:2", **cited, causal_mechanism_qualifier="biolink:agonism")
        f.write(json.dumps(agonist) + "\n")
    gather.main([str(qpath), str(graph)])
    rows = [json.loads(line) for line in open("data/cleaning/evidence/drug_target/evidence.jsonl")]
    claims = sorted(r["claim"] for r in rows if r["key"][:3] == ["C:A", "biolink:interacts_with",
                                                                  "P:2"])  # fmt: skip
    assert claims == ["drug A interacts with (agonism) P2", "drug A interacts with (inhibition) P2"]

    judge.main([str(qpath)], client=FakeJev())
    build.main([str(qpath)])
    drops = [json.loads(line) for line in open("data/cleaning/drop_lists/drug_target.jsonl")]
    a_p2 = sorted(
        (d["causal_mechanism_qualifier"], d["statement_id"]) for d in drops if d["subject"] == "C:A"
    )
    assert [mechanism for mechanism, _ in a_p2] == ["biolink:agonism", "biolink:inhibition"]
    assert a_p2[0][1] != a_p2[1][1]

    from agents.cleaning import garbage

    # Keep the agonism line only: kgb-clean then drops that edge and keeps the inhibition one.
    lists = Path("lists/drop_lists")
    lists.mkdir(parents=True)
    agonism = next(d for d in drops if d.get("causal_mechanism_qualifier") == "biolink:agonism")
    (lists / "check.jsonl").write_text(json.dumps(agonism) + "\n")
    keys = garbage.load_drop_lists(lists)
    assert garbage.edge_drop_reason(agonist, {"C:A", "P:2"}, set(), keys) == "drop_list:check"
    inhibition = _edge("C:A", "P:2", **cited)
    assert garbage.edge_drop_reason(inhibition, {"C:A", "P:2"}, set(), keys) is None


def test_build_refuses_evidence_from_before_statement_keys(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    qpath = tmp_path / "q.json"
    qpath.write_text(json.dumps(QUESTION))
    work = tmp_path / "data/cleaning/evidence/drug_target"
    work.mkdir(parents=True)
    old = {"key": ["C:A", "biolink:interacts_with", "P:1", "infores:dc"], "pmid": "111"}
    (work / "evidence.jsonl").write_text(json.dumps({**old, "claim": "c", "based_on_text": False}))
    (work / "answers.jsonl").write_text(json.dumps({**old, "choice": "x", "probabilities": {}}))
    assert build.main([str(qpath)]) == 1
    assert "rerun gather_evidence.py" in capsys.readouterr().out
