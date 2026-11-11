#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["typesafe-sdk==0.7.2"]
# ///
"""Step 2: ask Jev the question about every piece of gathered evidence.

    judge_evidence.py QUESTION

Reads data/cleaning/evidence/<name>/evidence.jsonl and appends Jev's answers (top choice and a
probability per option) to answers.jsonl in the same directory. Resumable: evidence already
answered is skipped, and a failed call goes to errors.jsonl and is retried on the next run.

Needs a TypeSafe API key in the TYPESAFE_API_KEY environment variable. Without one it asks
nothing: it writes the exact requests it would send to requests.jsonl, with an estimate of the
cost, and says where the key goes. Run it with `uv run --script`, which installs the SDK in a
throwaway environment, so the project doesn't depend on it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import question  # noqa: E402

KEY_VARIABLE = "TYPESAFE_API_KEY"
CONCURRENCY = 16
PRICE_PER_MILLION_INPUT_TOKENS = 0.042  # USD, jev-1.13.0; output tokens are free
CHARS_PER_TOKEN_ESTIMATE = 4  # for the estimate written without a key


def state(evidence: dict) -> dict:
    """What Jev judges for one piece of evidence."""
    return {
        "text": evidence["text"],
        "subject": evidence["subject"],
        "object": evidence["object"],
        "claim": evidence["claim"],
    }


def request_body(q: dict, evidence: dict) -> dict:
    """The JSON the SDK sends to /v1/systemone, for the record when there is no key."""
    return {
        "model": q["model"],
        "state": state(evidence),
        "questions": {
            "q": {
                "type": "choice",
                "instructions": q["instructions"],
                "criteria": question.criteria(q),
            }
        },
    }


def evidence_id(evidence: dict) -> str:
    return "\t".join([*evidence["key"], evidence["pmid"]])


def ask(client, q: dict, evidence: dict) -> dict:
    """One Choice question. Service errors are returned, not raised, so a rerun retries them."""
    from typesafe_sdk import Choice, TypeSafeError

    base = {"key": evidence["key"], "pmid": evidence["pmid"]}
    try:
        response = client.system_one(
            state=state(evidence),
            questions={"q": Choice(instructions=q["instructions"], criteria=question.criteria(q))},
        )
    except TypeSafeError as e:
        return {**base, "error": f"{type(e).__name__}: {e}"}
    answer = response.answers["q"]
    return {
        **base,
        "choice": answer.choice,
        "probabilities": dict(answer.probabilities),
        "model": response.model,
        "input_tokens": response.usage.input_tokens if response.usage else None,
    }


def write_requests(q: dict, todo: list[dict], work: Path) -> None:
    """No key: write what would be sent, and how to send it."""
    path = work / "requests.jsonl"
    chars = 0
    with open(path, "w") as out:
        for evidence in todo:
            body = request_body(q, evidence)
            chars += len(json.dumps(body))
            out.write(json.dumps(body) + "\n")
    tokens = chars / CHARS_PER_TOKEN_ESTIMATE
    print(
        f"No {KEY_VARIABLE} set, so nothing was sent. Wrote the {len(todo):,} requests to {path} "
        f"(about {tokens / 1e6:.2f}M input tokens, roughly "
        f"${tokens / 1e6 * PRICE_PER_MILLION_INPUT_TOKENS:.2f}).\n"
        f"To run them: export {KEY_VARIABLE}=<key from https://typesafe.ai> (keep it in your shell "
        "profile or secret manager, never in the repo), then run this command again."
    )


def main(argv: list[str] | None = None, client=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("question", type=Path, help="question file (reference/question-format.md)")
    args = parser.parse_args(argv)

    q = question.load(args.question)
    work = question.work_dir(q)
    evidence_path = work / "evidence.jsonl"
    if not evidence_path.exists():
        print(f"No {evidence_path}; run gather_evidence.py first.")
        return 1
    answers_path = work / "answers.jsonl"
    done = set()
    if answers_path.exists():
        with open(answers_path) as f:
            done = {evidence_id(r) for r in map(json.loads, f)}
    with open(evidence_path) as f:
        todo = [e for e in map(json.loads, f) if evidence_id(e) not in done]
    print(f"{len(done):,} already answered, {len(todo):,} to ask")
    if not todo:
        return 0

    if client is None:
        key = os.environ.get(KEY_VARIABLE)
        if not key:
            write_requests(q, todo, work)
            return 0
        from typesafe_sdk import RetryPolicy, TypeSafeClient

        # No SDK retries: a failure is recorded and retried by the next run instead of hidden
        client = TypeSafeClient(
            api_key=key, model=q["model"], timeout=30, retry=RetryPolicy(max_retries=0)
        )

    ok = failed = tokens = 0
    with (
        ThreadPoolExecutor(max_workers=CONCURRENCY) as pool,
        open(answers_path, "a") as out,
        open(work / "errors.jsonl", "w") as errors,
    ):
        for result in pool.map(lambda e: ask(client, q, e), todo):
            if "error" in result:
                errors.write(json.dumps(result) + "\n")
                failed += 1
            else:
                out.write(json.dumps(result) + "\n")
                ok += 1
                tokens += result.get("input_tokens") or 0
    cost = tokens / 1e6 * PRICE_PER_MILLION_INPUT_TOKENS
    print(f"answered {ok:,}, failed {failed:,}; {tokens:,} input tokens, ${cost:.2f}")
    if failed:
        print(f"See {work / 'errors.jsonl'}; run again to retry the failed ones.")
    return 1 if failed > ok else 0


if __name__ == "__main__":
    raise SystemExit(main())
