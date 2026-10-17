import json

import httpx
from careerforge.infrastructure.llm.openai_compatible import OpenAICompatibleProvider
from careerforge.schemas.model_io import ModelRequest


async def test_reasoning_request_preserves_configured_model_and_low():
    def respond(request):
        body = json.loads(request.content)
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
                reasoning_effort="low",
                max_output_tokens=2048,
            )
        )
    assert result.usage.status == "known" and not result.synthetic
