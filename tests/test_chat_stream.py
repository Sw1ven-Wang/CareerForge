import json
from dataclasses import replace

import httpx
import pytest
from test_model_safety import provider_settings

from careerforge.adapters.model import OpenAICompatibleProvider
from careerforge.errors import DomainError


def event(delta=None, finish=None, **extra):
    return (
        "data: "
        + json.dumps(
            {
                "id": "reply",
                "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
                **extra,
            }
        )
        + "\n\n"
    )


async def call_stream(data, *, schema=None, status=200):
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(status, text=data, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAICompatibleProvider(
            replace(provider_settings(), model_streaming=True), client
        )
        result = await provider.complete(
            {"role": "reviewer", "messages": [], "max_output_tokens": 128, "schema": schema}
        )
    assert len(calls) == 1 and calls[0]["stream"] is True
    return result


@pytest.mark.asyncio
async def test_complete_json_stream_and_usage():
    data = event({"content": '{"ok":'}) + ": heartbeat\n\n" + event({"content": "true}"}, "stop")
    data += 'data: {"choices":[],"usage":{"total_tokens":12,"private":"secret"}}\n\n'
    result = await call_stream(data + "data: [DONE]\n\n", schema={"type": "object"})
    assert result.structured == {"ok": True}
    assert result.usage == {"total_tokens": 12}


@pytest.mark.asyncio
async def test_tool_call_fragments():
    data = event(
        {
            "tool_calls": [
                {"index": 0, "id": "tool1", "function": {"name": "read", "arguments": '{"path":'}}
            ]
        }
    )
    data += event(
        {"tool_calls": [{"index": 0, "function": {"arguments": '"a.py"}'}}]}, "tool_calls"
    )
    result = await call_stream(data + "data: [DONE]\n\n")
    assert result.tool_calls[0].arguments == {"path": "a.py"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        event({"content": "partial"}),
        event({"content": "partial"}, "stop"),
        event({"content": "truncated"}, "length") + "data: [DONE]\n\n",
        "data: [DONE]\n\n",
        'data: {"error":{"message":"secret"}}\n\n',
    ],
)
async def test_incomplete_or_failed_stream_is_not_success(data):
    with pytest.raises(DomainError) as error:
        await call_stream(data)
    assert error.value.code == "MODEL_RESULT_UNCERTAIN"
    assert "secret" not in str(error.value)


@pytest.mark.asyncio
async def test_stream_http_status_preserved():
    with pytest.raises(DomainError) as error:
        await call_stream("private diagnostic", status=524)
    assert error.value.provider_http_status == 524


def test_streaming_changes_profile_but_default_stays_nonstreaming():
    original = OpenAICompatibleProvider(provider_settings())
    streamed = OpenAICompatibleProvider(replace(provider_settings(), model_streaming=True))
    assert original.profile_version != streamed.profile_version
    request = {"role": "reviewer", "messages": [], "max_output_tokens": 128}
    assert "stream" not in original._chat_body(request)


@pytest.mark.asyncio
async def test_network_disconnect_does_not_accept_partial_or_retry():
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield event({"content": "partial"}).encode()
            raise httpx.ReadError("private upstream details")

    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, stream=Broken(), headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAICompatibleProvider(
            replace(provider_settings(), model_streaming=True), client
        )
        with pytest.raises(DomainError) as error:
            await provider.complete({"role": "reviewer", "messages": [], "max_output_tokens": 128})
    assert error.value.code == "MODEL_RESULT_UNCERTAIN"
    assert "private" not in str(error.value)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_connect_failure_is_distinct_from_uncertain_response():
    def fail(request):
        raise httpx.ConnectError("private endpoint details")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        provider = OpenAICompatibleProvider(
            replace(provider_settings(), model_streaming=True), client
        )
        with pytest.raises(DomainError) as error:
            await provider.complete({"role": "reviewer", "messages": [], "max_output_tokens": 128})
    assert error.value.code == "MODEL_CONNECT_ERROR"
    assert "private endpoint" not in str(error.value)
