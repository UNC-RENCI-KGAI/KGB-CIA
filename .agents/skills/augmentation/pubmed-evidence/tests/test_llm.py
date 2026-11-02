"""llm.py: model lookup, the Responses API client and reply parsing, all without network."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pubmed_evidence import config, llm

MODEL = llm.ModelInfo(
    id="gpt-test",
    base_url="https://example.test/openai/v1",
    reasoning=True,
    input_cost=0.2,
    output_cost=1.2,
    cache_read_cost=0.02,
    max_tokens=128000,
)


def reply(text: str, *, usage: dict | None = None, **extra) -> dict:
    return {
        "id": "resp_1",
        "status": "completed",
        "output": [
            {"type": "reasoning", "summary": []},
            {
                "type": "message",
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
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        r = self.responses.pop(0)
        return r if isinstance(r, httpx.Response) else httpx.Response(200, json=r)

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(llm, "_sleep", waits.append)
    return waits


def client(server: Server, model: llm.ModelInfo = MODEL, **kw) -> llm.Client:
    return llm.Client(model, "secret", transport=httpx.MockTransport(server), **kw)


def test_parses_output_text_and_sends_the_request():
    server = Server(reply('{"labels": [], "proposals": []}'))
    data, usage = client(server).complete_json(
        "system text", "user text", max_output_tokens=500, reasoning_effort="low"
    )
    assert data == {"labels": [], "proposals": []}
    request = server.requests[0]
    assert str(request.url) == "https://example.test/openai/v1/responses"
    assert request.headers["api-key"] == "secret"
    body = server.bodies()[0]
    assert body["model"] == "gpt-test"
    assert body["instructions"] == "system text"
    assert body["input"] == "user text"
    assert body["max_output_tokens"] == 500
    assert body["reasoning"] == {"effort": "low"}
    assert body["text"] == {"format": {"type": "json_object"}}
    assert (usage.input_tokens, usage.cached_tokens, usage.output_tokens) == (1000, 400, 100)
    # (600 * 0.2 + 400 * 0.02 + 100 * 1.2) / 1e6
    assert usage.cost == pytest.approx(248 / 1e6)
    assert usage.seconds >= 0


def test_json_in_code_fences_and_prose():
    text = 'Here are the labels:\n```json\n{"labels": [{"statement": "S1"}]}\n```\nDone.'
    data, _ = client(Server(reply(text))).complete_json("s", "u")
    assert data == {"labels": [{"statement": "S1"}]}


def test_top_level_output_text():
    data = {"status": "completed", "output": [], "output_text": '{"a": 1}', "usage": {}}
    out, usage = client(Server(data)).complete_json("s", "u")
    assert out == {"a": 1}
    assert usage.input_tokens == 0


@pytest.mark.parametrize(
    "text, expected",
    [
        ('{"a": 1}', {"a": 1}),
        ('```\n{"a": 1}\n```', {"a": 1}),
        ('The answer is {"a": {"b": 2}} as requested.', {"a": {"b": 2}}),
        ('First {"a": 1} and then {"b": 2}', {"a": 1}),
    ],
)
def test_parse_json_object(text, expected):
    assert llm.parse_json_object(text) == expected


@pytest.mark.parametrize("text", ["", "no json here", "[1, 2]", '{"a": '])
def test_parse_json_object_fails(text):
    with pytest.raises(ValueError):
        llm.parse_json_object(text)


def test_reasoning_omitted_without_effort_or_for_non_reasoning_models():
    server = Server(reply("{}"), reply("{}"))
    client(server).complete_json("s", "u")
    plain = llm.ModelInfo(id="gpt-plain", base_url="https://example.test/v1")
    client(server, plain).complete_json("s", "u", reasoning_effort="low")
    assert all("reasoning" not in body for body in server.bodies())


def test_max_output_tokens_capped_at_the_models_limit():
    small = llm.ModelInfo(id="m", base_url="https://example.test/v1", max_tokens=100)
    server = Server(reply("{}"))
    client(server, small).complete_json("s", "u", max_output_tokens=16000)
    assert server.bodies()[0]["max_output_tokens"] == 100


def test_retries_429_honoring_retry_after(no_sleep):
    server = Server(
        httpx.Response(429, headers={"retry-after": "7"}, json={"error": {"message": "slow"}}),
        httpx.Response(503, json={"error": {"message": "busy"}}),
        reply('{"ok": true}'),
    )
    data, _ = client(server).complete_json("s", "u")
    assert data == {"ok": True}
    assert len(server.requests) == 3
    assert 7 <= no_sleep[0] <= 8
    assert len(no_sleep) == 2


def test_gives_up_after_max_retries():
    server = Server(*[httpx.Response(500, json={"error": {"message": "down"}})] * 3)
    with pytest.raises(llm.LLMError, match="HTTP 500 after 3 attempts"):
        client(server, max_retries=2).complete_json("s", "u")
    assert len(server.requests) == 3


def test_retries_timeouts():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json=reply("{}"))

    c = llm.Client(MODEL, "secret", transport=httpx.MockTransport(handler))
    assert c.complete_json("s", "u")[0] == {}
    assert len(calls) == 2


def test_other_httpx_errors_become_llm_errors():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.DecodingError("bad gzip", request=request)

    c = llm.Client(MODEL, "secret", transport=httpx.MockTransport(handler))
    with pytest.raises(llm.LLMError, match="DecodingError"):
        c.complete_json("s", "u")
    assert len(calls) == 1


def test_json_format_rejected_is_retried_without_and_remembered():
    rejected = httpx.Response(
        400,
        json={
            "error": {
                "code": "invalid_request",
                "param": "text.format",
                "message": "Response format json_object is not supported by this model.",
            }
        },
    )
    server = Server(rejected, reply("{}"), reply("{}"))
    c = client(server)
    c.complete_json("s", "u")
    c.complete_json("s", "u")
    bodies = server.bodies()
    assert "text" in bodies[0]
    assert "text" not in bodies[1] and "text" not in bodies[2]
    assert c.json_format is False


def test_reasoning_rejected_is_retried_without():
    rejected = httpx.Response(
        400, json={"error": {"message": "Unsupported parameter: 'reasoning.effort'."}}
    )
    server = Server(rejected, reply("{}"))
    client(server).complete_json("s", "u", reasoning_effort="low")
    assert "reasoning" in server.bodies()[0] and "reasoning" not in server.bodies()[1]


def test_other_client_errors_fail_at_once():
    server = Server(httpx.Response(401, json={"error": {"message": "bad key"}}))
    with pytest.raises(llm.LLMError, match="HTTP 401: bad key"):
        client(server).complete_json("s", "u")
    assert len(server.requests) == 1


def test_incomplete_response_raises_with_its_usage():
    data = reply("{")
    data["status"] = "incomplete"
    data["incomplete_details"] = {"reason": "max_output_tokens"}
    with pytest.raises(llm.LLMError, match="max_output_tokens") as e:
        client(Server(data)).complete_json("s", "u")
    assert e.value.usage.output_tokens == 100
    assert e.value.usage.cost is not None


def test_unparseable_json_retried_once():
    server = Server(reply("not json"), reply('{"a": 1}'))
    data, usage = client(server).complete_json("s", "u")
    assert data == {"a": 1}
    assert usage.input_tokens == 2000  # both attempts count
    server = Server(reply("not json"), reply("still not json"))
    with pytest.raises(llm.LLMError, match="no JSON object"):
        client(server).complete_json("s", "u")


def test_missing_key(monkeypatch):
    monkeypatch.delenv(config.LLM_API_KEY, raising=False)
    with pytest.raises(llm.LLMError, match=config.LLM_API_KEY):
        llm.Client(MODEL)


def test_key_from_environment(monkeypatch):
    monkeypatch.setenv(config.LLM_API_KEY, "from-env")
    server = Server(reply("{}"))
    llm.Client(MODEL, transport=httpx.MockTransport(server)).complete_json("s", "u")
    assert server.requests[0].headers["api-key"] == "from-env"


def test_cost_unknown():
    model = llm.ModelInfo(id="m", base_url="https://example.test/v1")
    _, usage = client(Server(reply("{}")), model).complete_json("s", "u")
    assert usage.cost is None
    assert llm.cost_of(model, 10, 10) is None


def write_models(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "providers": {
                    "other": {"baseUrl": "https://other.test", "models": [{"id": "m1"}]},
                    config.LLM_PROVIDER: {
                        "baseUrl": "https://renci.test/openai/v1/",
                        "api": "azure-openai-responses",
                        "models": [
                            {
                                "id": "m1",
                                "reasoning": True,
                                "cost": {"input": 0.2, "output": 1.2, "cacheRead": 0.02},
                                "contextWindow": 1050000,
                                "maxTokens": 128000,
                            }
                        ],
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def test_load_model(tmp_path, monkeypatch):
    path = write_models(tmp_path / "models.json")
    monkeypatch.setattr(config, "PI_MODELS", [tmp_path / "missing.json", path])
    model = llm.load_model("m1")
    assert model == llm.ModelInfo(
        id="m1",
        base_url="https://renci.test/openai/v1",
        api="azure-openai-responses",
        reasoning=True,
        input_cost=0.2,
        output_cost=1.2,
        cache_read_cost=0.02,
        max_tokens=128000,
        context_window=1050000,
    )
    assert llm.load_model("m1", base_url="https://override.test/v1").base_url == (
        "https://override.test/v1"
    )


def test_load_model_unknown_falls_back(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "PI_MODELS", [write_models(tmp_path / "models.json")])
    model = llm.load_model("gpt-6-luna")
    assert model.base_url == config.LLM_BASE_URL
    assert model.input_cost is None and model.output_cost is None
    assert "gpt-6-luna" in capsys.readouterr().err


def test_load_model_from_the_repository():
    model = llm.load_model(config.BULK_MODEL)
    assert model.input_cost is not None and model.base_url.endswith("/openai/v1")


def test_prompt_version_tracks_content_and_extra(tmp_path):
    path = tmp_path / "label.md"
    path.write_text("one", encoding="utf-8")
    text, version = llm.load_prompt(path, extra="fmt-1")
    assert text == "one" and version.startswith("label@")
    assert llm.load_prompt(path, extra="fmt-2")[1] != version
    path.write_text("two", encoding="utf-8")
    assert llm.load_prompt(path, extra="fmt-1")[1] != version
