"""Mechanism regressions, with synthetic models and normally reviewed fixtures.

These test authorization, routing and recall plumbing, not live semantic quality.
"""

from __future__ import annotations

import pytest
from conftest import publish_fixture

from careerforge.contracts import IntentDecision, ModelOutput, RetrievalTaskSpec, TaskType
from careerforge.errors import DomainError
from careerforge.models import AgentRun, ObjectRow
from careerforge.services.orchestration import _intent_model_schema, rule_intent
from careerforge.services.security import create_principal
from careerforge.services.taxonomy import resolve_topics, term_matches

POLICIES = {
    "routing_policy": {"policy_id": "mechanism-test", "content": {"ambiguity_threshold": 0.6}}
}


def proposal(**updates):
    return {
        "interpreted_intent": "explain_implementation",
        "confidence": 0.95,
        "proposed_next_action": "retrieve_and_tutor",
        **updates,
    }


def active_run(env, key, *, roles=None, cap=2):
    app, actor = env["app"], env["actor"]
    funding = app.runtime.create_funding(actor, env["ns"], 10, roles=roles)
    run = app.runtime.create(actor, env["ns"], funding, "research", {}, cap, key)
    epoch = app.runtime.claim(run, "scope-tests")
    assert epoch is not None
    return run, epoch, "scope-tests"


def spec(**updates):
    return RetrievalTaskSpec.model_validate(
        {
            "turn_id": "mechanism",
            "task_type": "explain_implementation",
            "user_question": "Explain echo and cache boundaries",
            "focus_revision": 1,
            **updates,
        }
    )


@pytest.mark.parametrize(
    "question",
    [
        "Do not compare the projects; explain the call instead.",
        "Don't verify this claim, just locate its definition.",
        "Never import a job description automatically.",
        "不要比较这些项目，解释边界即可。",
        "不验证这条证据，只定位函数。",
        'Explain why the example contains "job description".',
        "请解释「岗位」这个字符串的用途。",
        "Explain the identifier `compare`.",
        "Compare the projects and locate the handler.",
        "Verify this claim, then build a learning plan.",
        "先定位函数，再解释实现。",
        "Verify the claim, then write and run a fix.",
        "Locate the endpoint and edit its configuration.",
        "Compare these APIs; next deploy the change.",
        "A learning plan, then create a branch.",
        "解释调用链，然后运行测试。",
        "证据在哪里，并修改配置。",
    ],
)
@pytest.mark.parametrize(
    "focus", [{}, {"topic_keys": ["code.python"], "project_source_ids": ["x"]}]
)
def test_rule_abstains_on_negation_quotes_and_multiple_actions(question, focus):
    assert rule_intent(question, focus) is None


@pytest.mark.parametrize(
    "question,task",
    [
        ("Compare the projects", TaskType.COMPARE),
        ("Locate the handler", TaskType.LOCATE),
        ("Verify the claim", TaskType.VERIFY),
        ("Explain architecture", TaskType.ARCHITECTURE),
        ("给我学习计划", TaskType.PLAN),
        ("帮我实现这个练习", TaskType.PRACTICE),
        ("证据错误", TaskType.DISPUTE),
        ("讲解Tool Calling", TaskType.IMPLEMENTATION),
    ],
)
def test_clear_single_requests_keep_fast_path(question, task):
    assert rule_intent(question, {}).interpreted_intent == task


def test_focus_only_resolves_continuations_and_policy_rules_use_boundaries():
    focus = {"topic_keys": ["code.python"]}
    assert rule_intent("Continue with this", focus).interpreted_intent == TaskType.IMPLEMENTATION
    assert rule_intent("How does a parking ticket appeal work?", focus) is None
    policy = {"keyword_rules": [{"keyword": "cache", "task_type": "teach_concept"}]}
    assert rule_intent("TTLCache", {}, policy) is None
    assert rule_intent("not cache", focus, policy) is None
    assert rule_intent("cache", {}, policy).interpreted_intent == TaskType.CONCEPT


@pytest.mark.parametrize(
    "text,term",
    [
        ("TTLCache", "cache"),
        ("assertion", "assert"),
        ("queued", "queue"),
        ("governance", "go"),
        ("cache_key", "cache"),
    ],
)
def test_english_taxonomy_terms_are_not_identifier_substrings(text, term):
    assert not term_matches(text, term)
    assert term_matches("use " + term + " here", term)


def test_taxonomy_boundaries_and_legacy_intent_defaults(env):
    topics = resolve_topics(env["app"].db, "TTLCache assertion queued", env["actor"])
    assert not {"storage.redis", "code.testing", "runtime.queue"} & set(topics)
    assert "storage.redis" in resolve_topics(env["app"].db, "cache", env["actor"])
    legacy = IntentDecision.model_validate(proposal(topic_keys=["code.python"]))
    assert legacy.retrieval_topic_keys is None and legacy.retrieval_scope_version is None
    assert "retrieval_topic_keys" not in _intent_model_schema()["properties"]
    assert "topic_hints" not in _intent_model_schema()["properties"]


@pytest.mark.asyncio
async def test_inference_and_model_proposals_cannot_replace_explicit_topics(env):
    app, actor = env["app"], env["actor"]
    hard = {"topic_keys": ["code.python"]}
    intent, decision = await app.coordinator.decide(
        actor, "Explain cache behavior", hard, 1, POLICIES, None
    )
    assert decision.action == "invoke_tutor"
    assert intent.topic_keys == intent.retrieval_topic_keys == ["code.python"]
    assert "storage.redis" in intent.topic_hints
    env["model"].overrides["coordinator"] = lambda _: ModelOutput(
        structured=proposal(topic_keys=["storage.redis"], retrieval_topic_keys=["storage.redis"]),
        synthetic=True,
    )
    intent, _ = await app.coordinator.decide(
        actor,
        "Do not compare; explain cache behavior",
        hard,
        1,
        POLICIES,
        active_run(env, "model-hint"),
    )
    assert intent.topic_keys == intent.retrieval_topic_keys == ["code.python"]
    assert "storage.redis" in intent.topic_hints
    # Persisted display topics must not be silently promoted into hard scope.
    followup, _ = await app.coordinator.decide(
        actor,
        "Explain this",
        {
            "topic_keys": ["storage.redis"],
            "retrieval_topic_keys": [],
            "topic_hints": ["storage.redis"],
        },
        2,
        POLICIES,
        None,
    )
    assert followup.retrieval_topic_keys == [] and followup.topic_hints == ["storage.redis"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        'Explain "job description" as text',
        "Do not verify; explain the behavior",
        "Compare projects and locate a function",
    ],
)
async def test_ambiguous_rule_uses_actual_coordinator_or_clarifies_without_budget(env, question):
    app, actor = env["app"], env["actor"]
    env["model"].overrides["coordinator"] = lambda _: ModelOutput(
        structured=proposal(), synthetic=True
    )
    before = len(env["model"].calls)
    intent, decision = await app.coordinator.decide(
        actor, question, {"topic_keys": ["code.python"]}, 1, POLICIES, active_run(env, "fallback")
    )
    assert (
        intent.interpreted_intent == TaskType.IMPLEMENTATION and decision.action == "invoke_tutor"
    )
    assert [call["role"] for call in env["model"].calls[before:]] == ["coordinator"]
    _, decision = await app.coordinator.decide(
        actor, question, {"topic_keys": ["code.python"]}, 1, POLICIES, None
    )
    assert decision.action == "ask_clarification"


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", ["role", "budget"])
async def test_unavailable_coordinator_clarifies_without_provider_call(env, unavailable):
    run = active_run(env, "unavailable", roles=["tutor"] if unavailable == "role" else None, cap=1)
    if unavailable == "budget":
        with env["app"].db.transaction() as db:
            db.get(AgentRun, run[0]).calls_consumed = 1
    _, decision = await env["app"].coordinator.decide(
        env["actor"], "Do not compare", {}, 1, POLICIES, run
    )
    assert decision.action == "ask_clarification" and not env["model"].calls


@pytest.mark.asyncio
async def test_compare_and_model_reference_do_not_widen_current_source_focus(env):
    app, actor = env["app"], env["actor"]
    first, _, _ = await publish_fixture(env, "focus-one", "scope/alpha")
    second, _, _ = await publish_fixture(env, "focus-two", "scope/alpha-long")
    focus = {"project_source_ids": [first], "topic_keys": ["agent.tool_calling"]}
    intent, _ = await app.coordinator.decide(
        actor, "Compare the projects", focus, 1, POLICIES, None
    )
    assert intent.project_source_ids == [first]
    intent, decision = await app.coordinator.decide(
        actor, "Explain scope/alpha-long", focus, 1, POLICIES, None
    )
    assert intent.project_source_ids == [first] and decision.action == "ask_clarification"
    env["model"].overrides["coordinator"] = lambda _: ModelOutput(
        structured=proposal(project_source_ids=[second]), synthetic=True
    )
    intent, decision = await app.coordinator.decide(
        actor, "Do not compare; explain it", focus, 1, POLICIES, active_run(env, "source-proposal")
    )
    assert intent.project_source_ids == [first] and decision.action == "ask_clarification"
    call = [c for c in env["model"].calls if c["role"] == "coordinator"][-1]
    assert {s["id"] for s in call["context"]["authorized_sources"]} == {first}
    assert second not in call["access_dependencies"]


@pytest.mark.asyncio
async def test_soft_hint_fallback_retains_hard_source_topic_and_principal(env):
    app, actor = env["app"], env["actor"]
    source, publication, _ = await publish_fixture(env, "in-scope", "scope/one")
    _, other_publication, _ = await publish_fixture(env, "outside-source", "scope/two")
    with app.db.transaction() as db:
        other, namespace, _ = create_principal(db, "other-owner")
        other_id, other_ns = other.id, namespace.id
    funding = app.runtime.create_funding(other_id, other_ns, 100)
    other_env = {**env, "actor": other_id, "ns": other_ns, "funding": funding}
    private_source, private_publication, _ = await publish_fixture(
        other_env, "outside-acl", "scope/private"
    )
    request = spec(
        project_source_ids=[source],
        topic_keys=["agent.tool_calling"],
        topic_hints=["storage.redis"],
    )
    result = await app.retrieval.retrieve(actor, request)
    assert [u["publication_id"] for u in result["knowledge_units"]] == [publication]
    assert result["query_normalization"]["hard_topic_keys"] == ["agent.tool_calling"]
    assert "storage.redis" in result["query_normalization"]["topic_hints"]
    with app.db.read() as db:
        frozen = db.get(ObjectRow, result["retrieval_manifest_id"]).payload["task_spec"]
    assert frozen["topic_keys"] == ["agent.tool_calling"] and frozen["project_source_ids"] == [
        source
    ]
    unscoped = await app.retrieval.retrieve(actor, spec(topic_hints=["storage.redis"]))
    assert {u["publication_id"] for u in unscoped["knowledge_units"]} == {
        publication,
        other_publication,
    }
    assert private_publication not in {u["publication_id"] for u in unscoped["knowledge_units"]}
    with pytest.raises(DomainError):
        await app.retrieval.retrieve(actor, spec(project_source_ids=[private_source]))


@pytest.mark.asyncio
async def test_hard_topic_is_never_relaxed_even_for_symbols_and_verification(env):
    app, actor = env["app"], env["actor"]
    source, _, _ = await publish_fixture(env)
    for task in [TaskType.IMPLEMENTATION, TaskType.VERIFY, TaskType.LOCATE]:
        result = await app.retrieval.retrieve(
            actor,
            spec(
                task_type=task,
                user_question="echo",
                project_source_ids=[source],
                topic_keys=["storage.redis"],
                topic_hints=["agent.tool_calling"],
            ),
        )
        assert (
            result["knowledge_units"] == []
            and result["symbols"] == []
            and result["source_observations"] == []
        )


@pytest.mark.asyncio
async def test_verify_lexical_and_symbol_channels_use_only_reviewed_spans(env):
    app, actor = env["app"], env["actor"]
    source, publication, _ = await publish_fixture(env)
    result = await app.retrieval.retrieve(
        actor,
        spec(
            task_type=TaskType.VERIFY,
            user_question="Verify echo returns value",
            project_source_ids=[source],
            topic_keys=["agent.tool_calling"],
        ),
    )
    assert [u["publication_id"] for u in result["knowledge_units"]] == [publication]
    assert {"ast_symbol", "postgres_fts"} <= set(result["plan"]["allowed_channels"])
    assert "source_read" not in result["plan"]["allowed_channels"]
    assert not result["symbols"] and not result["source_observations"]
    with app.db.read() as db:
        trace = db.get(ObjectRow, result["retrieval_manifest_id"]).payload["ranking_trace"]
    assert trace["postgres_fts"] == [publication] and trace["ast_symbol"] == [publication]


@pytest.mark.asyncio
async def test_scope_and_hint_changes_partition_cache_and_cache_cannot_widen_scope(
    env, monkeypatch
):
    app, actor = env["app"], env["actor"]
    source, publication, _ = await publish_fixture(env, "cache-one", "scope/cache-one")
    other, outside, _ = await publish_fixture(env, "cache-two", "scope/cache-two")
    original = app.cache.singleflight
    keys = []

    async def record(layer, key, loader, ttl=30, *, publish_guard=None):
        keys.append((layer, key))
        return await original(layer, key, loader, ttl, publish_guard=publish_guard)

    monkeypatch.setattr(app.cache, "singleflight", record)
    base = spec(
        user_question="echo", project_source_ids=[source], topic_keys=["agent.tool_calling"]
    )
    for request in [
        base,
        base.model_copy(update={"topic_hints": ["storage.redis"]}),
        base.model_copy(update={"topic_keys": ["storage.redis"]}),
        base.model_copy(update={"project_source_ids": [other]}),
    ]:
        await app.retrieval.retrieve(actor, request)
    assert len(set(keys)) == 4

    async def poisoned(*args, **kwargs):
        return {
            "ids": [outside, publication],
            "trace": {"postgres_fts": [outside, publication]},
            "query_rewrite": {"variants": ["echo"]},
        }

    monkeypatch.setattr(app.cache, "singleflight", poisoned)
    result = await app.retrieval.retrieve(actor, base)
    assert [u["publication_id"] for u in result["knowledge_units"]] == [publication]
    empty = await app.retrieval.retrieve(
        actor,
        base.model_copy(
            update={"topic_keys": ["storage.redis"], "topic_hints": ["agent.tool_calling"]}
        ),
    )
    assert empty["knowledge_units"] == []


@pytest.mark.asyncio
async def test_acl_change_after_loader_blocks_stale_cache_fill(env, monkeypatch):
    from careerforge.services.security import set_grant

    app, actor = env["app"], env["actor"]
    source, _, _ = await publish_fixture(env)
    with app.db.transaction() as db:
        reader, _, _ = create_principal(db, "ranking-reader")
        reader_id = reader.id
        set_grant(db, actor, env["ns"], reader_id, "reader", True)
    original = app.cache.singleflight
    initial_keys = set(app.cache.values)

    async def race(layer, key, loader, ttl=30, *, publish_guard=None):
        async def load_then_revoke():
            result = await loader()
            with app.db.transaction() as db:
                set_grant(db, actor, env["ns"], reader_id, "reader", False)
            return result

        return await original(layer, key, load_then_revoke, ttl, publish_guard=publish_guard)

    monkeypatch.setattr(app.cache, "singleflight", race)
    with pytest.raises(DomainError) as error:
        await app.retrieval.retrieve(reader_id, spec(project_source_ids=[source]))
    assert error.value.code == "RETRIEVAL_CHANGED"
    assert set(app.cache.values) == initial_keys


@pytest.mark.asyncio
async def test_coordinator_does_not_egress_previous_answers_outside_funded_focus(env):
    app, actor = env["app"], env["actor"]
    previous, _, _ = await publish_fixture(env, "history-a", "scope/history-a")
    current, _, _ = await publish_fixture(env, "history-b", "scope/history-b")
    funding = app.runtime.create_funding(actor, env["ns"], 3, allowed_source_ids=[current])
    run_id = app.runtime.create(actor, env["ns"], funding, "research", {}, 1, "history-scope")
    epoch = app.runtime.claim(run_id, "history-worker")
    env["model"].overrides["coordinator"] = lambda _: ModelOutput(
        structured=proposal(), synthetic=True
    )
    focus = {
        "project_source_ids": [current],
        "topic_keys": ["agent.tool_calling"],
        "history": [
            {"question": "PRIVATE_A_QUESTION_CANARY", "answer_excerpt": "PRIVATE_A_BODY_CANARY"}
        ],
        "code_focus": {"source_id": previous, "symbol": "PRIVATE_A_SYMBOL_CANARY"},
        "unrecognized_body": "PRIVATE_A_EXTRA_CANARY",
    }
    _, decision = await app.coordinator.decide(
        actor,
        "Do not compare; explain this call",
        focus,
        1,
        POLICIES,
        (run_id, epoch, "history-worker"),
    )
    assert decision.action == "invoke_tutor"
    request = [c for c in env["model"].calls if c["role"] == "coordinator"][-1]
    assert "CANARY" not in repr(request)
    assert (
        previous not in request["access_dependencies"] and current in request["access_dependencies"]
    )
    assert "history" not in request["context"]["focus"]
    assert "code_focus" not in request["context"]["focus"]
