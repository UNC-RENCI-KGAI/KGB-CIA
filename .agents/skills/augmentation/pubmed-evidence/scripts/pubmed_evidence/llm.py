"""Calling the models directly, without Pi.

Step 3 labels in bulk by calling a cheap model straight from the library rather than through
subagents, and `kgb-augment` has to run from `shared/pipeline.py`, which can't start subagents.
Every model the steps can call is in `config.MODELS`, with its endpoint, the environment
variable holding its key, its prices and the settings a call sends by default; any other
model is an error.

Prompts are versioned by content: `prompt_version` hashes the prompt text, so any edit to a
prompt starts a fresh set of labels in the cache without anyone remembering to bump a number. A
prompt file may include another (`{{include NAME}}` on a line of its own), so that two prompts
share a section; the version hashes the text with its includes.

Replies are strict structured output: every call sends its step's JSON Schema (`Schema`), the
same for every request so the service's prompt cache holds, and the service returns an object
that fits it. Code then checks only what a schema can't say, such as verbatim quotes.
A client's `call` returns the reply and its `Usage`: the tokens of each attempt, in one form
for every backend (uncached input, cache read, cache write, output), and the model that
answered.

A model's `backend` picks its client (`client_for`): `OpenAIClient` speaks the OpenAI v1
Responses API, which the RENCI Azure endpoint serves, through the `openai` SDK, and
`AnthropicClient` Anthropic's Messages API, for Claude models, through the `anthropic` SDK.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import openai
from openai.types.responses import Response

from pubmed_evidence import config
from pubmed_evidence.cache import CallRow, LabelCache

# Anthropic's strict output allows at most this many parameters with union types (`anyOf`, as
# `nullable` makes, or a type array) across a request's schemas, and rejects every call over it.
MAX_UNION_PARAMETERS = 16


@dataclass(frozen=True)
class Schema:
    """A reply's JSON Schema, kept within both providers' rules for strict output: every object
    lists all its properties as required (optional ones are nullable) and allows no others,
    nothing has a length or numeric limit, and no more than MAX_UNION_PARAMETERS parameters are
    nullable. `name` is what the Responses API asks for."""

    name: str
    body: dict[str, Any]

    def text(self) -> str:
        """Its canonical JSON, which prompt versions hash: a changed schema starts afresh."""
        return json.dumps(self.body, sort_keys=True, separators=(",", ":"))


def nullable(schema: dict[str, Any]) -> dict[str, Any]:
    """`schema` or null, for a property strict mode requires but the reply may leave empty."""
    return {"anyOf": [schema, {"type": "null"}]}


def positions(prefix: str, most: int) -> dict[str, Any]:
    """A reference to an item of the request by its position, `<prefix>1` to `<prefix><most>`:
    an enum, so the schema stays the same whatever the request holds."""
    return {"type": "string", "enum": [f"{prefix}{i}" for i in range(1, most + 1)]}


def strict_object(properties: dict[str, Any]) -> dict[str, Any]:
    """An object with exactly these properties, all required."""
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


@dataclass
class Attempt:
    """One attempt at a call, by one model: its uncached input, prompt-cache reads and writes,
    and output (reasoning included)."""

    model: str
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0

    @property
    def cost(self) -> float | None:
        """USD at its model's prices; None for a model that isn't in config.MODELS."""
        model = config.MODELS.get(self.model)
        if model is None:
            return None
        return cost_of(
            model,
            self.input_tokens,
            self.cache_read_tokens,
            self.cache_write_tokens,
            self.output_tokens,
        )


@dataclass
class Usage:
    """A call's token use, per attempt: a service that retries a refused request on another
    model (Anthropic's fallbacks) bills each attempt at its own model's price. The model that
    answered is the last attempt's."""

    attempts: list[Attempt] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def model(self) -> str | None:
        return self.attempts[-1].model if self.attempts else None

    @property
    def input_tokens(self) -> int:
        """Every prompt token, the cache's reads and writes included."""
        return sum(
            a.input_tokens + a.cache_read_tokens + a.cache_write_tokens for a in self.attempts
        )

    @property
    def cache_read_tokens(self) -> int:
        return sum(a.cache_read_tokens for a in self.attempts)

    @property
    def cache_write_tokens(self) -> int:
        return sum(a.cache_write_tokens for a in self.attempts)

    @property
    def output_tokens(self) -> int:
        return sum(a.output_tokens for a in self.attempts)

    @property
    def cost(self) -> float | None:
        """USD over the attempts; None if any attempt's cost is unknown."""
        costs = [a.cost for a in self.attempts]
        return None if None in costs else sum(costs)


class LLMError(RuntimeError):
    """A call that failed after retries, was refused or cut short, or whose reply isn't JSON.

    `usage` is what the failed call cost, when the service reported it (an incomplete reply
    still uses tokens), so callers can log it."""

    def __init__(self, message: str, usage: Usage | None = None):
        super().__init__(message)
        self.usage = usage


def prompt_version(text: str, extra: str = "", name: str = "prompt") -> str:
    """'<name>@<first 8 hex digits of SHA-256(text + extra)>'. `extra` is for whatever else
    shapes the request, such as the version of the code that formats the user message."""
    digest = hashlib.sha256((text + "\0" + extra).encode("utf-8")).hexdigest()[:8]
    return f"{name}@{digest}"


# A line of a prompt file that includes another: `{{include claim-rules.md}}`.
_INCLUDE = re.compile(r"^\{\{include ([\w.-]+)\}\}[ \t]*$", re.M)


def read_prompt(path: Path) -> str:
    """A prompt file's text, each `{{include NAME}}` line replaced by the text of NAME: the file
    beside it, else the one in the skill's prompts directory, so a copy of a prompt being tuned
    elsewhere still finds the shared ones. Includes aren't nested. Raises FileNotFoundError for
    an include found in neither place."""
    path = Path(path)

    def include(match: re.Match) -> str:
        name = match.group(1)
        for directory in (path.parent, config.PROMPTS_DIR):
            if (directory / name).is_file():
                return (directory / name).read_text(encoding="utf-8").rstrip("\n")
        raise FileNotFoundError(
            f"{path} includes {name}, which is neither beside it nor in {config.PROMPTS_DIR}"
        )

    return _INCLUDE.sub(include, path.read_text(encoding="utf-8"))


def load_prompt(path: Path, extra: str = "") -> tuple[str, str]:
    """(prompt text, prompt version) of a prompt file, its includes in place; the version is
    named after its stem."""
    text = read_prompt(path)
    return text, prompt_version(text, extra, Path(path).stem)


# --- Models ---------------------------------------------------------------------------------


def model(model_id: str) -> config.Model:
    """The model's entry in `config.MODELS`. Raises LLMError for a model that isn't there."""
    try:
        return config.MODELS[model_id]
    except KeyError:
        raise LLMError(
            f"{model_id} is not in config.MODELS; add it there, with its prices, to use it"
        ) from None


def cost_of(
    model: config.Model,
    input_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    output_tokens: int,
) -> float:
    """USD for one attempt by `model`, from its per-million prices: uncached input, the prompt
    cache's reads and writes, and output. A prompt (all three kinds of input) longer than the
    model's `long_prompt` tier is billed at that tier's price."""
    price = model.price
    prompt = input_tokens + cache_read_tokens + cache_write_tokens
    if model.long_prompt is not None and prompt > model.long_prompt[0]:
        price = model.long_prompt[1]
    return (
        input_tokens * price.input
        + cache_read_tokens * price.cache_read
        + cache_write_tokens * price.cache_write
        + output_tokens * price.output
    ) / 1e6


# --- Replies --------------------------------------------------------------------------------


def _reply_object(model: str, text: str) -> dict:
    """The reply's JSON object. Raises LLMError if the text isn't one."""
    try:
        reply = json.loads(text)
    except ValueError:
        snippet = " ".join(text.split())[:200]
        raise LLMError(f"{model}: the reply is not JSON: {snippet!r}") from None
    if not isinstance(reply, dict):
        raise LLMError(f"{model}: the reply is not a JSON object")
    return reply


def response_text(response: Response) -> str:
    """A Responses API reply's text. Raises LLMError for an incomplete, failed or refused
    response."""
    if response.status == "incomplete":
        details = response.incomplete_details
        raise LLMError(f"incomplete response ({details and details.reason or 'no reason given'})")
    if response.status == "failed" or response.error:
        error = response.error
        raise LLMError(f"response failed: {error.code} {error.message}" if error else "failed")
    if response.output_text.strip():
        return response.output_text
    refusals = [
        part.refusal
        for item in response.output
        if item.type == "message"
        for part in item.content
        if part.type == "refusal"
    ]
    if refusals:
        raise LLMError(f"the model refused: {' '.join(refusals)[:300]}")
    return ""


def _attempt(model: str, response: Response) -> Attempt:
    """A Responses API reply's usage in the common form. OpenAI counts cached tokens among its
    `input_tokens`, and writes to its cache cost nothing."""
    u = response.usage
    if u is None:
        return Attempt(model)
    total, details = u.input_tokens or 0, u.input_tokens_details
    cached = min(total, details and details.cached_tokens or 0)
    return Attempt(model, total - cached, cached, 0, u.output_tokens or 0)


def claude_attempts(model: str, usage: Any) -> list[Attempt]:
    """A Messages API reply's usage in the common form, an Attempt per attempt. With a
    server-side fallback, `usage.iterations` lists each (the declined attempt, then the one
    that served the reply), each billed at its own model's price, and the top-level usage
    covers only the last; an entry without a model is the requested one's. Anthropic's input
    tokens leave out the cache's reads and writes, as the common form does."""
    entries = [i for i in usage.iterations or [] if i.type in ("message", "fallback_message")]
    return [
        Attempt(
            getattr(i, "model", None) or model,
            i.input_tokens or 0,
            i.cache_read_input_tokens or 0,
            i.cache_creation_input_tokens or 0,
            i.output_tokens or 0,
        )
        for i in entries or [usage]
    ]


# --- Clients --------------------------------------------------------------------------------


def sent_effort(model: config.Model, override: str | None) -> str | None:
    """The effort a call sends: the override, else the model's; none for 'none', or for a
    model that takes none."""
    effort = model.effort if override is None else override
    return effort if model.reasoning and effort and effort.lower() != "none" else None


def run_effort(model_id: str, override: str | None = None) -> str:
    """The reasoning effort a step's calls to `model_id` send, as its prompt version hashes it:
    the model's own in config.MODELS, or `override`; 'none' when they send none. Raises
    LLMError for a model not in config.MODELS."""
    return sent_effort(model(model_id), override) or "none"


class OpenAIClient:
    """One model of `config.MODELS` on an OpenAI v1 Responses API endpoint, through the
    `openai` SDK, which retries rate limits, server errors, timeouts and dropped connections
    itself, as the service's `retry-after` asks.

    `max_output_tokens` and `reasoning_effort` override the model's own settings, for tuning:
    an effort of 'none' sends none. A model that takes no reasoning effort is sent none.
    `http_client` (an `httpx2.Client`, the SDK's HTTP library) is for tests."""

    def __init__(
        self,
        model: config.Model,
        api_key: str | None = None,
        *,
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
        timeout: float = 300.0,
        max_retries: int = 4,
        http_client: Any = None,
    ):
        key = api_key or os.environ.get(model.api_key)
        if not key:
            raise LLMError(f"no API key for {model.id}: set {model.api_key}")
        self.model = model
        self.max_output_tokens = max_output_tokens or model.max_output_tokens
        self.effort = sent_effort(model, reasoning_effort)
        self._sdk = openai.OpenAI(
            base_url=model.base_url,
            api_key=key,
            timeout=timeout,
            max_retries=max_retries,
            http_client=http_client,
        )

    def close(self) -> None:
        self._sdk.close()

    def __enter__(self) -> OpenAIClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _body(self, system: str, user: str, schema: Schema) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model.id,
            "instructions": system,
            "input": user,
            "max_output_tokens": self.max_output_tokens,
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema.name,
                    "strict": True,
                    "schema": schema.body,
                }
            },
        }
        if self.effort:
            body["reasoning"] = {"effort": self.effort}
        return body

    def call(self, system: str, user: str, schema: Schema) -> tuple[dict, Usage]:
        """Send one request for a reply fitting `schema`, and return the reply with its token
        use. Raises LLMError for a call that still fails after the SDK's retries, that the
        service rejects (a deployment that doesn't take the schema, or strict output, fails
        here, clearly), or that is refused or cut short, or whose reply isn't a JSON object.
        Safe to call from several threads at once."""
        start = time.monotonic()
        usage = Usage()
        try:
            try:
                response = self._sdk.responses.create(**self._body(system, user, schema))
            except openai.APIError as e:
                raise LLMError(f"{self.model.id}: {type(e).__name__}: {e}") from e
            usage.attempts.append(_attempt(self.model.id, response))
            reply = _reply_object(self.model.id, response_text(response))
            usage.seconds = time.monotonic() - start
            return reply, usage
        except LLMError as e:
            usage.seconds = time.monotonic() - start
            if e.usage is None:
                e.usage = usage
            raise


class AnthropicClient:
    """One Claude model of `config.MODELS` on Anthropic's Messages API, through the `anthropic`
    SDK (imported only when a Claude model is used), which retries rate limits, server errors,
    timeouts and dropped connections itself.

    The SDK finds the credentials, `ANTHROPIC_API_KEY` or an `ant auth login` profile; with
    none, building the client raises LLMError, before any call. The system prompt ends in a
    `cache_control` breakpoint, so later calls read it from the prompt cache. The schema goes in
    `output_config.format` with the effort beside it; thinking stays at the model's default,
    adaptive. A model with `fallbacks` asks for Anthropic's server-side fallback: a request a
    safety classifier declines is run again on a model Anthropic picks by the refusal's
    category, and each attempt is priced by its own model (`claude_attempts`). A refusal that
    stands is a failed call. The settings and `http_client` are as for `OpenAIClient`."""

    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(
        self,
        model: config.Model,
        api_key: str | None = None,
        *,
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
        timeout: float = 300.0,
        max_retries: int = 4,
        http_client: Any = None,
    ):
        import anthropic

        self._errors = anthropic.APIError
        self.model = model
        self.max_output_tokens = max_output_tokens or model.max_output_tokens
        self.effort = sent_effort(model, reasoning_effort)
        problem = None
        try:
            self._sdk = anthropic.Anthropic(
                api_key=api_key,
                base_url=model.base_url,
                timeout=timeout,
                max_retries=max_retries,
                http_client=http_client,
            )
        except anthropic.AnthropicError as e:
            problem = str(e)  # a profile the configuration names but that isn't there
        else:
            # Without a key, a token or a profile on disk the SDK builds a client all the same,
            # and every call fails.
            sdk = self._sdk
            if sdk.api_key is None and sdk.auth_token is None and sdk.credentials is None:
                problem = "none found"
        if problem:
            raise LLMError(
                f"no credentials for {model.id}: set {model.api_key} or run `ant auth login` "
                f"({problem})"
            )

    def close(self) -> None:
        self._sdk.close()

    def __enter__(self) -> AnthropicClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _body(self, system: str, user: str, schema: Schema) -> dict[str, Any]:
        output: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema.body}}
        if self.effort:
            output["effort"] = self.effort
        body: dict[str, Any] = {
            "model": self.model.id,
            "max_tokens": self.max_output_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
            "output_config": output,
        }
        if self.model.fallbacks:
            body |= {"betas": [self.FALLBACK_BETA], "fallbacks": "default"}
        return body

    def call(self, system: str, user: str, schema: Schema) -> tuple[dict, Usage]:
        """As `OpenAIClient.call`. `Usage.model` is the model that answered: a fallback's, if
        one did."""
        start = time.monotonic()
        usage = Usage()
        try:
            try:
                message = self._sdk.beta.messages.create(**self._body(system, user, schema))
            except self._errors as e:
                raise LLMError(f"{self.model.id}: {type(e).__name__}: {e}") from e
            usage.attempts += claude_attempts(self.model.id, message.usage)
            if message.stop_reason == "refusal":
                category = getattr(message.stop_details, "category", None) or "no category"
                raise LLMError(f"{self.model.id}: the model refused ({category})")
            if message.stop_reason == "max_tokens":
                raise LLMError(f"{self.model.id}: incomplete response (max_tokens)")
            text = "".join(block.text for block in message.content if block.type == "text")
            reply = _reply_object(self.model.id, text)
            usage.seconds = time.monotonic() - start
            return reply, usage
        except LLMError as e:
            usage.seconds = time.monotonic() - start
            if e.usage is None:
                e.usage = usage
            raise


def client_for(
    model: config.Model,
    *,
    max_output_tokens: int | None = None,
    reasoning_effort: str | None = None,
) -> OpenAIClient | AnthropicClient:
    """A client for the model's backend, with these overrides of its settings. Raises LLMError
    when the model's credentials are missing."""
    backend = AnthropicClient if model.backend == "anthropic" else OpenAIClient
    return backend(model, max_output_tokens=max_output_tokens, reasoning_effort=reasoning_effort)


# --- Running calls --------------------------------------------------------------------------

# A step whose failed calls are more than this share of the calls its work needs fails; below
# it, it warns.
MAX_FAILED_SHARE = 0.05


def too_many_failed(needed: int, failed: int) -> bool:
    """Whether a step failed: more than MAX_FAILED_SHARE of the calls its work needs did.
    `needed` counts the calls this run sent and those the work answered by earlier runs took,
    so a rerun that resends only a few calls that keep failing isn't judged on those alone,
    and a stuck call can't fail every later run."""
    return bool(needed) and failed / needed > MAX_FAILED_SHARE


@dataclass
class Call:
    """One request: the user message, the pair it is about, whatever the step needs to read the
    reply (`context`), and the reply's schema when it isn't the step's own (label's differs by
    the pair's scope rules)."""

    pair_id: str
    user: str
    context: Any = None
    schema: Schema | None = None


@dataclass
class Tally:
    """What a step's calls did: how many, how many failed, how many another model answered
    (a server-side fallback; a call that failed counts as unanswered), the pairs and the token
    use. Input counts every prompt token,
    the cache's reads and writes among them."""

    calls: int = 0
    failed: int = 0
    fallback_served: int = 0
    pairs: set[str] = field(default_factory=set)
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    cost: float | None = None

    def add(self, usage: Usage) -> None:
        self.input_tokens += usage.input_tokens
        self.cache_read_tokens += usage.cache_read_tokens
        self.cache_write_tokens += usage.cache_write_tokens
        self.output_tokens += usage.output_tokens
        if usage.cost is not None:
            self.cost = (self.cost or 0.0) + usage.cost


def _send(
    client: Any, system: str, user: str, schema: Schema
) -> tuple[Any, Usage, str | None, float]:
    """(reply, usage, error, seconds) of one call. Any exception is a failed call: raising
    here would lose the replies of the calls still in flight."""
    start = time.monotonic()
    try:
        reply, usage = client.call(system, user, schema)
    except Exception as e:
        error = str(e) if isinstance(e, LLMError) else f"{type(e).__name__}: {e}"
        usage = getattr(e, "usage", None) or Usage()
        return None, usage, error or type(e).__name__, time.monotonic() - start
    return reply, usage, None, time.monotonic() - start


def run_calls(
    calls: Iterable[Call],
    client: Any,
    on_result: Callable[[Call, Any, Usage], str | None],
    *,
    system: str,
    schema: Schema | None,
    cache: LabelCache,
    step: str,
    model: str,
    prompt_version: str,
    total: int,
    workers: int,
) -> Tally:
    """Send every call to `client` (anything with a client's `call`) from a thread pool, for a
    reply fitting its own schema or else `schema`, and log each in `cache` under (step, model,
    prompt version), failed or not.

    Calls are taken from `calls` only as the pool frees up, so a step can build its messages
    lazily. Each reply goes to `on_result(call, reply, usage)` on this thread, since SQLite
    connections stay in the thread that made them: it stores what the reply says and returns
    an error if the reply can't be used, which makes the call a failed one. A failed call
    stores nothing, so the step's next run asks again. `total` is only for the progress lines.
    """
    tally = Tally()
    every = max(1, total // 20)

    def finish(future: Future, call: Call) -> None:
        reply, usage, error, seconds = future.result()
        if error is None:
            error = on_result(call, reply, usage)
        tally.calls += 1
        tally.fallback_served += error is None and usage.model not in (None, model)
        tally.pairs.add(call.pair_id)
        tally.add(usage)
        if error is not None:
            tally.failed += 1
            print(f"{step}: warning: {call.pair_id}: {error}", file=sys.stderr)
        cache.log_call(
            CallRow(
                step=step,
                pair_id=call.pair_id,
                model=model,
                prompt_version=prompt_version,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost=usage.cost,
                seconds=usage.seconds or seconds,
                error=error,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                answered_by=usage.model,
            )
        )
        if tally.calls % every == 0 or tally.calls == total:
            cost = f", ${tally.cost:.4f}" if tally.cost is not None else ""
            print(
                f"{step}: {tally.calls}/{total} calls ({tally.failed} failed{cost})",
                file=sys.stderr,
            )

    workers = max(1, workers)
    pool = ThreadPoolExecutor(max_workers=workers)
    pending: dict[Future, Call] = {}
    try:
        for call in calls:
            reply_schema = call.schema or schema
            if reply_schema is None:
                raise ValueError(f"{step}: a call for {call.pair_id} has no reply schema")
            pending[pool.submit(_send, client, system, call.user, reply_schema)] = call
            # A bounded window: messages are built only as calls free up.
            while len(pending) >= 2 * workers:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    finish(future, pending.pop(future))
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                finish(future, pending.pop(future))
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown()
    return tally
