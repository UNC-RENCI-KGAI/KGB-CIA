"""Calling the RENCI Azure models directly, without Pi.

Step 5 labels in bulk by calling a cheap model straight from this script rather than through
subagents, and `kgb-augment` has to run from `shared/pipeline.py`, which can't start subagents.
Models, endpoints and costs come from `.pi/models.json` (the `renci-azure` provider), so the
costs reported match what Pi shows; the key is read from `RENCI_AZURE_API_KEY`.

Prompts are versioned by content: `prompt_version` hashes the prompt text, so any edit to a
prompt starts a fresh set of labels in the cache without anyone remembering to bump a number.

`Client` speaks the OpenAI v1 Responses API, which is what `azure-openai-responses` in
`.pi/models.json` means. It asks for a JSON object (`text.format: json_object`) but still digs
the object out of code fences or prose, since not every deployment on the endpoint honours the
format; one that rejects the format, or the reasoning effort, is retried without it.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

from pubmed_evidence import config

# Longest wait between retries, whatever `retry-after` asks for.
MAX_RETRY_WAIT = 120.0


@dataclass(frozen=True)
class ModelInfo:
    id: str
    base_url: str
    api: str = "azure-openai-responses"
    reasoning: bool = False
    input_cost: float | None = None  # USD per million tokens, as in .pi/models.json
    output_cost: float | None = None
    cache_read_cost: float | None = None
    max_tokens: int | None = None
    context_window: int | None = None


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost: float | None = None
    seconds: float = 0.0


class LLMError(RuntimeError):
    """A call that failed after retries, or a response with no JSON object in it.

    `usage` is what the failed call cost, when the service reported it (an incomplete or
    unparseable reply still uses tokens), so callers can log it."""

    def __init__(self, message: str, usage: Usage | None = None):
        super().__init__(message)
        self.usage = usage


def prompt_version(text: str, extra: str = "", name: str = "prompt") -> str:
    """'<name>@<first 8 hex digits of SHA-256(text + extra)>'. `extra` is for whatever else
    shapes the request, such as the version of the code that formats the user message."""
    digest = hashlib.sha256((text + "\0" + extra).encode("utf-8")).hexdigest()[:8]
    return f"{name}@{digest}"


def load_prompt(path: Path, extra: str = "") -> tuple[str, str]:
    """(prompt text, prompt version) of a prompt file; the version is named after its stem."""
    text = Path(path).read_text(encoding="utf-8")
    return text, prompt_version(text, extra, Path(path).stem)


# --- Models ---------------------------------------------------------------------------------


def _warn(message: str) -> None:
    print(f"llm: warning: {message}", file=sys.stderr)


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _provider(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        _warn(f"can't read {path}: {e}")
        return None
    provider = (data.get("providers") or {}).get(config.LLM_PROVIDER)
    return provider if isinstance(provider, dict) else None


def load_model(model_id: str, base_url: str | None = None) -> ModelInfo:
    """The model's entry in `.pi/models.json`, or one with unknown costs at the default URL.

    The files in `config.PI_MODELS` are searched in order (the repository's first) for the
    model under the `renci-azure` provider. A model that isn't listed, such as a candidate not
    yet added, gets `config.LLM_BASE_URL` and unknown costs; it is assumed to take a reasoning
    effort, which `Client` drops if the deployment rejects it. `base_url` overrides the URL.
    """
    for path in map(Path, config.PI_MODELS):
        if not path.exists():
            continue
        provider = _provider(path)
        if provider is None:
            continue
        for entry in provider.get("models") or []:
            if not isinstance(entry, dict) or entry.get("id") != model_id:
                continue
            cost = entry.get("cost") or {}
            url = base_url or entry.get("baseUrl") or provider.get("baseUrl") or config.LLM_BASE_URL
            return ModelInfo(
                id=model_id,
                base_url=url.rstrip("/"),
                api=entry.get("api") or provider.get("api") or ModelInfo.api,
                reasoning=bool(entry.get("reasoning", False)),
                input_cost=_number(cost.get("input")),
                output_cost=_number(cost.get("output")),
                cache_read_cost=_number(cost.get("cacheRead")),
                max_tokens=entry.get("maxTokens"),
                context_window=entry.get("contextWindow"),
            )
    _warn(
        f"{model_id} is not listed under {config.LLM_PROVIDER} in "
        f"{', '.join(map(str, config.PI_MODELS))}; using {base_url or config.LLM_BASE_URL} with "
        "unknown costs"
    )
    return ModelInfo(
        id=model_id, base_url=(base_url or config.LLM_BASE_URL).rstrip("/"), reasoning=True
    )


def cost_of(
    model: ModelInfo, input_tokens: int, output_tokens: int, cached_tokens: int = 0
) -> float | None:
    """USD for a call, from the model's per-million prices; None if they are unknown. Cached
    input tokens are billed at the cache-read price (the input price if that is unknown)."""
    if model.input_cost is None or model.output_cost is None:
        return None
    cached = max(0, min(cached_tokens, input_tokens))
    cache_read = model.cache_read_cost if model.cache_read_cost is not None else model.input_cost
    return (
        (input_tokens - cached) * model.input_cost
        + cached * cache_read
        + output_tokens * model.output_cost
    ) / 1e6


# --- Replies --------------------------------------------------------------------------------

_FENCE = re.compile(r"```[A-Za-z]*\s*\n?(.*?)```", re.S)
# raw_decode is tried from at most this many '{' before giving up on a reply.
_MAX_STARTS = 50


def parse_json_object(text: str) -> dict:
    """The JSON object in a reply: the whole text, a fenced block, the outermost `{...}`, or
    the first object that parses. Raises ValueError if there is none."""
    text = (text or "").strip()
    for candidate in [text, *(m.group(1).strip() for m in _FENCE.finditer(text))]:
        try:
            obj = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    first, last = text.find("{"), text.rfind("}")
    if 0 <= first < last:
        try:
            obj = json.loads(text[first : last + 1])
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
    decoder = json.JSONDecoder()
    for n, match in enumerate(re.finditer(r"\{", text)):
        if n >= _MAX_STARTS:
            break
        try:
            obj, _ = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError("no JSON object in the reply")


def response_text(data: dict) -> str:
    """The reply's text: its `output_text` content parts joined (or a top-level `output_text`).
    Raises LLMError for an incomplete, failed or refused response."""
    status = data.get("status")
    if status == "incomplete":
        reason = (data.get("incomplete_details") or {}).get("reason") or "no reason given"
        raise LLMError(f"incomplete response ({reason})")
    error = data.get("error")
    if status == "failed" or error:
        error = error if isinstance(error, dict) else {"message": str(error or "")}
        raise LLMError(f"response failed: {error.get('code') or ''} {error.get('message') or ''}")
    texts: list[str] = []
    refusals: list[str] = []
    for item in data.get("output") or []:
        content = item.get("content") if isinstance(item, dict) else None
        for part in content if isinstance(content, list) else []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "output_text":
                texts.append(part.get("text") or "")
            elif part.get("type") == "refusal":
                refusals.append(part.get("refusal") or "")
    if any(t.strip() for t in texts):
        return "".join(texts)
    if isinstance(data.get("output_text"), str) and data["output_text"].strip():
        return data["output_text"]
    if refusals:
        raise LLMError(f"the model refused: {' '.join(refusals)[:300]}")
    return ""


def _add_usage(usage: Usage, data: dict) -> None:
    u = data.get("usage") or {}
    usage.input_tokens += int(u.get("input_tokens") or 0)
    usage.output_tokens += int(u.get("output_tokens") or 0)
    usage.cached_tokens += int((u.get("input_tokens_details") or {}).get("cached_tokens") or 0)


def _error_message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:500]
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        return " ".join(str(error.get(k)) for k in ("code", "param", "message") if error.get(k))
    return json.dumps(data)[:500]


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds the service asks us to wait: Azure's `retry-after-ms`, or `retry-after` as
    seconds or an HTTP date."""
    ms = response.headers.get("retry-after-ms")
    if ms:
        try:
            return max(0.0, float(ms) / 1000)
        except ValueError:
            pass
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _backoff(attempt: int) -> float:
    """Exponential backoff with jitter: about 1, 2, 4, ... seconds, at most a minute."""
    return min(60.0, 2.0**attempt) * random.uniform(0.5, 1.5)


def _sleep(seconds: float) -> None:
    # A module function so tests can skip the waits.
    time.sleep(seconds)


# --- Client ---------------------------------------------------------------------------------


class Client:
    """One model on the RENCI Azure endpoint (OpenAI v1 Responses API)."""

    def __init__(
        self,
        model: ModelInfo,
        api_key: str | None = None,
        *,
        timeout: float = 300.0,
        max_retries: int = 4,
        transport=None,
    ):
        if "responses" not in model.api:
            raise LLMError(f"{model.id}: api {model.api!r} is not supported, only Responses")
        key = api_key or os.environ.get(config.LLM_API_KEY)
        if not key:
            raise LLMError(f"no API key for {model.id}: set {config.LLM_API_KEY}")
        self.model = model
        self.max_retries = max_retries
        # Turned off for the client's lifetime when the deployment rejects them.
        self.json_format = True
        self.send_reasoning = model.reasoning
        self._http = httpx.Client(
            base_url=model.base_url.rstrip("/") + "/",
            headers={"api-key": key},
            timeout=httpx.Timeout(timeout, connect=min(timeout, 30.0)),
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _body(
        self, system: str, user: str, max_output_tokens: int, reasoning_effort: str | None
    ) -> dict[str, Any]:
        if self.model.max_tokens:
            max_output_tokens = min(max_output_tokens, self.model.max_tokens)
        body: dict[str, Any] = {
            "model": self.model.id,
            "instructions": system,
            "input": user,
            "max_output_tokens": max_output_tokens,
            "store": False,
        }
        if self.send_reasoning and reasoning_effort:
            body["reasoning"] = {"effort": reasoning_effort}
        if self.json_format:
            body["text"] = {"format": {"type": "json_object"}}
        return body

    def _post(self, body: dict[str, Any]) -> dict:
        """POST /responses, retrying 429, 5xx, timeouts and connection errors with backoff."""
        attempt = 0
        while True:
            try:
                response = self._http.post("responses", json=body)
            except httpx.TransportError as e:
                if attempt >= self.max_retries:
                    raise LLMError(
                        f"{self.model.id}: {type(e).__name__} after {attempt + 1} attempts: {e}"
                    ) from e
                delay = _backoff(attempt)
                _warn(f"{self.model.id}: {type(e).__name__}; retrying in {delay:.1f} s")
                _sleep(delay)
                attempt += 1
                continue
            except httpx.HTTPError as e:
                # Not a network failure (a body that can't be decoded, too many redirects):
                # retrying won't help, but it must still surface as a failed call.
                raise LLMError(f"{self.model.id}: {type(e).__name__}: {e}") from e
            status = response.status_code
            if status in (408, 429) or status >= 500:
                if attempt >= self.max_retries:
                    raise LLMError(
                        f"{self.model.id}: HTTP {status} after {attempt + 1} attempts: "
                        f"{_error_message(response)}"
                    )
                wait = _retry_after(response)
                delay = _backoff(attempt) if wait is None else wait + random.uniform(0, 1)
                delay = min(delay, MAX_RETRY_WAIT)
                _warn(f"{self.model.id}: HTTP {status}; retrying in {delay:.1f} s")
                _sleep(delay)
                attempt += 1
                continue
            if status >= 400:
                message = _error_message(response)
                lowered = message.lower()
                if status == 400 and "text" in body and "format" in lowered:
                    self.json_format = False
                    body = {k: v for k, v in body.items() if k != "text"}
                    continue
                if status == 400 and "reasoning" in body and "reasoning" in lowered:
                    self.send_reasoning = False
                    body = {k: v for k, v in body.items() if k != "reasoning"}
                    continue
                raise LLMError(f"{self.model.id}: HTTP {status}: {message}")
            try:
                data = response.json()
            except ValueError as e:
                raise LLMError(f"{self.model.id}: the response is not JSON") from e
            if not isinstance(data, dict):
                raise LLMError(f"{self.model.id}: unexpected response: {str(data)[:200]}")
            return data

    def complete_json(
        self,
        system: str,
        user: str,
        *,
        max_output_tokens: int = 16000,
        reasoning_effort: str | None = None,
    ) -> tuple[dict, Usage]:
        """Send one request and return the JSON object in the reply, with its token use.

        Retries on 429 and 5xx with backoff, and once on a reply with no parseable JSON
        object; raises LLMError after that. Safe to call from several threads at once.
        """
        start = time.monotonic()
        usage = Usage()

        def finish() -> Usage:
            usage.seconds = time.monotonic() - start
            usage.cost = cost_of(
                self.model, usage.input_tokens, usage.output_tokens, usage.cached_tokens
            )
            return usage

        try:
            for attempt in (1, 2):
                data = self._post(self._body(system, user, max_output_tokens, reasoning_effort))
                _add_usage(usage, data)
                text = response_text(data)
                try:
                    return parse_json_object(text), finish()
                except ValueError:
                    if attempt == 2:
                        snippet = " ".join(text.split())[:200]
                        raise LLMError(
                            f"{self.model.id}: no JSON object in the reply: {snippet!r}"
                        ) from None
        except LLMError as e:
            if e.usage is None:
                e.usage = finish()
            raise
        raise AssertionError("unreachable")
