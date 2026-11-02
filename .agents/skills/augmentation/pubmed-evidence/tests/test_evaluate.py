"""Scoring cached labels against a gold set, with the metrics checked by hand.

Gold (8 pairs) and run A's labels (`luna@label@aaaa`), rows gold, columns label:

| # | statement, PMID | gold | A's label | A's quote |
| - | --- | --- | --- | --- |
| 1 | s1, 101 | confirmed | confirmed | valid |
| 2 | s1, 102 | confirmed | confirmed | invalid |
| 3 | s1, 103 | contradicted | contradicted | valid |
| 4 | s1, 104 | no_evidence | no_evidence | |
| 5 | s2, 101 | no_evidence | confirmed | valid |
| 6 | s2, 102 | ambiguous | ambiguous | |
| 7 | s2, 105 | confirmed | (none) | |
| 8 | s2, 106 | no_evidence | no_evidence | |

As labeled, over the 7 covered: 6 right. Gold totals (conf, contra, amb, none) 2, 1, 1, 3;
label totals 3, 1, 1, 2; chance agreement (6 + 1 + 1 + 6) / 49 = 2/7, so kappa
(6/7 - 2/7) / (5/7) = 0.8. Quote-checked, #2 becomes no_evidence: 5 right, label totals
2, 1, 1, 3, chance (4 + 1 + 1 + 9) / 49 = 15/49, kappa (35 - 15) / (49 - 15) = 10/17.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
from pubmed_evidence import config, evaluate, records
from pubmed_evidence.cache import CallRow, LabelCache, LabelRow
from pubmed_evidence.records import LabelRun

GOLD = [
    ("s1", "101", "confirmed"),
    ("s1", "102", "confirmed"),
    ("s1", "103", "contradicted"),
    ("s1", "104", "no_evidence"),
    ("s2", "101", "no_evidence"),
    ("s2", "102", "ambiguous"),
    ("s2", "105", "confirmed"),
    ("s2", "106", "no_evidence"),
]
A = ("luna", "label@aaaa")
A_LABELS = [
    ("s1", "101", "confirmed", True),
    ("s1", "102", "confirmed", False),
    ("s1", "103", "contradicted", True),
    ("s1", "104", "no_evidence", None),
    ("s2", "101", "confirmed", True),
    ("s2", "102", "ambiguous", None),
    ("s2", "106", "no_evidence", None),
    ("s9", "999", "confirmed", True),  # not in the gold set
]


def _write_gold(path: Path, items=GOLD, **extra) -> Path:
    lines = [{"statement_id": s, "pmid": p, "gold_label": g, **extra} for s, p, g in items]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def _put(cache: LabelCache, model: str, version: str, labels, created: str) -> None:
    rows = [
        LabelRow(s, p, version, model, label, "q" if valid is not None else None, valid)
        for s, p, label, valid in labels
    ]
    for row in rows:
        row.created = created
    cache.put_labels(rows)


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "cache"
    perfect = [(s, p, g, True if g in ("confirmed", "contradicted") else None) for s, p, g in GOLD]
    with LabelCache(directory / config.LABEL_CACHE) as cache:
        _put(cache, *A, A_LABELS, "2026-11-01T00:00:00+00:00")
        # terra has three prompt versions: b1 labels the whole gold set right, b2 half of it,
        # b3 only something else.
        _put(cache, "terra", "label@b2", perfect[:4], "2026-11-02T00:00:00+00:00")
        _put(cache, "terra", "label@b1", perfect, "2026-11-01T00:00:00+00:00")
        _put(cache, "terra", "label@b3", [("s9", "999", "confirmed", True)], "2026-11-03")
        calls = [("p1", 0.01, None), ("p1", 0.02, None), ("p2", 0.03, None), ("p1", None, "500")]
        for pair, cost, error in calls:
            cache.log_call(CallRow("label", pair, *A, 9000, 1000, cost=cost, error=error))
        cache.log_call(CallRow("review", "p1", *A, 5000, 500, cost=1.0))  # another step
    return directory


def test_metrics_by_hand(tmp_path, cache_dir):
    [r] = evaluate.evaluate(_write_gold(tmp_path / "gold.jsonl"), cache_dir, ["luna@label@aaaa"])
    assert (r["model"], r["prompt_version"]) == A
    assert (r["gold"], r["covered"]) == (8, 7) and r["coverage"] == pytest.approx(7 / 8)

    m = r["as_labeled"]
    assert m["accuracy"] == pytest.approx(6 / 7)
    assert m["kappa"] == pytest.approx(0.8)
    assert m["confusion"] == [[2, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [1, 0, 0, 2]]
    confirmed, none = m["per_label"]["confirmed"], m["per_label"]["no_evidence"]
    assert (confirmed["precision"], confirmed["recall"]) == (pytest.approx(2 / 3), 1.0)
    assert confirmed["f1"] == pytest.approx(0.8)
    assert (none["precision"], none["recall"]) == (1.0, pytest.approx(2 / 3))
    assert none["f1"] == pytest.approx(0.8)
    assert m["per_label"]["contradicted"]["f1"] == 1.0
    assert m["macro_f1"] == pytest.approx(0.9)

    # Decisive labels: #1, #2, #3, #5; #2's quote is invalid.
    assert (r["decisive"], r["invalid_quotes"]) == (4, 1)
    assert r["invalid_quote_share"] == pytest.approx(0.25)
    q = r["quote_checked"]
    assert q["accuracy"] == pytest.approx(5 / 7)
    assert q["kappa"] == pytest.approx(10 / 17)
    assert q["confusion"] == [[1, 0, 0, 1], [0, 1, 0, 0], [0, 0, 1, 0], [1, 0, 0, 2]]
    assert q["per_label"]["confirmed"]["recall"] == pytest.approx(0.5)

    assert {(d["statement_id"], d["pmid"]) for d in r["disagreements"]} == {
        ("s1", "102"),
        ("s2", "101"),
    }


def test_cost_from_logged_label_calls(tmp_path, cache_dir):
    gold = _write_gold(tmp_path / "gold.jsonl")
    [a] = evaluate.evaluate(gold, cache_dir, [A])
    assert a["usage"]["calls"] == 4 and a["usage"]["errors"] == 1 and a["usage"]["pairs"] == 2
    assert a["cost_per_call"] == pytest.approx(0.02)  # $0.06 over the 3 calls that worked
    assert a["cost_per_pair"] == pytest.approx(0.03)
    assert a["cost_per_10k_pairs"] == pytest.approx(300.0)
    assert a["input_tokens_per_pair"] == 18000

    [b] = evaluate.evaluate(gold, cache_dir, ["terra@label@b1"])
    assert b["cost_per_call"] is None and b["cost_per_10k_pairs"] is None


def test_model_alone_scores_each_prompt_version(tmp_path, cache_dir):
    results = evaluate.evaluate(_write_gold(tmp_path / "gold.jsonl"), cache_dir, ["terra"])
    # Oldest first; b3 has no labels on the gold set and is skipped.
    assert [r["prompt_version"] for r in results] == ["label@b1", "label@b2"]
    b1, b2 = results
    assert b1["covered"] == 8 and b1["as_labeled"]["accuracy"] == 1.0
    assert b1["as_labeled"]["kappa"] == pytest.approx(1.0)
    assert b2["covered"] == 4 and b2["coverage"] == 0.5

    # Given with its version, a run is scored even with no gold labels.
    [b3] = evaluate.evaluate(tmp_path / "gold.jsonl", cache_dir, ["terra@label@b3"])
    assert b3["covered"] == 0 and b3["as_labeled"]["accuracy"] is None


def test_label_sample_lines_are_accepted(tmp_path, cache_dir):
    path = tmp_path / "label_sample.jsonl"
    sample = {"pair_id": "a|b", "sentence": "x", "label": "confirmed", "quote": "q"}
    lines = [
        {"statement_id": "s1", "pmid": "101", "gold_label": "confirmed"},
        {"statement_id": "s1", "pmid": "PMID:102", "gold_label": None},
        {"statement_id": "s2", "pmid": 101, "gold_label": "no evidence"},
        {"statement_id": "s2", "pmid": "106", "gold_label": "wrong"},
    ]
    lines = [sample | line for line in lines]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    gold = evaluate.read_gold(path)
    assert [(g.statement_id, g.pmid, g.gold_label) for g in gold.items] == [
        ("s1", "101", "confirmed"),
        ("s2", "101", "no_evidence"),
    ]
    assert (gold.unlabeled, gold.invalid) == (1, 1)
    [r] = evaluate.evaluate(gold, cache_dir, [A])
    assert r["covered"] == 2 and r["as_labeled"]["accuracy"] == 0.5


def _args(work: Path, cache_dir: Path, *argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    evaluate.add_arguments(parser)
    args = parser.parse_args(list(argv))
    args.work, args.cache_dir = work, cache_dir
    return args


def test_run_writes_report_for_label_run(tmp_path, cache_dir, capsys):
    work = tmp_path / "work"
    records.write_label_run(work, LabelRun(model=A[0], prompt_version=A[1]))
    _write_gold(work / evaluate.GOLD)
    assert evaluate.run(_args(work, cache_dir)) == 0

    text = (work / evaluate.REPORT).read_text()
    assert text.startswith("# Label evaluation")
    assert text.index("## Comparison") < text.index("## `luna@label@aaaa`")
    assert "| `luna@label@aaaa` | 7/8 | 0.857 | 0.800 | 0.714 | 0.588 |" in text
    assert "$300.00" in text and "### Quote-checked" in text and "### Disagreements" in text
    out = capsys.readouterr().out
    assert "| `luna@label@aaaa` |" in out and "evaluate: wrote" in out

    # Several runs compare in one table.
    assert evaluate.run(_args(work, cache_dir, "--run", "luna", "--run", "terra")) == 0
    table = (work / evaluate.REPORT).read_text().split("## Comparison")[1].split("##")[0]
    assert table.count("| `") == 3


def test_run_fails_without_gold_or_labels(tmp_path, cache_dir):
    work = tmp_path / "work"
    records.write_label_run(work, LabelRun(model=A[0], prompt_version=A[1]))
    assert evaluate.run(_args(work, cache_dir)) == 1  # no gold file
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n", encoding="utf-8")
    assert evaluate.run(_args(work, cache_dir, "--gold", str(empty))) == 1
    gold = str(_write_gold(tmp_path / "gold.jsonl"))
    assert evaluate.run(_args(work, cache_dir, "--gold", gold, "--run", "nobody")) == 1
    assert evaluate.run(_args(work, cache_dir, "--gold", gold, "--run", "terra@label@b3")) == 1
    assert evaluate.run(_args(work, tmp_path / "nocache", "--gold", gold)) == 1
    assert not (work / evaluate.REPORT).exists()


def test_parse_run():
    assert evaluate.parse_run("gpt-5.6-luna") == ("gpt-5.6-luna", None)
    assert evaluate.parse_run("gpt-5.6-luna@label@1a2b3c4d") == ("gpt-5.6-luna", "label@1a2b3c4d")
