import json
from pathlib import Path

import httpx
import pytest
from careerforge.infrastructure.llm.fake import FakeModel
from careerforge.infrastructure.llm.openai_compatible import (
    OpenAICompatibleProvider,
    parse_response,
    parse_stream,
)
from careerforge.infrastructure.llm.provider import ModelError
from careerforge.schemas.model_io import ModelRequest

FIXTURES = Path(__file__).parents[1] / "fixtures" / "model"
REQUEST = ModelRequest(model="offline-fixture", prompt="contract check")


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name,count", [("text", 0), ("one_tool", 1), ("two_tools", 2)])
async def test_fake_and_adapter_share_contract(name, count):
    payload = fixture(name)
    real_contract = parse_response(payload)
    fake = await FakeModel(payload).complete(REQUEST)
    assert len(fake.tool_calls) == count
    assert fake.synthetic and fake.provider == "fake"
    assert fake.text == real_contract.text
    assert fake.usage == real_contract.usage


def test_unknown_usage_is_not_zero():
    result = parse_response(fixture("missing_usage"))
    assert result.usage.status == "unknown"
    assert result.usage.input_tokens is None
    assert result.usage.output_tokens is None


def test_structured_json():
    result = parse_response(fixture("structured"), structured=True)
    assert result.structured == {"status": "unknown", "evidence_ids": []}


@pytest.mark.parametrize("payload", [{}, {"choices": []}, {"choices": [{}]}, []])
def test_malformed_response_is_safe(payload):
    with pytest.raises(ModelError, match="invalid_response"):
        parse_response(payload)


@pytest.mark.parametrize("arguments", ["not json", "[]", "null", "secret-key"])
def test_bad_tool_arguments_are_rejected(arguments):
    payload = fixture("one_tool")
    payload["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = arguments
    with pytest.raises(ModelError) as error:
        parse_response(payload)
    assert arguments not in str(error.value)


def test_bad_structured_json():
    with pytest.raises(ModelError):
        parse_response(fixture("text"), structured=True)


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (401, "authentication", False),
        (403, "authentication", False),
        (429, "rate_limited", True),
        (503, "unavailable", True),
        (400, "rejected", False),
    ],
)
async def test_http_errors_do_not_leak_secret(status, code, retryable, caplog):
    secret = "sensitive-test-key"
    transport = httpx.MockTransport(lambda _: httpx.Response(status, text=secret))
    async with httpx.AsyncClient(transport=transport) as client:
        provider = OpenAICompatibleProvider(
            base_url="https://offline.test/v1", api_key=secret, client=client
        )
        with pytest.raises(ModelError) as error:
            await provider.complete(REQUEST)
    assert error.value.code == code
    assert error.value.retryable == retryable
    assert secret not in str(error.value) + caplog.text


async def test_timeout_is_billable_unknown():
    def fail(request):
        raise httpx.ReadTimeout("provider-secret", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        provider = OpenAICompatibleProvider(
            base_url="https://offline.test/v1", api_key="secret", client=client
        )
        with pytest.raises(ModelError) as error:
            await provider.complete(REQUEST)
    assert error.value.code == "timeout" and error.value.usage_unknown
    assert "provider-secret" not in str(error.value)


async def test_adapter_success_uses_bounded_request():
    def respond(request):
        body = json.loads(request.content)
        assert body["max_tokens"] == 512
        assert body["model"] == "offline-fixture"
        return httpx.Response(200, json=fixture("text"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await OpenAICompatibleProvider(
            base_url="https://offline.test/v1",
            api_key="secret",
            client=client,
        ).complete(REQUEST)
    assert result.text == "offline text" and not result.synthetic


def stream_lines(*events, done=True):
    lines = ["data: " + json.dumps(event) for event in events]
    return lines + (["data: [DONE]"] if done else [])


def test_stream_text_and_usage():
    result = parse_stream(
        stream_lines(
            {"choices": [{"delta": {"content": "hello"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 1}},
        )
    )
    assert result.text == "hello" and result.usage.status == "known"


@pytest.mark.parametrize("lines", [[], ["data: [DONE]"], ["data: invalid-json"]])
def test_incomplete_stream_rejected(lines):
    with pytest.raises(ModelError):
        parse_stream(lines)


def test_stream_tool_fragments():
    result = parse_stream(
        stream_lines(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "read", "arguments": '{"id":'},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [{"index": 0, "function": {"arguments": '"source"}'}}]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        )
    )
    assert result.tool_calls[0].arguments == {"id": "source"}
    assert result.usage.status == "unknown"
