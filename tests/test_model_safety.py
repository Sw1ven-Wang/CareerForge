"""Paid-attempt boundaries exercised using only HTTP MockTransport fixtures."""

from dataclasses import replace

import httpx
import pytest

from careerforge.adapters.model import OpenAICompatibleProvider
from careerforge.config import Settings
from careerforge.contracts import QueryPlan
from careerforge.errors import DomainError


def provider_settings():
    return Settings(
        mode="test",
        model_base_url="https://models.example/v1",
        model_api_key="fixture-not-a-real-key",
        model_name="fixture-model",
        embedding_base_url="https://embed.example/v1",
        embedding_api_key="fixture-key",
        embedding_model="fixture-embedding",
        embedding_dimensions=8,
        rerank_url="https://rank.example/rerank",
        rerank_api_key="fixture-key",
        rerank_model="fixture-rerank",
    )


@pytest.mark.asyncio
async def test_http_failure_retains_only_numeric_status_without_retry():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(504, text="private provider details do-not-leak")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAICompatibleProvider(provider_settings(), client)
        with pytest.raises(DomainError) as error:
            await provider.complete({"role": "planner", "messages": [], "max_output_tokens": 16})
    assert error.value.code == "MODEL_HTTP_ERROR"
    assert error.value.provider_http_status == 504
    assert "do-not-leak" not in str(error.value)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("embedding_base_url", "https://new-embed.example/v1"),
        ("rerank_url", "https://new-rank.example/rerank"),
    ],
)
def test_provider_profile_pins_retrieval_endpoints(field, value):
    settings = provider_settings()
    assert (
        OpenAICompatibleProvider(settings).profile_version
        != OpenAICompatibleProvider(replace(settings, **{field: value})).profile_version
    )


@pytest.mark.parametrize(
    "role,field",
    [
        ("planner", "model_base_url"),
        ("embedding", "embedding_base_url"),
        ("reranker", "rerank_url"),
    ],
)
def test_malformed_endpoint_rejected_before_paid_attempt(role, field):
    provider = OpenAICompatibleProvider(replace(provider_settings(), **{field: "not-an-http-url"}))
    with pytest.raises(DomainError) as error:
        provider.validate_config(role)
    assert error.value.code == "MODEL_NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_model_receipt_binds_data_classification(env):
    app = env["app"]
    identifier = app.runtime.create(
        env["actor"], env["ns"], env["funding"], "research", {}, 2, "classification"
    )
    epoch = app.runtime.claim(identifier, "worker")
    args = (identifier, epoch, "worker", "request", "planner", "p", {}, QueryPlan)
    await app.gateway.call(*args, data_classes=["public_source"])
    with pytest.raises(DomainError) as error:
        await app.gateway.call(*args, data_classes=["learning_state"])
    assert error.value.code == "MODEL_REPLAY_CHANGED"
    assert len(env["model"].calls) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"data": [{"index": 0, "embedding": [0.1] * 8}, {"index": 0, "embedding": [0.2] * 8}]},
        {"data": [{"index": 1, "embedding": [0.1] * 8}, {"index": 2, "embedding": [0.2] * 8}]},
        {"data": [{"index": 0, "embedding": [0.1] * 8}]},
    ],
)
@pytest.mark.asyncio
async def test_embedding_indices_must_cover_exactly_the_requested_inputs(body):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleProvider(provider_settings(), client)
        with pytest.raises(DomainError) as error:
            await provider.complete({"role": "embedding", "context": {"texts": ["a", "b"]}})
    assert error.value.code == "MODEL_RESULT_UNCERTAIN"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "role,context",
    [
        ("embedding", {"texts": ["a"]}),
        ("reranker", {"query": "q", "documents": ["a"]}),
    ],
)
@pytest.mark.asyncio
async def test_malformed_retrieval_response_is_safe_domain_error(role, context):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b'{"untrusted_diagnostic":"do-not-leak"}')
        )
    ) as client:
        provider = OpenAICompatibleProvider(provider_settings(), client)
        with pytest.raises(DomainError) as error:
            await provider.complete({"role": role, "context": context})
    assert error.value.code == "MODEL_RESULT_UNCERTAIN"
    assert "do-not-leak" not in str(error.value)
