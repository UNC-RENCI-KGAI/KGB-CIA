"""llm.py: the model table, the Responses API client and reply parsing, all without network.

The client goes through the `openai` SDK, so a fake endpoint answers through its HTTP library,
`httpx2`. The SDK's own retries aren't tested here; the tests turn them off where a call fails.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import httpx2
import pytest
from pubmed_evidence import config, llm
from pubmed_evidence.cache import LabelCache

MODEL = config.Model(
    id="gpt-test",
    backend="openai",
    base_url="https://example.test/openai/v1",
    api_key=config.AZURE_KEY_VAR,
    price=config.Price(input=0.2, output=1.2, cache_read=0.02, cache_write=0.25),
    price_source="a test",
    effort="medium",
)


def reply(text: str, *, usage: dict | None = None, **extra) -> dict:
    return {
        "id": "resp_1",
        "object": "response",
        "model": "gpt-test-2026-01-01",
        "status": "completed",
        "output": [
            {"type": "reasoning", "id": "rs_1", "summary": []},
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            },
        ],
        "usage": usage
        or {
            "input_tokens": 1000,
            "input_tokens_details": {"cached_tokens": 400},
            "output_tokens": 100,
        },
        **extra,
    }


class Server:
    """A MockTransport handler that answers with the queued responses and records requests."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        r = self.responses.pop(0)
        return r if isinstance(r, httpx2.Response) else httpx2.Response(200, json=r)

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


def client(server, model: config.Model = MODEL, **kw) -> llm.OpenAIClient:
    http = httpx2.Client(transport=httpx2.MockTransport(server))
    return llm.OpenAIClient(model, kw.pop("api_key", "secret"), http_client=http, **kw)


SCHEMA = llm.Schema("test_reply", llm.strict_object({"labels": {"type": "array", "items": {}}}))


def call(c: llm.OpenAIClient) -> tuple[dict, llm.Usage]:
    return c.call("s", "u", SCHEMA)


def test_sends_a_strict_schema_and_reads_the_reply():
    server = Server(reply('{"labels": [], "proposals": []}'))
    data, usage = client(server, max_output_tokens=500, reasoning_effort="low").call(
        "system text", "user text", SCHEMA
    )
    assert data == {"labels": [], "proposals": []}
    request = server.requests[0]
    assert str(request.url) == "https://example.test/openai/v1/responses"
    assert request.headers["authorization"] == "Bearer secret"
    body = server.bodies()[0]
    assert body["model"] == "gpt-test"
    assert body["instructions"] == "system text"
    assert body["input"] == "user text"
    assert body["max_output_tokens"] == 500
    assert body["store"] is False
    assert body["reasoning"] == {"effort": "low"}
    assert body["text"] == {
        "format": {
            "type": "json_schema",
            "name": "test_reply",
            "strict": True,
            "schema": SCHEMA.body,
        }
    }
    # OpenAI counts cached tokens among its input tokens; the common form doesn't. Usage is
    # priced by the model asked for, whatever versioned name the deployment answers with.
    assert usage.attempts == [llm.Attempt("gpt-test", 600, 400, 0, 100)]
    assert (usage.input_tokens, usage.cache_read_tokens, usage.output_tokens) == (1000, 400, 100)
    assert usage.model == "gpt-test"
    assert usage.seconds >= 0


def test_a_reply_without_usage_details():
    out, usage = call(client(Server(reply('{"a": 1}', usage={"output_tokens": 7}))))
    assert out == {"a": 1} and usage.attempts == [llm.Attempt("gpt-test", 0, 0, 0, 7)]


@pytest.mark.parametrize("text", ["", "not json", '```json\n{"a": 1}\n```', "[1, 2]"])
def test_a_reply_that_is_not_a_json_object_fails_at_once(text):
    server = Server(reply(text))
    with pytest.raises(llm.LLMError, match="not") as e:
        call(client(server))
    assert len(server.requests) == 1
    assert e.value.usage.output_tokens == 100  # what it cost is still logged


def test_settings_come_from_the_model_unless_overridden():
    server = Server(reply("{}"), reply("{}"), reply("{}"))
    call(client(server))
    call(client(server, reasoning_effort="None"))
    plain = dataclasses.replace(MODEL, id="gpt-plain", reasoning=False, max_output_tokens=900)
    call(client(server, plain, reasoning_effort="low"))
    default, none, no_reasoning = server.bodies()
    assert (default["reasoning"], default["max_output_tokens"]) == ({"effort": "medium"}, 16000)
    assert "reasoning" not in none
    assert "reasoning" not in no_reasoning and no_reasoning["max_output_tokens"] == 900


def test_a_server_error_the_retries_dont_clear_is_a_failed_call():
    server = Server(httpx2.Response(500, json={"error": {"message": "down"}}))
    with pytest.raises(llm.LLMError, match="gpt-test: InternalServerError: .*down"):
        call(client(server, max_retries=0))
    assert len(server.requests) == 1


def test_a_connection_failure_is_a_failed_call():
    def handler(request):
        raise httpx2.ConnectError("refused", request=request)

    with pytest.raises(llm.LLMError, match="APIConnectionError"):
        call(client(handler, max_retries=0))


def test_a_rejected_schema_fails_at_once():
    rejected = httpx2.Response(
        400,
        json={
            "error": {
                "code": "invalid_request",
                "param": "text.format",
                "message": "Response format json_schema is not supported by this model.",
            }
        },
    )
    server = Server(rejected)
    with pytest.raises(llm.LLMError, match="BadRequestError: .*json_schema is not supported"):
        call(client(server))
    assert len(server.requests) == 1  # a 400 isn't retried


def test_other_client_errors_fail_at_once():
    server = Server(httpx2.Response(401, json={"error": {"message": "bad key"}}))
    with pytest.raises(llm.LLMError, match="AuthenticationError: .*bad key"):
        call(client(server))
    assert len(server.requests) == 1


def test_incomplete_response_raises_with_its_usage():
    data = reply("{")
    data["status"] = "incomplete"
    data["incomplete_details"] = {"reason": "max_output_tokens"}
    with pytest.raises(llm.LLMError, match="max_output_tokens") as e:
        call(client(Server(data)))
    assert e.value.usage.output_tokens == 100
    assert e.value.usage.cost is None  # gpt-test isn't in the model table


def test_a_failed_response_is_a_failed_call():
    data = reply("")
    data["status"] = "failed"
    data["error"] = {"code": "server_error", "message": "it broke"}
    with pytest.raises(llm.LLMError, match="response failed: server_error it broke"):
        call(client(Server(data)))


def test_a_refusal_is_a_failed_call():
    data = reply("")
    data["output"][1]["content"] = [{"type": "refusal", "refusal": "I can't help with that."}]
    with pytest.raises(llm.LLMError, match="refused"):
        call(client(Server(data)))


def test_missing_key(monkeypatch):
    monkeypatch.delenv(config.AZURE_KEY_VAR, raising=False)
    with pytest.raises(llm.LLMError, match=config.AZURE_KEY_VAR):
        llm.OpenAIClient(MODEL)


def test_key_from_environment(monkeypatch):
    monkeypatch.setenv(config.AZURE_KEY_VAR, "from-env")
    server = Server(reply("{}"))
    call(client(server, api_key=None))
    assert server.requests[0].headers["authorization"] == "Bearer from-env"


def test_cost_with_a_long_prompt_tier():
    tier = config.Price(input=1, output=6, cache_read=0.1, cache_write=1.25)
    tiered = dataclasses.replace(MODEL, long_prompt=(100_000, tier))
    # (80,000 * 0.2 + 10,000 * 0.02 + 10,000 * 0.25 + 1,000 * 1.2) / 1e6, at the base price...
    assert llm.cost_of(tiered, 80_000, 10_000, 10_000, 1_000) == pytest.approx(19_900 / 1e6)
    # ... and a prompt one token longer all at the tier's.
    expected = (80_001 * 1 + 10_000 * 0.1 + 10_000 * 1.25 + 1_000 * 6) / 1e6
    assert llm.cost_of(tiered, 80_001, 10_000, 10_000, 1_000) == pytest.approx(expected)


def test_usage_prices_each_attempt_by_its_own_model():
    luna, terra = config.MODELS["gpt-6-luna"], config.MODELS["gpt-5.6-terra"]
    usage = llm.Usage([llm.Attempt(luna.id, 100, 0, 0, 10), llm.Attempt(terra.id, 100, 50, 0, 20)])
    expected = llm.cost_of(luna, 100, 0, 0, 10) + llm.cost_of(terra, 100, 50, 0, 20)
    assert usage.cost == pytest.approx(expected)
    assert (usage.model, usage.input_tokens, usage.output_tokens) == (terra.id, 250, 30)
    usage.attempts.append(llm.Attempt("not-in-the-table", 1, 0, 0, 1))
    assert usage.cost is None


def _check_strict(schema: dict, path: str = "$", defs: dict | None = None) -> None:
    """Both providers' strict-output rules, all through a schema, following its `$ref`s into
    its `$defs` (internal references only)."""
    defs = schema.get("$defs", defs or {})
    assert not {"minLength", "maxLength", "pattern", "minimum", "maximum", "format"} & set(schema)
    assert schema.get("minItems", 0) <= 1 and "maxItems" not in schema, path
    if "$ref" in schema:
        name = schema["$ref"].removeprefix("#/$defs/")
        assert schema["$ref"] == f"#/$defs/{name}" and name in defs, path
        assert set(schema) == {"$ref"}, path
        return
    assert "type" in schema or "anyOf" in schema, path
    if schema.get("type") == "object":
        assert schema["additionalProperties"] is False, path
        assert schema["required"] == list(schema["properties"]), path
        for name, sub in schema["properties"].items():
            _check_strict(sub, f"{path}.{name}", defs)
    for sub in schema.get("anyOf", []):
        _check_strict(sub, path, defs)
    if "items" in schema:
        _check_strict(schema["items"], f"{path}[]", defs)
    for name, sub in schema.get("$defs", {}).items():
        _check_strict(sub, f"{path}.$defs.{name}", defs)


def _schemas() -> list[llm.Schema]:
    """Every reply schema a step sends: label's, one per scope rule and one for a pair matching
    every rule, then review's and repair's."""
    from pubmed_evidence import label, repair, review, scope

    rules = scope.DEFAULT_RULES
    every = [r.id for r in rules if r.tier != "skip"]
    label_schemas = [*label.rule_schemas(rules).values()]
    label_schemas.append(label.reply_schema(label.predicates_of(rules, every)))
    return [*label_schemas, review.REPLY_SCHEMA, repair.REPLY_SCHEMA]


def test_reply_schemas_keep_both_providers_rules(monkeypatch):
    monkeypatch.setenv(config.AZURE_KEY_VAR, "key")
    monkeypatch.setenv(config.ANTHROPIC_API_KEY, "key")
    openai = llm.client_for(config.MODELS["gpt-6-luna"])
    claude = llm.client_for(config.MODELS["claude-sonnet-5-5"])
    for schema in _schemas():
        _check_strict(schema.body)
        json.loads(schema.text())  # plain JSON, the same text every time
        assert schema.text() == llm.Schema(schema.name, json.loads(schema.text())).text()
        # As each backend's request body carries it: strict json_schema output.
        sent = openai._body("system", "user", schema)["text"]["format"]
        assert (sent["type"], sent["strict"], sent["name"]) == ("json_schema", True, schema.name)
        _check_strict(sent["schema"])
        sent = claude._body("system", "user", schema)["output_config"]["format"]
        assert sent["type"] == "json_schema"
        _check_strict(sent["schema"])


def _unions(schema: Any, defs: dict) -> int:
    """The parameters using `anyOf` or a type array, each `$ref` counted where it is used, as
    if its definition were written out there (the docs don't say whether a definition counts
    once, so the higher count)."""
    if isinstance(schema, list):
        return sum(_unions(sub, defs) for sub in schema)
    if not isinstance(schema, dict):
        return 0
    if "$ref" in schema:
        return _unions(defs[schema["$ref"].removeprefix("#/$defs/")], defs)
    own = "anyOf" in schema or isinstance(schema.get("type"), list)
    return own + sum(_unions(sub, defs) for key, sub in schema.items() if key != "$defs")


def test_reply_schemas_keep_anthropics_limit_on_unions():
    """Anthropic's strict output allows at most 16 parameters with union types (`anyOf` or a
    type array) across a request's schemas; one over, and every call is rejected."""
    assert llm.MAX_UNION_PARAMETERS == 16
    for schema in _schemas():
        count = _unions(schema.body, schema.body.get("$defs", {}))
        assert count <= llm.MAX_UNION_PARAMETERS, (schema.name, count)


def test_every_step_default_is_in_the_model_table():
    for model_id in (config.BULK_MODEL, config.REVIEW_MODEL):
        model = llm.model(model_id)
        assert model is config.MODELS[model_id] and model.base_url.endswith("/openai/v1")
    assert config.MODELS["gpt-6-luna"].effort == "low"
    assert config.MODELS["gpt-oss-120b"].reasoning is False


def test_claude_models_and_their_client(monkeypatch):
    haiku, sonnet = config.MODELS["claude-haiku-5-5"], config.MODELS["claude-sonnet-5-5"]
    assert (haiku.backend, haiku.base_url, haiku.fallbacks) == ("anthropic", None, False)
    assert config.MODELS["claude-opus-5-5"].fallbacks and sonnet.fallbacks
    for target in ("claude-sonnet-5", "claude-opus-5", "claude-opus-4-8"):  # their fallbacks
        assert config.MODELS[target].backend == "anthropic"
    monkeypatch.setenv(config.ANTHROPIC_API_KEY, "key")
    monkeypatch.setenv(config.AZURE_KEY_VAR, "key")
    assert isinstance(llm.client_for(config.MODELS["gpt-6-luna"]), llm.OpenAIClient)
    claude = llm.client_for(sonnet, reasoning_effort="low")
    assert isinstance(claude, llm.AnthropicClient)
    body = claude._body("system text", "user text", SCHEMA)
    assert body["system"] == [
        {"type": "text", "text": "system text", "cache_control": {"type": "ephemeral"}}
    ]
    assert body["messages"] == [{"role": "user", "content": "user text"}]
    assert body["output_config"] == {
        "format": {"type": "json_schema", "schema": SCHEMA.body},
        "effort": "low",
    }
    assert (body["betas"], body["fallbacks"]) == (["server-side-fallback-2026-07-01"], "default")
    assert "fallbacks" not in llm.client_for(haiku)._body("s", "u", SCHEMA)


CREDENTIAL_VARIABLES = (
    config.ANTHROPIC_API_KEY,
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_CONFIG_DIR",
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_SERVICE_ACCOUNT_ID",
    "ANTHROPIC_IDENTITY_TOKEN_FILE",
    "ANTHROPIC_IDENTITY_TOKEN",
)


@pytest.mark.parametrize("config_dir", [True, False])
def test_claude_without_credentials_is_an_error(monkeypatch, tmp_path, config_dir):
    for name in CREDENTIAL_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # no `ant auth login` profile on disk
    if config_dir:
        monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "config"))
    with pytest.raises(llm.LLMError, match="no credentials for claude-haiku-5-5: set ANTHROPIC"):
        llm.client_for(config.MODELS["claude-haiku-5-5"])


def test_claude_prices_follow_anthropics_multipliers():
    # Anthropic's pricing page, 2026-11-03: a 5-minute cache write costs 1.25x the input price
    # and a cache read 0.1x, except on Opus 5.5 and Sonnet 5.5, whose cache reads cost 0.05x.
    for model in config.MODELS.values():
        if model.backend != "anthropic":
            continue
        read = 0.05 if model.id in ("claude-opus-5-5", "claude-sonnet-5-5") else 0.1
        tier = [model.long_prompt[1]] if model.long_prompt else []
        for price in [model.price, *tier]:
            assert price.cache_write == pytest.approx(1.25 * price.input), model.id
            assert price.cache_read == pytest.approx(read * price.input), model.id
    assert config.MODELS["claude-haiku-5-5"].long_prompt[0] == 100_000


def test_a_call_counts_as_fallback_served_only_when_answered(tmp_path):
    used = llm.Usage([llm.Attempt("bulk"), llm.Attempt("claude-opus-5", 10, 0, 0, 5)])

    class Client:
        def call(self, system, user, schema):
            if user == "refused by both":
                raise llm.LLMError("refused", usage=used)
            return {}, used

    calls = [llm.Call("a|b", "answered"), llm.Call("a|c", "refused by both")]
    with LabelCache(tmp_path / "labels.sqlite") as cache:
        tally = llm.run_calls(
            calls,
            Client(),
            lambda call, reply, usage: None,
            system="s",
            schema=SCHEMA,
            cache=cache,
            step="label",
            model="bulk",
            prompt_version="label@1",
            total=2,
            workers=1,
        )
    assert (tally.calls, tally.failed, tally.fallback_served) == (2, 1, 1)


def test_claude_attempts_are_priced_by_their_own_model():
    from anthropic.types.beta import BetaUsage

    usage = BetaUsage.model_validate(
        {
            "input_tokens": 300,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 9000,
            "output_tokens": 50,
            # A refusal before any output, then the fallback that served the reply.
            "iterations": [
                {
                    "type": "message",
                    "input_tokens": 300,
                    "cache_read_input_tokens": 9000,
                    "cache_creation_input_tokens": 0,
                    "output_tokens": 0,
                },
                {
                    "type": "fallback_message",
                    "model": "claude-opus-5",
                    "input_tokens": 300,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 9000,
                    "output_tokens": 50,
                },
            ],
        }
    )
    attempts = llm.claude_attempts("claude-opus-5-5", usage)
    assert attempts == [
        llm.Attempt("claude-opus-5-5", 300, 9000, 0, 0),
        llm.Attempt("claude-opus-5", 300, 0, 9000, 50),
    ]
    used = llm.Usage(attempts)
    # $4 in and $0.20 cache reads for Opus 5.5, then $5 in, $6.25 cache writes, $25 out.
    expected = (300 * 4 + 9000 * 0.2) / 1e6 + (300 * 5 + 9000 * 6.25 + 50 * 25) / 1e6
    assert used.model == "claude-opus-5" and used.cost == pytest.approx(expected)
    # Without iterations, the top-level usage is the one attempt.
    usage.iterations = None
    assert llm.claude_attempts("claude-opus-5-5", usage) == [
        llm.Attempt("claude-opus-5-5", 300, 0, 9000, 50)
    ]


def test_an_unknown_model_is_an_error():
    with pytest.raises(llm.LLMError, match="not-a-model is not in config.MODELS"):
        llm.model("not-a-model")


def test_a_call_may_carry_its_own_schema(tmp_path):
    other = llm.Schema("other", llm.strict_object({"x": {"type": "string"}}))
    seen: list[str] = []

    class Client:
        def call(self, system, user, schema):
            seen.append(schema.name)
            return {}, llm.Usage()

    calls = [llm.Call("a|b", "default"), llm.Call("a|c", "own", schema=other)]
    with LabelCache(tmp_path / "labels.sqlite") as cache:
        kw = dict(system="s", cache=cache, step="label", model="m", prompt_version="v", workers=1)
        llm.run_calls(calls, Client(), lambda *a: None, schema=SCHEMA, total=2, **kw)
        assert seen == [SCHEMA.name, "other"]
        with pytest.raises(ValueError, match="no reply schema"):
            llm.run_calls(calls[:1], Client(), lambda *a: None, schema=None, total=1, **kw)


def test_a_prompt_includes_a_shared_file(tmp_path, monkeypatch):
    shared = tmp_path / "skill"
    shared.mkdir()
    (shared / "rules.md").write_text("## Rules\n\nBe strict.\n", encoding="utf-8")
    monkeypatch.setattr(config, "PROMPTS_DIR", shared)
    path = tmp_path / "work" / "label.md"
    path.parent.mkdir()
    path.write_text("# Label\n\n{{include rules.md}}\n\nThe end.\n", encoding="utf-8")
    text, version = llm.load_prompt(path)
    # Found in the prompts directory when it isn't beside the prompt.
    assert text == "# Label\n\n## Rules\n\nBe strict.\n\nThe end.\n"
    # The version hashes the included text too.
    (shared / "rules.md").write_text("## Rules\n\nBe lenient.\n", encoding="utf-8")
    assert llm.load_prompt(path)[1] != version
    # A file beside the prompt wins.
    (path.parent / "rules.md").write_text("Local rules.", encoding="utf-8")
    assert "Local rules." in llm.load_prompt(path)[0]
    path.write_text("{{include missing.md}}\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="missing.md"):
        llm.load_prompt(path)


def test_the_label_prompt_includes_the_claim_rules():
    text = llm.read_prompt(config.LABEL_PROMPT)
    rules = (config.PROMPTS_DIR / "claim-rules.md").read_text(encoding="utf-8").rstrip("\n")
    assert rules in text and "{{include" not in text


def test_prompt_version_tracks_content_and_extra(tmp_path):
    path = tmp_path / "label.md"
    path.write_text("one", encoding="utf-8")
    text, version = llm.load_prompt(path, extra="fmt-1")
    assert text == "one" and version.startswith("label@")
    assert llm.load_prompt(path, extra="fmt-2")[1] != version
    path.write_text("two", encoding="utf-8")
    assert llm.load_prompt(path, extra="fmt-1")[1] != version
