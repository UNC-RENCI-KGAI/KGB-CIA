#!/usr/bin/env python3
"""Measure how often the evidence check is right: draw edge-article pairs for a person to label,
then score the labels against the judge.

    label_sample.py QUESTION [--size 50] [--seed 0]   # writes label_sample.tsv
    label_sample.py QUESTION --score                   # reads it back once labelled

The sample is drawn from verdicts.jsonl (run build_drop_list.py first), stratified by verdict:
every flagged and dropped edge up to half the sample, since there are few and they are the calls
that change the graph, and the rest split evenly between keep and unconfirmed. Each row is one
edge with one of its articles, in shuffled order: the claim, both nodes with their other names,
the PubMed link, and the title and abstract. The labelling is blind: the judge's answers go to
label_key.tsv, not the sheet. The person fills `label` with the role the abstract has for the
edge (supports, contradicts or silent; for a flagging question, the flag name when the edge is
real but a detail is wrong) and may add `notes`. A spreadsheet opens the file as it is.

--score joins the labels to the key by row and prints, per verdict, how many agree with the
judge's role for that article, and writes label_score.json beside the sample. Standard library
only.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import question  # noqa: E402

COLUMNS = ("row", "claim", "subject", "object", "pmid", "url", "label", "notes", "text")
KEY_COLUMNS = ("row", "evidence_check", "judge_choice", "judge_role")
VERDICTS = ("flag", "drop", "keep", "unconfirmed")


def judge_role(q: dict, choice: str) -> str:
    """The role the judge's top choice gives the article: its option's role, or its flag."""
    option = next(o for o in q["options"] if o["name"] == choice)
    return option.get("flag") or option["role"]


def draw(verdicts: list[dict], size: int, seed: int) -> list[dict]:
    """Edges stratified by verdict: flags and drops first (up to half), the rest evenly split
    between keep and unconfirmed, any shortfall filled from the other."""
    rng = random.Random(seed)
    by_verdict = defaultdict(list)
    for v in verdicts:
        by_verdict[v["verdict"]].append(v)
    for group in by_verdict.values():
        rng.shuffle(group)
    rare = (by_verdict["flag"] + by_verdict["drop"])[: size // 2]
    rest = size - len(rare)
    keep, unconfirmed = by_verdict["keep"], by_verdict["unconfirmed"]
    n_keep = min(len(keep), max(rest // 2, rest - len(unconfirmed)))
    return rare + keep[:n_keep] + unconfirmed[: rest - n_keep]


def write_sample(q: dict, work: Path, size: int, seed: int) -> int:
    verdicts_path = work / "verdicts.jsonl"
    if not verdicts_path.exists():
        print(f"No {verdicts_path}; run build_drop_list.py first.")
        return 1
    verdicts = [json.loads(line) for line in open(verdicts_path)]
    evidence = {}
    with open(work / "evidence.jsonl") as f:
        for e in map(json.loads, f):
            evidence[(tuple(e["key"]), e["pmid"])] = e
    rng = random.Random(seed)
    drawn = draw(verdicts, size, seed)
    rng.shuffle(drawn)  # so the order doesn't give the verdicts away
    out_path, key_path = work / "label_sample.tsv", work / "label_key.tsv"
    counts = Counter()
    with open(out_path, "w", newline="") as f, open(key_path, "w", newline="") as k:
        writer = csv.DictWriter(f, COLUMNS, delimiter="\t", lineterminator="\n")
        key = csv.DictWriter(k, KEY_COLUMNS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        key.writeheader()
        for n, v in enumerate(drawn, 1):
            article = rng.choice(v["articles"])  # one article per edge, so each row is quick
            e = evidence[(tuple(v["key"]), article["pmid"])]
            counts[v["verdict"]] += 1
            writer.writerow(
                {
                    "row": n,
                    "claim": v["claim"],
                    "subject": e.get("subject", v["key"][0]),
                    "object": e.get("object", v["key"][2]),
                    "pmid": article["pmid"],
                    "url": f"https://pubmed.ncbi.nlm.nih.gov/{article['pmid']}/",
                    "label": "",
                    "notes": "",
                    "text": " ".join(e.get("text", "").split()),
                }
            )
            key.writerow(
                {
                    "row": n,
                    "evidence_check": v["verdict"] + (f":{v['flag']}" if v["flag"] else ""),
                    "judge_choice": article["choice"],
                    "judge_role": judge_role(q, article["choice"]),
                }
            )
    labels = ", ".join(sorted({judge_role(q, o["name"]) for o in q["options"]}))
    print(
        f"Wrote {out_path} for labelling and the judge's answers to {key_path}: "
        f"{sum(counts.values())} edges ("
        + ", ".join(f"{v} {counts[v]}" for v in VERDICTS if counts[v])
        + f"). Fill `label` with one of: {labels}; then rerun with --score."
    )
    return 0


def score(work: Path) -> int:
    path = work / "label_sample.tsv"
    if not path.exists():
        print(f"No {path}; draw a sample first.")
        return 1
    with open(work / "label_key.tsv") as f:
        key = {r["row"]: r for r in csv.DictReader(f, delimiter="\t")}
    with open(path) as f:
        rows = [
            {**key[r["row"]], **r}
            for r in csv.DictReader(f, delimiter="\t")
            if (r.get("label") or "").strip()
        ]
    if not rows:
        print(f"No row of {path} has a label yet.")
        return 1
    result = {}
    for verdict in VERDICTS:
        group = [r for r in rows if r["evidence_check"].split(":")[0] == verdict]
        if group:
            agree = sum(r["label"].strip().lower() == r["judge_role"] for r in group)
            result[verdict] = {"labelled": len(group), "agree": agree}
    agree = sum(g["agree"] for g in result.values())
    result["all"] = {"labelled": len(rows), "agree": agree}
    for verdict, g in result.items():
        print(f"  {verdict}: {g['agree']}/{g['labelled']} agree ({g['agree'] / g['labelled']:.0%})")
    (work / "label_score.json").write_text(json.dumps(result, indent=2) + "\n")
    print(f"Wrote {work / 'label_score.json'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("question", type=Path, help="question file (reference/question-format.md)")
    parser.add_argument("--size", type=int, default=50, help="edges to draw (default 50)")
    parser.add_argument("--seed", type=int, default=0, help="random seed for the draw (default 0)")
    parser.add_argument("--score", action="store_true", help="score a labelled sample")
    args = parser.parse_args(argv)

    q = question.load(args.question)
    work = question.work_dir(q)
    return score(work) if args.score else write_sample(q, work, args.size, args.seed)


if __name__ == "__main__":
    raise SystemExit(main())
