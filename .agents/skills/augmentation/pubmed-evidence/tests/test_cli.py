"""The whole stage through `cli.main`, as `shared/pipeline.py` runs it: `main([])` from the
repository root, every step with its defaults. PubMed, NodeNorm and the models are fakes; the
Biolink lookups are real.

The fake literature: PMID 100 says imatinib treats CML, PMID 101 that it doesn't, and PMID 102
that it doesn't help older patients, both trials also showing it was tested in clinical trials
for CML; PMID 200 says imatinib inhibits ABL1 kinase activity. The fake labeler gives exactly
these claims, all in human. The fake reviewer upholds whatever it is asked; the review step
sends the claims that would count, 100 and 200, so step 6 adds PMID 100 to both treats edges and
PMID 200 to the qualified imatinib-ABL1 edge. Claims 101 and 102 deny that imatinib treats CML,
and the graph has no edge for what they also support, so they aren't sent. No API keys: CI has
none.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import pytest
from conftest import FakeClient, Sent
from pubmed_evidence import cli, config, graphio, ground, label, llm, records, uberon
from pubmed_evidence.cache import LabelCache
from toy import ABL1, CML, EDGES, IMATINIB, NODES, UBERON_OBO, UBERON_VERSION

from agents.augmentation import cli as stage
from shared import nodenorm
from shared.pubmed import EUtils, default_cache_path
from shared.pubmed import eutils as eutils_module
from shared.pubmed import pairs as pairs_module
from shared.schema import AugmentedEdge, Edge

ABSTRACTS = {
    "100": "Imatinib induced durable complete cytogenetic responses in patients with chronic "
    "myeloid leukemia.",
    "101": "We conclude that imatinib is not an effective treatment for chronic myeloid leukemia.",
    "102": "Imatinib did not improve survival in older patients with chronic myeloid leukemia.",
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
                ids = ["100", "101", "102"]
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


def fake_model(calls: list[str]) -> FakeClient:
    """Answers label requests (entities X and Y, abstracts A1..) and review requests (claims
    R1..), from the request text alone, and notes which kind each call was in `calls`."""

    def answer(sent: Sent):
        user = sent.user
        items = re.findall(r"^### (R\d+): PMID:\d+$", user, re.M)
        calls.append("review" if items else "label")
        if items:
            return {"reviews": [{"item": i, "verdict": "upheld", "reason": "ok"} for i in items]}
        side = {node: x for x, node in re.findall(r"^([XY]): .* \((\S+)\)$", user, re.M)}
        pmids = {pmid: a for a, pmid in re.findall(r"^## (A\d+) \(PMID:(\d+)", user, re.M)}

        def statement(predicate: str, **qualifiers) -> dict:
            qualified = "causes" if qualifiers else None
            return {
                "subject": side[IMATINIB],
                "predicate": predicate,
                "qualified_predicate": qualified,
                "qualifiers": qualifiers,
            }

        def claim(
            statement: dict, sentence: str, polarity="positive", finding=None, also=()
        ) -> dict:
            return {
                "polarity": polarity,
                "statement": statement,
                "also_supports": list(also),
                "claim_role": "primary_finding",
                "hedged": False,
                "negative_finding_type": finding,
                "context": {"species": "human", "anatomy": None, "notes": None},
                "evidence_sentence": sentence,
            }

        claims = {
            "100": [
                claim(
                    statement("treats"), "imatinib induced durable complete cytogenetic responses"
                )
            ],
            "101": [
                claim(
                    statement("treats"),
                    "imatinib is not an effective treatment for chronic myeloid leukemia",
                    "negative",
                    "demonstrated_absence",
                    [statement("in_clinical_trials_for")],
                )
            ],
            "102": [
                claim(
                    statement("treats"),
                    "Imatinib did not improve survival in older patients",
                    "negative",
                    "failed_to_detect",
                    [statement("in_clinical_trials_for")],
                )
            ],
            "200": [
                claim(
                    statement(
                        "affects",
                        object_aspect_qualifier="activity",
                        object_direction_qualifier="decreased",
                    ),
                    "Imatinib potently inhibited ABL1 kinase activity",
                )
            ],
        }
        abstracts = [
            {"abstract": a, "study_design": "rct", "claims": claims.get(pmid, [])}
            for pmid, a in pmids.items()
        ]
        return {"abstracts": abstracts}

    return FakeClient(answer)


@pytest.fixture
def fakes(monkeypatch):
    """Route NCBI, NodeNorm, the Name Resolver, UBERON's download and the models to fakes;
    return the list of calls they saw."""
    calls: list[str] = []

    def eutils(*args, **kw):
        client = httpx.Client(transport=httpx.MockTransport(fake_ncbi(calls)))
        return EUtils(client, email="test@example.org", rate=0, sleep=lambda s: None)

    def name_lookup(names, **kw):
        calls.append("nameres")
        return {}

    def fetch_uberon(path, *args, **kw):
        calls.append("uberon")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(UBERON_OBO)

    monkeypatch.setattr(pairs_module, "EUtils", eutils)
    monkeypatch.setattr(nodenorm, "normalize", lambda curies, **kw: {})
    monkeypatch.setattr(ground, "name_lookup", name_lookup)
    monkeypatch.setattr(uberon, "fetch", fetch_uberon)
    monkeypatch.setattr(llm, "client_for", lambda model, **kw: fake_model(calls))
    for name in (config.AZURE_KEY_VAR, eutils_module.NCBI_API_KEY, eutils_module.NCBI_EMAIL):
        monkeypatch.delenv(name, raising=False)
    return calls


def edges_by_statement(path: Path) -> dict[tuple, list[Edge]]:
    out: dict[tuple, list[Edge]] = {}
    for e in graphio.read_edges(path):
        key = (e.subject, e.predicate, e.object, graphio.is_negated(e))
        out.setdefault(key, []).append(e)
    return out


def claims_of(cache_dir: Path, work: Path) -> dict[tuple[str, str], list[dict]]:
    """The claims of the run label_run.json names, by (pair, PMID)."""
    run = records.read_label_run(work)
    with LabelCache(cache_dir / config.LABEL_CACHE) as cache:
        rows = cache.abstract_claims(run.prompt_version, run.model)
        checks = cache.claim_checks(run.prompt_version, run.model)
    assert checks and all(c.valid and c.sentence_valid for c in checks)
    return {(r.pair_id, r.pmid): r.claims for r in rows}


def test_main_with_no_arguments_runs_every_step(tmp_path, monkeypatch, fakes):
    graphio.write_jsonl(tmp_path / "data" / "filter" / "nodes.jsonl", NODES)
    graphio.write_jsonl(tmp_path / "data" / "filter" / "edges.jsonl", EDGES)
    monkeypatch.chdir(tmp_path)

    # Through the stage's entry point, exactly as the pipeline calls it.
    assert stage.main([]) == 0

    out = tmp_path / config.DEFAULT_OUTPUT
    cache_dir = tmp_path / config.DEFAULT_CACHE_DIR
    assert (cache_dir / config.LABEL_CACHE).exists()
    assert not (out / "work" / "sentences.jsonl").exists()  # no sentences step any more

    # Step 3 read each pair's abstracts once, for every edge between the pair.
    assert fakes.count("label") == 2
    claims = claims_of(cache_dir, out / "work")
    treats = graphio.pair_id(IMATINIB, CML)
    assert {pmid for pair, pmid in claims if pair == treats} == {"100", "101", "102", "11287972"}
    assert [c["polarity"] for c in claims[(treats, "101")]] == ["negative"]
    assert claims[(treats, "11287972")] == []  # the source's own citation: no claim
    [inhibits] = claims[(graphio.pair_id(IMATINIB, ABL1), "200")]
    assert inhibits["statement"] == {
        "subject": IMATINIB,
        "predicate": "biolink:affects",
        "object": ABL1,
        "qualified_predicate": "biolink:causes",
        "qualifiers": {
            "object_aspect_qualifier": "activity",
            "object_direction_qualifier": "decreased",
        },
    }

    # Step 5 reviewed the claims that would count, one call per pair, and the run names the
    # review.
    assert fakes.count("review") == 2
    run = records.read_label_run(out / "work")
    with LabelCache(cache_dir / config.LABEL_CACHE) as cache:
        reviews = cache.claim_reviews(run.prompt_version, run.model, run.reviewer)
    assert {(r.pair_id, r.pmid, r.verdict) for r in reviews} == {
        (treats, "100", "upheld"),
        (graphio.pair_id(IMATINIB, ABL1), "200", "upheld"),
    }
    assert (run.reviewer, run.review_reasoning_effort) == (config.REVIEW_MODEL, "medium")
    assert {r.review_prompt_version for r in reviews} == {run.review_prompt_version}

    # Step 6 refined every edge in scope from its pair's claims, and kept every input edge as it
    # was read, with an id. No edge is added: without their species, "imatinib treats CML in
    # human" and the qualified edge in human are the edges they refine.
    edges = list(graphio.read_jsonl(out / "edges.jsonl"))
    assert len(edges) == len(EDGES)
    for before, after in zip(EDGES, edges, strict=True):
        record = json.loads(before.model_dump_json(exclude_none=True))
        cited = record.pop("publications", [])
        assert {k: after[k] for k in record} == record and after["id"]
        assert after.get("publications", []) == cited + after.get("lit_added_publications", [])
    # PMID 100 supports both treats edges, and is added to their publications; the DrugCentral
    # edge keeps its own citation first.
    first, second = edges[:2]
    assert first["publications"] == ["PMID:11287972", "PMID:100"]
    assert second["publications"] == ["PMID:100"]
    for e in (first, second):
        assert (e["augmentation"], e["lit_support_score"]) == ("checked", 1)
        assert e["lit_supporting_publications"] == e["lit_added_publications"] == ["PMID:100"]
        assert e["lit_refined_by"] == []
        assert supports(e) == {
            "PMID:100": [True],
            "PMID:101": [False],
            "PMID:102": [False],
            "PMID:11287972": [],
        }
    # The source's own citation is `source` on the edge that cites it.
    assert found_by(first)["PMID:11287972"] == ["source"]
    assert found_by(second)["PMID:11287972"] == []
    # PMID 200 supports the qualified imatinib-ABL1 edge, and not the
    # directly_physically_interacts_with one: nothing is held.
    interacts, qualified = edges[2:4]
    assert (qualified["augmentation"], qualified["lit_support_score"]) == ("checked", 1)
    assert qualified["publications"] == qualified["lit_added_publications"] == ["PMID:200"]
    assert supports(qualified) == {"PMID:200": [True], "PMID:10592173": []}
    assert (interacts["augmentation"], interacts["lit_support_score"]) == ("checked", 0)
    assert interacts["publications"] == ["PMID:10592173"]
    assert supports(interacts) == {"PMID:200": [False], "PMID:10592173": []}
    assert found_by(interacts)["PMID:10592173"] == ["source"]
    # Every other edge in scope, the negated one among them, is checked and unsupported; the
    # skipped tier passes through.
    checked = [e for e in edges if e.get("augmentation") == "checked"]
    assert len(checked) == 9 and checked[-1]["negated"] is True
    assert [e["lit_support_score"] for e in checked] == [1, 1, 0, 1, 0, 0, 0, 0, 0]
    assert [e.get("augmentation") for e in edges[8:11]] == [None, None, None]
    for e in checked:
        AugmentedEdge.model_validate(e)
    lines = list(graphio.read_jsonl(out / "work" / "refinements.jsonl"))
    assert {x["edge_id"] for line in lines for x in line["edges"]} == {e["id"] for e in checked}
    report = json.loads((out / "work" / "apply_report.json").read_text())
    assert (report["edges_checked"], report["edges_supported"]) == (9, 3)
    assert report["publications_added"] == 3
    assert report["proposed"] == {"edges": 0, "links": 0, "species_folded": 2}
    assert report["held"] == {"pairs_unreviewed": 0, "pairs_anatomy_not_looked_up": 0, "edges": 0}
    assert report["uberon_version"] == UBERON_VERSION and report["edges_written"] == 12

    # A rerun is served from the caches and writes the same graph.
    first = (out / "edges.jsonl").read_bytes()
    seen = len(fakes)
    assert cli.main([]) == 0
    assert len(fakes) == seen
    assert (out / "edges.jsonl").read_bytes() == first

    # The ground step pinned UBERON's release in the cache, once, and the run names it.
    assert (cache_dir / config.UBERON_FILE).exists()
    assert fakes.count("uberon") == 1
    assert records.read_label_run(out / "work").uberon_version == UBERON_VERSION

    # With species in proposals, "imatinib treats CML in human" refines both treats edges, and
    # the qualified edge in human refines the qualified edge.
    species = tmp_path / "species"
    argv = ["--output", str(species), "--work", str(out / "work"), "apply"]
    assert cli.main([*argv, "--species-in-proposals"]) == 0
    added = list(graphio.read_jsonl(species / "edges.jsonl"))[len(EDGES) :]
    proposed = {e["predicate"]: e for e in added}
    assert sorted(proposed) == ["biolink:affects", "biolink:treats"]
    assert {e["species_context_qualifier"] for e in added} == {"NCBITaxon:9606"}
    assert proposed["biolink:treats"]["original_edge_ids"] == [edges[0]["id"], edges[1]["id"]]
    assert proposed["biolink:treats"]["publications"] == ["PMID:100"]
    affects = proposed["biolink:affects"]
    assert {k: affects[k] for k in ("qualified_predicate", *QUALIFIERS)} == {
        k: qualified[k] for k in ("qualified_predicate", *QUALIFIERS)
    }
    assert affects["original_edge_ids"] == [qualified["id"]]
    assert affects["publications"] == ["PMID:200"]
    assert {e["primary_knowledge_source"] for e in added} == {config.INFORES}


QUALIFIERS = ("object_aspect_qualifier", "object_direction_qualifier")


def found_by(edge: dict) -> dict[str, list[str]]:
    return {e["publication"]: e["found_by"] for e in edge["lit_evidence"]}


def supports(edge: dict) -> dict[str, list[bool]]:
    return {e["publication"]: [c["supports"] for c in e["claims"]] for e in edge["lit_evidence"]}


def test_skip_review_adds_no_new_edges(tmp_path, fakes):
    graph = tmp_path / "filter"
    graphio.write_jsonl(graph / "nodes.jsonl", NODES)
    graphio.write_jsonl(graph / "edges.jsonl", EDGES)
    argv = ["--input", str(graph), "--output", str(tmp_path / "out")]
    argv += ["--cache-dir", str(tmp_path / "cache"), "--skip-review"]

    assert cli.main(argv) == 0
    assert "review" not in fakes
    # Both pairs with claims that would count are held, their edges written as read.
    edges = list(graphio.read_jsonl(tmp_path / "out" / "edges.jsonl"))
    assert len(edges) == len(EDGES)
    for before, after in zip(EDGES[:4], edges[:4], strict=True):
        assert after == {**json.loads(before.model_dump_json(exclude_none=True)), "id": after["id"]}
    report = json.loads((tmp_path / "out" / "work" / "apply_report.json").read_text())
    assert report["held"] == {"pairs_unreviewed": 2, "pairs_anatomy_not_looked_up": 0, "edges": 4}
    assert report["proposed"]["edges"] == report["edges_supported"] == 0


def test_steps_run_alone_with_options_before_or_after(tmp_path, fakes):
    graph = tmp_path / "filter"
    graphio.write_jsonl(graph / "nodes.jsonl", NODES)
    graphio.write_jsonl(graph / "edges.jsonl", EDGES)
    common = ["--input", str(graph), "--output", str(tmp_path / "out")]
    common += ["--cache-dir", str(tmp_path / "cache")]
    assert cli.main([*common, "select"]) == 0
    assert cli.main(["pubmed", *common, "--no-nodenorm"]) == 0
    assert (tmp_path / "out" / "work" / "pubmed.jsonl").exists()
    assert default_cache_path().exists()
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
    for argv in (
        ["--model", "gpt-6-luna", "label"],
        ["--reasoning-effort", "medium", "label"],
        ["--limit", "3", "pubmed"],
    ):
        with pytest.raises(SystemExit) as exit:
            cli.main([*common_args(tmp_path), *argv])
        assert exit.value.code == 2
    assert fakes == []


def test_sentences_is_no_longer_a_step(tmp_path, fakes):
    assert "sentences" not in cli.MODULES and "sentences" not in cli.FULL_RUN
    with pytest.raises(SystemExit) as exit:
        cli.main([*common_args(tmp_path), "sentences"])
    assert exit.value.code == 2


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

    # The pipeline's full run reuses that run's claims instead of reading the abstracts again
    # with the defaults, and reviews them. After that, a rerun makes no calls at all.
    labels, reviews = fakes.count("label"), fakes.count("review")
    assert cli.main(common) == 0
    assert fakes.count("label") == labels and fakes.count("review") > reviews
    run = records.read_label_run(tmp_path / "out" / "work")
    assert (run.model, run.reviewer) == ("gpt-oss-120b", config.REVIEW_MODEL)
    seen = len(fakes)
    assert cli.main(common) == 0
    assert len(fakes) == seen


def test_full_run_continues_the_runs_effort(tmp_path, fakes, monkeypatch):
    efforts: list[str | None] = []

    def client_for(model, **kw):
        if model.id == config.BULK_MODEL:
            efforts.append(kw.get("reasoning_effort"))
        return fake_model(fakes)

    monkeypatch.setattr(llm, "client_for", client_for)
    common = common_args(tmp_path)
    work = tmp_path / "out" / "work"
    assert cli.main(common) == 0
    assert records.read_label_run(work).reasoning_effort == "low"

    # A run at another effort labels afresh; the full run then continues it, without calls.
    assert cli.main([*common, "label", "--reasoning-effort", "medium"]) == 0
    assert efforts[-1] == "medium" and records.read_label_run(work).reasoning_effort == "medium"
    labels = fakes.count("label")
    assert cli.main(common) == 0
    assert fakes.count("label") == labels
    assert records.read_label_run(work).reasoning_effort == "medium"

    # The full run's own option overrides it.
    assert cli.main(["--reasoning-effort", "high", *common]) == 0
    assert efforts[-1] == "high" and fakes.count("label") > labels
    assert records.read_label_run(work).reasoning_effort == "high"


def test_full_run_continues_the_reviews_effort(tmp_path, fakes, monkeypatch):
    efforts: list[str | None] = []

    def client_for(model, **kw):
        if model.id == config.REVIEW_MODEL:
            efforts.append(kw.get("reasoning_effort"))
        return fake_model(fakes)

    monkeypatch.setattr(llm, "client_for", client_for)
    common = common_args(tmp_path)
    work = tmp_path / "out" / "work"
    assert cli.main(common) == 0
    assert efforts == [None] and records.read_label_run(work).review_reasoning_effort == "medium"

    # A review at another effort reviews afresh; the full run then continues it, without calls.
    assert cli.main([*common, "review", "--reasoning-effort", "high"]) == 0
    run = records.read_label_run(work)
    assert efforts[-1] == "high" and run.review_reasoning_effort == "high"
    reviews = fakes.count("review")
    assert cli.main(common) == 0
    assert fakes.count("review") == reviews
    assert records.read_label_run(work).review_prompt_version == run.review_prompt_version

    # Another reviewer starts from its own default.
    assert cli.main([*common, "--review-model", "gpt-6.1-sol"]) == 0
    run = records.read_label_run(work)
    assert (run.reviewer, run.review_reasoning_effort) == ("gpt-6.1-sol", "medium")


def test_affordable_counts_the_first_pairs_that_fit():
    assert cli.affordable([1.0, 0.0, 2.0, 1.0], 3.0) == 3
    assert cli.affordable([1.0, 2.0], 3.0) == 2
    assert cli.affordable([4.0, 0.0], 3.0) == 0
    assert cli.affordable([], 0.0) == 0


def test_full_run_labels_only_what_the_budget_allows(tmp_path, capsys, fakes):
    graph = tmp_path / "filter"
    graphio.write_jsonl(graph / "nodes.jsonl", NODES)
    graphio.write_jsonl(graph / "edges.jsonl", EDGES)
    argv = ["--input", str(graph), "--output", str(tmp_path / "out")]
    argv += ["--cache-dir", str(tmp_path / "cache")]

    # Estimate as the full run does, then give it a budget only the first pair fits.
    assert cli.main([*argv, "select"]) == 0 and cli.main([*argv, "pubmed"]) == 0
    work = tmp_path / "out" / "work"
    estimate = label.label_pairs(work, tmp_path / "cache", dry_run=True)
    costs = estimate["estimated_cost_by_pair"]
    assert len(costs) == 2 and all(c > 0 for c in costs)
    capsys.readouterr()

    # The run flags it, labels the first pair alone, and still writes the graph.
    assert cli.main([*argv, "--max-label-cost", str(costs[0])]) == 0
    assert fakes.count("label") == 1
    assert "labeling the first 1 of 2 pairs" in capsys.readouterr().err
    assert len(list(graphio.read_jsonl(tmp_path / "out" / "edges.jsonl"))) >= len(EDGES)

    # The default budget covers this toy graph: the rerun labels the other pair, and only it.
    assert cli.main(argv) == 0
    assert fakes.count("label") == 2
    assert "budget" not in capsys.readouterr().err


def test_script_header_lists_the_projects_dependencies():
    """`scripts/augment.py` runs alone under `uv run --script`, so its PEP 723 header repeats
    the project's dependencies and the `augmentation` extra; they must not drift apart."""
    import tomllib

    root = Path(__file__).resolve().parents[5]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    wanted = project["dependencies"] + project["optional-dependencies"]["augmentation"]
    script = (Path(__file__).resolve().parents[1] / "scripts" / "augment.py").read_text()
    header = re.search(r"^# /// script\n(.*?)^# ///$", script, re.M | re.S)
    assert header is not None
    meta = tomllib.loads("\n".join(line[2:] for line in header[1].splitlines()))
    assert sorted(meta["dependencies"]) == sorted(wanted)


def test_one_bad_edge_does_not_stop_the_run(tmp_path, capsys, fakes):
    """A bad edge among good ones: every step runs, select skips it, apply writes it as read,
    both reports count it, and the run warns instead of failing."""
    graph = tmp_path / "filter"
    bad = {"subject": IMATINIB, "predicate": "treats", "object": CML}  # no biolink:, no source
    graphio.write_jsonl(graph / "nodes.jsonl", NODES)
    graphio.write_jsonl(graph / "edges.jsonl", [*EDGES, bad])
    out = tmp_path / "out"
    argv = ["--input", str(graph), "--output", str(out), "--cache-dir", str(tmp_path / "cache")]

    assert cli.main(argv) == 0
    err = capsys.readouterr().err
    assert "select: warning: 1 edges" in err and "apply: warning: 1 edges" in err
    edges = list(graphio.read_jsonl(out / "edges.jsonl"))
    assert bad in edges
    for report in ("scope_report.json", "apply_report.json"):
        assert json.loads((out / "work" / report).read_text())["invalid_edges"]["edges"] == 1
