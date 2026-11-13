#!/usr/bin/env python3
"""Step 3: turn Jev's answers into a verdict per edge, and write the drop and flag lists
cleaning reads.

    build_drop_list.py QUESTION

Reads evidence.jsonl and answers.jsonl in data/cleaning/evidence/<name>/ and writes:

- verdicts.jsonl there: one line per edge with its verdict (keep, drop, flag, unconfirmed), its
  flag, the role probabilities for each article, and the claim, so a person can read why;
- data/cleaning/drop_lists/<name>.jsonl: the dropped edges, which kgb-clean then removes, each
  with the judge's confidence, which kgb-clean carries into its pruned_edges.tsv;
- data/cleaning/flag_lists/<name>.jsonl: the flagged edges, each with its flag and a reason
  naming the articles, which kgb-clean writes onto the edge for the MCP interface to show;
- gold_set.tsv beside verdicts.jsonl: every judged edge as a KGX TSV row with a keep or prune
  verdict and a confidence, the form the challenge's pruning gold set takes (see gold_row).

Each drop and flag line names its edge by subject, predicate, object, primary knowledge source
and `statement_id` (`shared.schema.edge_statement_id`), with the edge's qualifiers beside it to
read, so kgb-clean drops or flags only the edge that was judged, not others that differ from it
only in qualifiers. The verdict rules are in question.edge_verdict. Rerun with another threshold
in the question file to try it without asking Jev again. Standard library only.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import question  # noqa: E402

# subject, predicate, object, primary knowledge source, statement id (gather_evidence.py)
KEY_LENGTH = 5
GOLD_COLUMNS = (
    "subject",
    "predicate",
    "object",
    "primary_knowledge_source",
    "publications",
    "verdict",
    "confidence",
    "evidence_check",
    "flag",
    "question",
)


def gold_row(q: dict, record: dict) -> dict:
    """An edge's gold-set row. Only a dropped edge is `prune`; flagged and unconfirmed edges stay
    in the graph, so they are `keep`. The confidence is the judge's probability, from the article
    that says most, for what the verdict rests on:

    - keep: that an article supports the edge;
    - flag: for a wrong detail, that the edge is real (supports plus the flagging options); for
      an unsupported text-mined edge, that an article supports it, which is low;
    - unconfirmed: that an article supports it, also low: kept because nothing contradicts it,
      not because the abstracts confirm it;
    - drop (prune): that an article contradicts it.
    """
    articles = record["articles"]
    verdict, flag = record["verdict"], record["flag"]

    def best(score) -> float:
        return round(max(score(a) for a in articles), 3)

    if verdict == "drop":
        confidence = best(lambda a: a["contradicts"] + sum((a.get("flags") or {}).values()))
    elif verdict == "flag" and flag != question.UNSUPPORTED_FLAG:
        confidence = best(lambda a: a["supports"] + (a.get("flags") or {}).get(flag, 0.0))
    else:
        confidence = best(lambda a: a["supports"])
    subject, predicate, obj, source, _ = record["key"]
    return {
        "subject": subject,
        "predicate": predicate,
        "object": obj,
        "primary_knowledge_source": source,
        "publications": "|".join(f"PMID:{a['pmid']}" for a in articles),
        "verdict": "prune" if verdict == "drop" else "keep",
        "confidence": f"{min(confidence, 1.0):.3f}",
        "evidence_check": verdict,
        "flag": flag or "",
        "question": q["name"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("question", type=Path, help="question file (reference/question-format.md)")
    args = parser.parse_args(argv)

    q = question.load(args.question)
    work = question.work_dir(q)
    answers_path = work / "answers.jsonl"
    if not answers_path.exists():
        print(f"No {answers_path}; run judge_evidence.py first (it needs a TypeSafe key).")
        return 1
    answers = {}
    with open(answers_path) as f:
        for r in map(json.loads, f):
            answers[(tuple(r["key"]), r["pmid"])] = r

    edges: dict[tuple, dict] = {}
    unanswered = 0
    with open(work / "evidence.jsonl") as f:
        for e in map(json.loads, f):
            key = tuple(e["key"])
            if len(key) != KEY_LENGTH:
                print(
                    f"{work / 'evidence.jsonl'} is from before edges were keyed by statement; "
                    "rerun gather_evidence.py and judge_evidence.py."
                )
                return 1
            answer = answers.get((key, e["pmid"]))
            if answer is None:
                unanswered += 1
                continue
            record = edges.setdefault(
                key, {"key": list(key), "statement": e.get("statement", {}), "claim": e["claim"],
                      "based_on_text": e["based_on_text"], "articles": []},
            )  # fmt: skip
            record["articles"].append(
                {
                    "pmid": e["pmid"],
                    "choice": answer["choice"],
                    **question.role_probabilities(q, answer["probabilities"]),
                }
            )

    verdicts = Counter()
    flagged = Counter()
    reasons = question.flag_reasons(q)
    drop_path, flag_path = question.drop_list_path(q), question.flag_list_path(q)
    drop_path.parent.mkdir(parents=True, exist_ok=True)
    flag_path.parent.mkdir(parents=True, exist_ok=True)
    choices = defaultdict(Counter)
    gold_path = work / "gold_set.tsv"
    with (
        open(work / "verdicts.jsonl", "w") as out,
        open(drop_path, "w") as drops,
        open(flag_path, "w") as flags,
        open(gold_path, "w", newline="") as gold_file,
    ):
        gold = csv.DictWriter(gold_file, GOLD_COLUMNS, delimiter="\t", lineterminator="\n")
        gold.writeheader()
        for record in edges.values():
            verdict, flag = question.edge_verdict(q, record["based_on_text"], record["articles"])
            record["verdict"], record["flag"] = verdict, flag
            verdicts[verdict] += 1
            row = gold_row(q, record)
            gold.writerow(row)
            for a in record["articles"]:
                choices[verdict][a["choice"]] += 1
            out.write(json.dumps(record) + "\n")
            subject, predicate, obj, source, statement_id = record["key"]
            key = {
                "subject": subject,
                "predicate": predicate,
                "object": obj,
                "primary_knowledge_source": source,
                "statement_id": statement_id,
                **record["statement"],
            }
            pmids = [a["pmid"] for a in record["articles"]]
            if verdict == "drop":
                line = {
                    **key,
                    "reason": f"evidence check {q['name']}",
                    "pmids": pmids,
                    "confidence": float(row["confidence"]),
                }
                drops.write(json.dumps(line) + "\n")
            elif verdict == "flag":
                flagged[flag] += 1
                cited = ", ".join(f"PMID:{p}" for p in pmids)
                line = {**key, "flag": flag, "reason": f"{reasons[flag]} ({cited})"}
                flags.write(json.dumps(line) + "\n")

    total = sum(verdicts.values())
    print(f"{total:,} edges judged at threshold {q['threshold']}:")
    for verdict in ("keep", "unconfirmed", "flag", "drop"):
        top = ", ".join(f"{c} {n}" for c, n in choices[verdict].most_common(4))
        print(f"  {verdict}: {verdicts[verdict]:,}" + (f"  (answers: {top})" if top else ""))
    if unanswered:
        print(f"{unanswered:,} articles not answered yet; run judge_evidence.py again.")
    if flagged:
        print("  flags: " + ", ".join(f"{f} {n:,}" for f, n in flagged.most_common()))
    print(
        f"Wrote {work / 'verdicts.jsonl'}, {drop_path} ({verdicts['drop']:,} edges) and "
        f"{flag_path} ({verdicts['flag']:,} edges), and the gold set {gold_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
