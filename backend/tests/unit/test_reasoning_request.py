import json

import httpx
import pytest

from careerforge.infrastructure.llm.openai_compatible import OpenAICompatibleProvider
from careerforge.schemas.model_io import ModelRequest


@pytest.mark.parametrize(
    "messages",
    [
        None,
        [
            {"role": "system", "content": "Pi offline rules"},
            {"role": "user", "content": "Inspect a selected file"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "read",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"main.py"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "read", "content": "print(1)"},
        ],
    ],
)
async def test_reasoning_request_preserves_configured_model_and_low(messages):
    def respond(request):
        body = json.loads(request.content)
        assert body["messages"] == (messages or [{"role": "user", "content": "Return JSON"}])
        assert body["tools"][0]["function"]["name"] == "submit_reply"
        assert body["model"] == "supplier-exact-model-id"
        assert body["reasoning_effort"] == "low"
        assert body["max_completion_tokens"] == 2048
        assert "max_tokens" not in body and "temperature" not in body
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(
            200,
            json={
                "id": "offline-contract",
                "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 20},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await OpenAICompatibleProvider(
            base_url="https://offline.test/v1", api_key="offline-placeholder", client=client
        ).complete(
            ModelRequest(
                model="supplier-exact-model-id",
                prompt="Return JSON",
                messages=messages,
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "submit_reply",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
                reasoning_effort="low",
                max_output_tokens=2048,
            )
        )
    assert result.usage.status == "known" and not result.synthetic
