"""No-network, lossless representation and paid-attempt boundary regression tests."""

import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy import select

from careerforge.adapters.model import ModelGateway, OpenAICompatibleProvider
from careerforge.adapters.request_contract import (
    CONTEXT_ONCE_CONTRACT,
    MODEL_REQUEST_MAX_BYTES,
    PREFIX,
    check_bytes,
    json_bytes,
    legacy_request,
    pack_request,
    request_context,
    validate_request,
)
from careerforge.config import Settings
from careerforge.contracts import ModelOutput
from careerforge.errors import DomainError
from careerforge.models import AgentRun, FundingAgreement, ModelReceipt
from careerforge.util import canonical, fingerprint


class Result(BaseModel):
    ok: bool


def settings(**kwargs):
    return Settings(
        mode="test",
        model_request_contract=CONTEXT_ONCE_CONTRACT,
        model_base_url="https://fixture.invalid/v1",
        model_api_key="fixture-only",
        model_name="fixture-model",
        **kwargs,
    )


def gateway(**kwargs):
    s = settings(**kwargs)
    provider = OpenAICompatibleProvider(s)
    return ModelGateway(None, provider, s)


def full_authority(request):
    return dict(
        request, funding_source_scope=[], access_closure_hash="a" * 64, privacy_generations={}
    )


def test_default_request_and_profile_are_byte_exact_legacy():
    s = Settings(mode="test")
    provider = OpenAICompatibleProvider(s)
    profile = fingerprint(
        {
            "base_url": "",
            "model": "",
            "roles": {},
            "structured_output": "auto",
            "max_output": 8192,
            "embedding_model": "",
            "embedding_dimensions": 384,
            "rerank_model": "",
            "embedding_base_url": "",
            "rerank_url": "",
        }
    )
    assert provider.profile_version == profile
    g = ModelGateway(None, provider, s)
    context = {"quote": '猫\n\\"', "nested": [{"all": True}]}
    request = g.request_payload("reviewer", "system", context)
    expected = {
        "role": "reviewer",
        "context": context,
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": PREFIX + canonical(context)},
        ],
        "schema": None,
        "tools": [],
        "max_output_tokens": 8192,
        "profile_version": profile,
        "data_classes": ["public_source"],
        "access_dependencies": [],
    }
    assert json_bytes(request) == json_bytes(expected)
    assert fingerprint(request) == fingerprint(expected)
    assert (
        OpenAICompatibleProvider(
            replace(s, model_request_contract=CONTEXT_ONCE_CONTRACT)
        ).profile_version
        != profile
    )


@pytest.mark.parametrize(
    "context",
    [{}, {"utf8": "猫é😀", "quotes": '\\"\r\n'}, {"reads": [{"source": "full", "lines": [1, 2]}]}],
)
def test_lossless_context_binding_and_messages_schema_tools(context):
    g = gateway()
    tools = [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}]
    packed = g.request_payload("reviewer", "System unchanged", context, Result, tools)
    expanded = legacy_request(packed)
    assert request_context(packed) == context
    assert "context" not in packed
    assert packed["context_binding"]["utf8_bytes"] == len(canonical(context).encode())
    assert packed["context_binding"]["sha256"] == fingerprint(context)
    assert packed["messages"] == expanded["messages"]
    assert packed["schema"] == expanded["schema"]
    assert packed["tools"] == expanded["tools"] == tools
    assert pack_request(expanded, generated=True) == packed
    validate_request(full_authority(packed), authority=True)
    context["later"] = "must not mutate prepared request"
    tools[0]["function"]["name"] = "changed"
    assert "later" not in request_context(packed)
    assert packed["tools"][0]["function"]["name"] == "read"


@pytest.mark.parametrize(
    "role,context",
    [("embedding", {"texts": ["full"]}), ("reranker", {"query": "q", "documents": ["full"]})],
)
def test_retrieval_never_deduplicates(role, context):
    request = gateway().request_payload(role, "system", context)
    assert request["context"] == context and "context_binding" not in request
    validate_request(request)


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "user", "content": "unrelated"}],
        [{"role": "system", "content": "s"}, {"role": "user", "content": PREFIX + "{}"}],
        [
            {
                "role": "assistant",
                "tool_calls": [{"id": "1", "function": {"name": "read", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "1", "content": "all tool evidence"},
        ],
    ],
)
def test_custom_messages_are_not_guessed_or_deduplicated(messages):
    request = gateway().request_payload("reviewer", "unused", {}, messages=messages)
    assert request["messages"] == messages and request["context"] == {}
    assert "context_binding" not in request
    validate_request(request)


def test_exact_utf8_envelope_cap_and_plus_one():
    request = full_authority(gateway().request_payload("reviewer", "", {"x": "猫"}))
    spare = MODEL_REQUEST_MAX_BYTES - len(json_bytes(request))
    request["messages"][0]["content"] = "a" * spare
    assert len(json_bytes(request)) == MODEL_REQUEST_MAX_BYTES
    validate_request(request, authority=True)
    request["messages"][0]["content"] += "a"
    with pytest.raises(DomainError, match="合同限制") as exc:
        validate_request(request, authority=True)
    assert exc.value.code == "MODEL_CONTEXT_LIMIT"


@pytest.mark.parametrize(
    "field",
    [
        "schema",
        "tools",
        "messages",
        "data_classes",
        "access_dependencies",
        "funding_source_scope",
        "privacy_generations",
        "access_closure_hash",
    ],
)
def test_all_fields_including_authority_are_budgeted(field):
    request = full_authority(
        gateway().request_payload(
            "reviewer", "s", {}, messages=[{"role": "user", "content": "full"}]
        )
    )
    request[field] = "x" * MODEL_REQUEST_MAX_BYTES
    with pytest.raises(DomainError) as exc:
        validate_request(request, authority=True)
    assert exc.value.code == "MODEL_CONTEXT_LIMIT"


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.update(metadata={"hidden": "anything"}),
        lambda r: r.update(context={}),
        lambda r: r.update(request_contract="unknown"),
        lambda r: r["context_binding"].update(extra="unbudgeted"),
        lambda r: r["context_binding"].update(message_index=True),
        lambda r: r["context_binding"].update(utf8_bytes=True),
        lambda r: r["context_binding"].update(sha256="0" * 64),
        lambda r: r["messages"][1].update(content=PREFIX + '{ "x":1}'),
        lambda r: r["messages"].append({"role": "tool", "content": "unexpected"}),
        lambda r: r.update(role="embedding"),
    ],
)
def test_invalid_or_ambiguous_bindings_fail_closed(change):
    request = gateway().request_payload("reviewer", "s", {"x": 1})
    change(request)
    with pytest.raises(DomainError) as exc:
        validate_request(request)
    assert exc.value.code == "MODEL_REQUEST_CONTRACT"


def test_noncanonical_duplicate_keys_and_nonfinite_fail_closed():
    request = gateway().request_payload("reviewer", "s", {"x": 1})
    import hashlib

    raw = b'{"x":1,"x":1}'
    request["messages"][1]["content"] = PREFIX + raw.decode()
    request["context_binding"].update(sha256=hashlib.sha256(raw).hexdigest(), utf8_bytes=len(raw))
    with pytest.raises(DomainError):
        validate_request(request)
    with pytest.raises(DomainError):
        gateway().request_payload("reviewer", "s", {"x": float("nan")})


@pytest.mark.parametrize(
    "provider",
    [
        SimpleNamespace(profile_version="unsupported"),
        SimpleNamespace(profile_version="unsupported", request_contract=CONTEXT_ONCE_CONTRACT),
    ],
)
def test_unsupported_sdk_fails_before_request(provider):
    with pytest.raises(DomainError) as exc:
        ModelGateway(None, provider, settings())
    assert exc.value.code == "MODEL_REQUEST_CONTRACT"


@pytest.mark.parametrize("mode", ["auto", "json_schema", "json_object"])
async def test_http_actual_wire_is_exact_measured_bytes_and_legacy_equivalent(mode):
    seen = []

    def response(request):
        seen.append(request.content)
        assert request.headers["content-type"] == "application/json"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        s = settings(structured_output=mode)
        provider = OpenAICompatibleProvider(s, client)
        g = ModelGateway(None, provider, s)
        request = g.request_payload("reviewer", "s", {"all_evidence": "猫\n"}, Result)
        measured = provider.request_wire_bytes(request)
        await provider.complete(request)
        legacy = OpenAICompatibleProvider(replace(s, model_request_contract="legacy-v1"), client)
        await legacy.complete(legacy_request(request))
    assert seen == [measured, measured]
    if mode == "json_object":
        assert json.loads(measured)["messages"][-1]["content"].endswith(
            canonical(request["schema"])
        )


async def test_schema_fallback_expansion_bounded_before_any_http():
    g = gateway(structured_output="json_object")
    request = g.request_payload("reviewer", "s", {}, Result)
    # JSON embedding in the schema fallback can exceed the actual wire cap even
    # though the audit envelope fits. The schema must not hide expansion costs.
    request["schema"] = {"type": "object", "description": '"' * 110_000}
    assert len(json_bytes(request)) < MODEL_REQUEST_MAX_BYTES
    with pytest.raises(DomainError) as exc:
        g.provider.validate_request(request)
    assert exc.value.code == "MODEL_CONTEXT_LIMIT"


@pytest.mark.parametrize(
    "role,context",
    [("embedding", {"texts": ["猫"]}), ("reranker", {"query": "q", "documents": ["猫"]})],
)
async def test_retrieval_exact_wire_and_budget(role, context):
    seen = []

    def response(request):
        seen.append(request.content)
        return httpx.Response(
            200,
            json={
                "data": [{"index": 0, "embedding": [0.1] * 8}],
                "results": [{"index": 0, "relevance_score": 0.5}],
            },
        )

    s = settings(
        embedding_base_url="https://fixture.invalid",
        embedding_api_key="fixture",
        embedding_model="embed",
        embedding_dimensions=8,
        rerank_url="https://fixture.invalid",
        rerank_api_key="fixture",
        rerank_model="rank",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        provider = OpenAICompatibleProvider(s, client)
        request = ModelGateway(None, provider, s).request_payload(role, "s", context)
        expected = provider.request_wire_bytes(request)
        await provider.complete(request)
    assert seen == [expected]
    provider = OpenAICompatibleProvider(
        replace(s, **{"embedding_model" if role == "embedding" else "rerank_model": "x" * 250_000})
    )
    request["profile_version"] = provider.profile_version
    with pytest.raises(DomainError) as exc:
        provider.validate_request(request)
    assert exc.value.code == "MODEL_CONTEXT_LIMIT"


class ContractFixture:
    synthetic = True
    request_contract = CONTEXT_ONCE_CONTRACT
    profile_version = "fixture-context-once-v1"

    def __init__(self):
        self.calls = []
        self.reject_wire = False

    def validate_request(self, request):
        validate_request(request)
        if self.reject_wire:
            check_bytes(b"x" * (MODEL_REQUEST_MAX_BYTES + 1))

    async def complete(self, request):
        self.calls.append(deepcopy(request))
        return ModelOutput(text="offline fixture", synthetic=True)


def attach(env):
    provider = ContractFixture()
    g = ModelGateway(env["app"].runtime, provider, settings())
    return g, provider


def start(env, provider, profile=True):
    app = env["app"]
    manifest = {"model_profile": provider.profile_version} if profile else {}
    identifier = app.runtime.create(
        env["actor"], env["ns"], env["funding"], "research", manifest, 3, "context-once"
    )
    return identifier, app.runtime.claim(identifier, "worker")


def usage(env, identifier):
    with env["app"].db.read() as db:
        run = db.get(AgentRun, identifier)
        funding = db.get(FundingAgreement, env["funding"])
        return (
            run.calls_consumed,
            run.held_calls,
            funding.calls_consumed,
            funding.calls_held,
            len(list(db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == identifier)))),
        )


async def test_new_contract_exact_replay_and_changed_context_refusal(env):
    g, provider = attach(env)
    identifier, epoch = start(env, provider)
    args = (identifier, epoch, "worker", "request", "reviewer", "s")
    first = await g.call(*args, {"all_evidence": "full"})
    before = usage(env, identifier)
    assert await g.call(*args, {"all_evidence": "full"}) == first
    assert usage(env, identifier) == before and len(provider.calls) == 1
    assert "context_binding" in provider.calls[0]
    with pytest.raises(DomainError) as exc:
        await g.call(*args, {"all_evidence": "changed"})
    assert exc.value.code == "MODEL_REPLAY_CHANGED"
    assert usage(env, identifier) == before


@pytest.mark.parametrize("failure", ["unbound", "envelope", "wire", "authority"])
async def test_preflight_failure_has_no_receipt_or_budget_charge(env, failure, monkeypatch):
    g, provider = attach(env)
    identifier, epoch = start(env, provider, profile=failure != "unbound")
    before = usage(env, identifier)
    context = {"data": "x" * 250_000} if failure == "envelope" else {}
    provider.reject_wire = failure == "wire"
    if failure == "authority":
        from careerforge.adapters import model

        original = model.active_funding

        def inflated(db, run):
            funding = original(db, run)
            return SimpleNamespace(id=funding.id, allowed_source_ids=["x" * 250_000])

        monkeypatch.setattr(model, "active_funding", inflated)
        monkeypatch.setattr(model, "assert_source_scope", lambda *args: None)
    with pytest.raises(DomainError) as exc:
        await g.call(identifier, epoch, "worker", "request", "reviewer", "s", context)
    assert exc.value.code == (
        "MODEL_PROFILE_CHANGED" if failure == "unbound" else "MODEL_CONTEXT_LIMIT"
    )
    assert usage(env, identifier) == before and provider.calls == []


async def test_old_paid_receipt_cannot_migrate_to_new_contract(env):
    from careerforge.contracts import QueryPlan

    app = env["app"]
    identifier = app.runtime.create(
        env["actor"],
        env["ns"],
        env["funding"],
        "research",
        {"model_profile": env["model"].profile_version},
        3,
        "old-receipt",
    )
    epoch = app.runtime.claim(identifier, "worker")
    args = (identifier, epoch, "worker", "request", "planner", "s", {}, QueryPlan)
    original = await app.gateway.call(*args)
    before = usage(env, identifier)
    g, provider = attach(env)
    with pytest.raises(DomainError) as exc:
        await g.call(*args)
    assert exc.value.code == "MODEL_PROFILE_CHANGED"
    assert provider.calls == [] and usage(env, identifier) == before
    assert await app.gateway.call(*args) == original
    assert usage(env, identifier) == before


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_legacy_planner_size_decision_unchanged_at_threshold(delta):
    request = gateway().request_payload("learning_planner", "", {})
    expanded = legacy_request(request)
    spare = MODEL_REQUEST_MAX_BYTES + delta - len(json_bytes(expanded))
    request["messages"][0]["content"] = "x" * spare
    before = legacy_request(request)
    assert len(json_bytes(before)) == MODEL_REQUEST_MAX_BYTES + delta
    assert (len(json_bytes(legacy_request(request))) > MODEL_REQUEST_MAX_BYTES) == (delta > 0)
    # A transport-only branch would incorrectly choose the old business prompt.
    assert len(json_bytes(request)) < MODEL_REQUEST_MAX_BYTES + 300


def test_provider_config_snapshot_and_current_profile_fence():
    s = settings(role_models={"reviewer": "original"})
    provider = OpenAICompatibleProvider(s)
    g = ModelGateway(None, provider, s)
    request = g.request_payload("reviewer", "s", {})
    before = provider.request_wire_bytes(request)
    s.role_models["reviewer"] = "mutated external mapping"
    provider.validate_request(request)
    assert provider.request_wire_bytes(request) == before
    provider.role_models["reviewer"] = "mutated provider mapping"
    with pytest.raises(DomainError) as exc:
        provider.validate_request(request)
    assert exc.value.code == "MODEL_REQUEST_CONTRACT"


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_name", "new-model"),
        ("structured_output", "json_object"),
        ("max_output_tokens", 4096),
        ("model_request_contract", "legacy-v1"),
    ],
)
def test_provider_settings_replacement_rejected(field, value):
    g = gateway()
    request = g.request_payload("reviewer", "s", {})
    g.provider.settings = replace(g.provider.settings, **{field: value})
    with pytest.raises(DomainError) as exc:
        g.provider.validate_request(request)
    assert exc.value.code == "MODEL_REQUEST_CONTRACT"


async def test_new_unknown_receipt_is_never_redispatched_or_recharged(env):
    g, provider = attach(env)
    identifier, epoch = start(env, provider)

    async def uncertain(request):
        provider.calls.append(deepcopy(request))
        raise DomainError("MODEL_RESULT_UNCERTAIN", "offline uncertain fixture")

    provider.complete = uncertain
    args = (identifier, epoch, "worker", "request", "reviewer", "s", {})
    with pytest.raises(DomainError):
        await g.call(*args)
    before = usage(env, identifier)
    with env["app"].db.read() as db:
        assert (
            db.scalar(select(ModelReceipt).where(ModelReceipt.run_id == identifier)).status
            == "unknown"
        )
    with pytest.raises(DomainError) as exc:
        await g.call(*args)
    assert exc.value.code == "MODEL_RESULT_UNCERTAIN"
    assert len(provider.calls) == 1 and usage(env, identifier) == before


@pytest.mark.parametrize("denial", ["role", "data_class", "dependency"])
async def test_new_contract_authority_denial_never_dispatches_or_charges(env, denial):
    g, provider = attach(env)
    identifier, epoch = start(env, provider)
    with env["app"].db.transaction() as db:
        funding = db.get(FundingAgreement, env["funding"])
        if denial == "role":
            funding.allowed_roles = ["planner"]
        if denial == "data_class":
            funding.allowed_data_classes = ["learning_state"]
    before = usage(env, identifier)
    kwargs = {"access_dependencies": ["nonexistent-evidence"]} if denial == "dependency" else {}
    with pytest.raises(DomainError):
        await g.call(identifier, epoch, "worker", "request", "reviewer", "s", {}, **kwargs)
    assert provider.calls == [] and usage(env, identifier) == before


@pytest.mark.parametrize("delta", [-1, 0, 1])
async def test_actual_planner_admission_keeps_legacy_threshold_branch(env, monkeypatch, delta):
    from conftest import publish_fixture
    from test_learning_loop_v16 import plan_input

    from careerforge.services.planner_context_v166 import (
        BOUNDED_PLANNER_CONTEXT_VERSION,
        PLANNER_CONTEXT_VERSION,
    )

    app = env["app"]
    _, publication, _ = await publish_fixture(env)
    # Controlled size at the payload-construction seam; the actual admission
    # service must make the same prompt/version choice for each representation.
    original_payload = app.gateway.request_payload
    original = original_payload("learning_planner", "", {"evidence": "x" * 60_000})
    original["messages"][0]["content"] = "s" * (
        MODEL_REQUEST_MAX_BYTES + delta - len(json_bytes(original))
    )
    packed = pack_request(original, generated=True)
    assert len(json_bytes(packed)) < MODEL_REQUEST_MAX_BYTES
    choices = []
    for index, payload in enumerate([original, packed]):
        monkeypatch.setattr(
            app.gateway, "request_payload", lambda *a, payload=payload, **k: payload
        )
        admitted = app.learning.start_personalized_plan(
            env["actor"], plan_input(env, publication, key=f"threshold-{index}")
        )
        with app.db.read() as db:
            run = db.get(AgentRun, admitted["run_id"])
            choices.append(run.input_manifest["planner_model_context_version"])
    expected = BOUNDED_PLANNER_CONTEXT_VERSION if delta > 0 else PLANNER_CONTEXT_VERSION
    assert choices == [expected, expected]


def test_http_request_output_cap_matches_profile_even_with_different_gateway_settings():
    provider = OpenAICompatibleProvider(settings())
    g = ModelGateway(None, provider, settings(max_output_tokens=4096))
    request = g.request_payload("reviewer", "s", {})
    with pytest.raises(DomainError) as exc:
        provider.validate_request(request)
    assert exc.value.code == "MODEL_REQUEST_CONTRACT"


async def test_output_cap_profile_mismatch_never_reserves_receipt(env):
    provider = OpenAICompatibleProvider(settings())
    g = ModelGateway(env["app"].runtime, provider, settings(max_output_tokens=4096))
    identifier, epoch = start(env, provider)
    before = usage(env, identifier)
    with pytest.raises(DomainError) as exc:
        await g.call(identifier, epoch, "worker", "request", "reviewer", "s", {})
    assert exc.value.code == "MODEL_REQUEST_CONTRACT"
    assert usage(env, identifier) == before


@pytest.mark.parametrize("delta", [0, 1])
def test_actual_http_wire_exact_cap_and_plus_one(delta):
    s = settings()
    provider = OpenAICompatibleProvider(s)
    request = ModelGateway(None, provider, s).request_payload("reviewer", "s", {})
    overhead = len(provider.request_wire_bytes(request)) - len(s.model_name)
    provider = OpenAICompatibleProvider(replace(s, model_name="x" * (250_000 + delta - overhead)))
    request["profile_version"] = provider.profile_version
    assert len(json_bytes(request)) < 1_000
    if delta:
        with pytest.raises(DomainError) as exc:
            provider.validate_request(request)
        assert exc.value.code == "MODEL_CONTEXT_LIMIT"
    else:
        provider.validate_request(request)
        assert len(provider.request_wire_bytes(request)) == 250_000


async def test_full_synthetic_build_preserves_review_and_publication_receipt_consumers(
    env, tmp_path
):
    from conftest import publish_fixture
    from support import FixtureModel, FixtureRepositories

    from careerforge.application import Application

    class PackedFixture(FixtureModel):
        request_contract = CONTEXT_ONCE_CONTRACT
        profile_version = "synthetic-packed-build-v1"

        def __init__(self):
            super().__init__()
            self.packed_calls = []

        def validate_request(self, request):
            validate_request(request)

        async def complete(self, request):
            self.packed_calls.append(deepcopy(request))
            # Local synthetic fixture execution only. Native workers NEVER see
            # this reconstructed duplicate representation.
            return await super().complete(legacy_request(request))

    provider = PackedFixture()
    s = replace(
        env["app"].settings,
        model_request_contract=CONTEXT_ONCE_CONTRACT,
        database_url="sqlite+pysqlite:///" + str(tmp_path / "packed-build.db"),
        state_dir=tmp_path / "packed-state",
    )
    app = Application(s, provider, FixtureRepositories())
    owner = app.bootstrap("packed-fixture")
    funded = app.runtime.create_funding(owner["principal_id"], owner["namespace_id"], 100)
    test_env = dict(
        app=app,
        actor=owner["principal_id"],
        ns=owner["namespace_id"],
        funding=funded,
        model=provider,
    )
    try:
        _, publication, _ = await publish_fixture(test_env)
        assert publication
        assert any(r["role"] == "analysis" for r in provider.packed_calls)
        assert any(r["role"] == "reviewer" for r in provider.packed_calls)
        assert all("context_binding" in r and "context" not in r for r in provider.packed_calls)
        with app.db.read() as db:
            receipts = list(db.scalars(select(ModelReceipt)))
            assert receipts and all(r.status == "completed" for r in receipts)
            assert all(r.profile_version == provider.profile_version for r in receipts)
    finally:
        app.db.engine.dispose()
