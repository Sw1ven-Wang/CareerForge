"""Mechanism regressions for an observed public native-run retrieval miss.

The native Chinese question is unchanged. Test publications/model replies are
explicit synthetic fixtures; the separate native replay checks the real packs.
These checks do not assert live answer quality or promote hints into evidence.
"""

from __future__ import annotations

import pytest
from conftest import publish_fixture

from careerforge.contracts import ModelOutput, RetrievalTaskSpec, TaskType

NATIVE_QUESTION = "预先准备好的工具参数和这次输入不一致时，会怎么处理？"
OLD_HINTS = ["code.python", "runtime.async"]
POLICIES = {
    "routing_policy": {
        "policy_id": "focused-retrieval-test",
        "content": {"ambiguity_threshold": 0.6},
    }
}


def request(source, question=NATIVE_QUESTION, **changes):
    return RetrievalTaskSpec.model_validate(
        {
            "turn_id": "focused-followup",
            "task_type": TaskType.IMPLEMENTATION,
            "user_question": question,
            "focus_revision": 3,
            "project_source_ids": [source],
            "topic_keys": [],
            "topic_hints": OLD_HINTS,
            **changes,
        }
    )


def focus(source=""):
    return {
        "topic_keys": OLD_HINTS,
        "retrieval_topic_keys": [],
        "topic_hints": OLD_HINTS,
        "retrieval_scope_version": "explicit_scope_v2",
        "project_source_ids": [source] if source else [],
    }


@pytest.mark.asyncio
async def test_native_followup_question_does_not_expand_previous_async_aliases(env):
    app, actor = env["app"], env["actor"]
    source, publication, _ = await publish_fixture(env)
    before = len(env["model"].calls)
    result = await app.retrieval.retrieve(actor, request(source))
    assert result["query_normalization"]["alias_variants"] == [NATIVE_QUESTION]
    assert result["query_normalization"]["hard_topic_keys"] == []
    assert result["knowledge_units"][0]["publication_id"] == publication
    assert len(env["model"].calls) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "Explain parameter mismatch handling",
        "不要继续讲解，解释参数的校验边界。",
        "Explain the identifier `continue`.",
        "Explain why the quote says 'continue'.",
        "Explain quantum entanglement",
    ],
)
async def test_substantive_questions_do_not_inherit_old_soft_topics(env, question):
    intent, _ = await env["app"].coordinator.decide(
        env["actor"], question, focus(), 3, POLICIES, None
    )
    assert not set(OLD_HINTS) & set(intent.topic_hints)
    assert intent.retrieval_topic_keys == []


@pytest.mark.asyncio
async def test_native_coordinator_reply_keeps_source_but_not_old_topics(env):
    app, actor = env["app"], env["actor"]
    source, _, _ = await publish_fixture(env)
    funding = app.runtime.create_funding(actor, env["ns"], 2, roles=["coordinator"])
    run = app.runtime.create(actor, env["ns"], funding, "research", {}, 1, "native-followup")
    epoch = app.runtime.claim(run, "focused-tests")
    env["model"].overrides["coordinator"] = lambda _: ModelOutput(
        synthetic=True,
        structured={
            "interpreted_intent": "explain_implementation",
            "topic_keys": [],
            "project_source_ids": [source],
            "reference_resolution": "resolved",
            "confidence": 0.95,
            "proposed_next_action": "retrieve_and_tutor",
            "rationale_codes": [
                "inherited_authorized_project_focus",
                "implementation_question",
                "implementation_evidence_required",
            ],
        },
    )
    intent, decision = await app.coordinator.decide(
        actor, NATIVE_QUESTION, focus(source), 3, POLICIES, (run, epoch, "focused-tests")
    )
    assert intent.project_source_ids == [source]
    assert decision.action == "invoke_tutor"
    assert intent.topic_hints == [] and intent.retrieval_topic_keys == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question", ["继续讲解", "Explain this", "Continue with this", "Tell me more"]
)
async def test_bare_continuations_keep_context_topics(env, question):
    intent, _ = await env["app"].coordinator.decide(
        env["actor"], question, focus(), 3, POLICIES, None
    )
    assert intent.topic_hints == OLD_HINTS
    assert intent.retrieval_topic_keys == []


@pytest.mark.asyncio
async def test_current_positive_topics_and_hard_constraints_are_not_erased(env):
    intent, _ = await env["app"].coordinator.decide(
        env["actor"], "Explain Redis cache", focus(), 3, POLICIES, None
    )
    assert "storage.redis" in intent.topic_hints
    assert not set(OLD_HINTS) & set(intent.topic_hints)
    intent, _ = await env["app"].coordinator.decide(
        env["actor"],
        NATIVE_QUESTION,
        {**focus(), "retrieval_topic_keys": ["code.python"]},
        3,
        POLICIES,
        None,
    )
    assert intent.retrieval_topic_keys == ["code.python"]


@pytest.mark.asyncio
async def test_explicit_source_switch_drops_soft_subject_but_keeps_hard_topic(env):
    app, actor = env["app"], env["actor"]
    source, _, _ = await publish_fixture(env, "focused-source-a", "scope/alpha")
    other, _, _ = await publish_fixture(env, "focused-source-b", "scope/beta")
    prior = {**focus(source), "retrieval_topic_keys": ["agent.tool_calling"]}
    intent, decision = await app.coordinator.decide(
        actor, "Switch to scope/beta and explain parameter handling", prior, 3, POLICIES, None
    )
    assert intent.project_source_ids == [other]
    assert intent.retrieval_topic_keys == ["agent.tool_calling"]
    assert not set(OLD_HINTS) & set(intent.topic_hints)
    assert "explicit_source_focus_change" in intent.rationale_codes
    # Negated or quoted switches cannot change the source, regardless of hints.
    for question in [
        "Do not switch to scope/beta; explain parameters",
        'Explain "switch to scope/beta"',
    ]:
        intent, _ = await app.coordinator.decide(actor, question, prior, 3, POLICIES, None)
        assert intent.project_source_ids == [source]
        assert intent.retrieval_topic_keys == ["agent.tool_calling"]


@pytest.mark.asyncio
async def test_legacy_normalization_and_cache_partition_are_preserved(env, monkeypatch):
    from careerforge.services.retrieval_context import RETRIEVAL_CONTEXT_CONTRACT

    app, actor = env["app"], env["actor"]
    source, _, _ = await publish_fixture(env)
    original = app.cache.singleflight
    keys = []

    async def capture(layer, key, loader, ttl=30, *, publish_guard=None):
        keys.append(key)
        return await original(layer, key, loader, ttl, publish_guard=publish_guard)

    monkeypatch.setattr(app.cache, "singleflight", capture)
    current = await app.retrieval.retrieve(actor, request(source))
    legacy = await app.retrieval.retrieve(actor, request(source), retrieval_context_contract=None)
    assert keys[0] != keys[1]
    assert (
        current["query_normalization"]["retrieval_context_contract"] == RETRIEVAL_CONTEXT_CONTRACT
    )
    assert "retrieval_context_contract" not in legacy["query_normalization"]
    assert legacy["query_normalization"]["alias_variants"] == [
        NATIVE_QUESTION,
        "Python",
        "python",
        "异步 I/O",
        "asyncio",
        "异步",
        "async",
        "await",
    ]
    intent, _ = await app.coordinator.decide(
        actor,
        NATIVE_QUESTION,
        focus(source),
        3,
        POLICIES,
        None,
        retrieval_context_contract=None,
    )
    assert intent.topic_hints == OLD_HINTS


@pytest.mark.asyncio
async def test_bounded_selection_keeps_whole_frozen_pack_and_empty_budget_gap(env):
    from careerforge.models import ObjectRow
    from careerforge.services.consumer_context import CONSUMER_CONTEXT_CONTRACT
    from careerforge.services.retrieval_context import LEGACY_FACT_RETRIEVAL_CONTEXT_CONTRACT
    from careerforge.util import canonical

    app, actor = env["app"], env["actor"]
    source, publication, _ = await publish_fixture(env)
    result = await app.retrieval.retrieve(actor, request(source))
    with app.db.read() as db:
        legacy_pack = app.knowledge.publication_pack(db, actor, publication)
        pack = app.knowledge.publication_pack(
            db, actor, publication, consumer_context_contract=CONSUMER_CONTEXT_CONTRACT
        )
        manifest = db.get(ObjectRow, result["retrieval_manifest_id"]).payload
    assert {k: v for k, v in pack.items() if k != "consumer_context"} == legacy_pack
    assert "consumer_context" not in legacy_pack
    assert pack["consumer_context"]["contract"] == CONSUMER_CONTEXT_CONTRACT
    assert all(row["standalone"] is True for row in pack["consumer_context"]["facts"].values())
    assert result["knowledge_units"] == [pack]
    assert manifest["evidence_budget"] == {
        "unit": "utf8_bytes",
        "limit": 12000,
        "selected_bytes": len(canonical(pack).encode()),
    }
    legacy = await app.retrieval.retrieve(
        actor, request(source), retrieval_context_contract=LEGACY_FACT_RETRIEVAL_CONTEXT_CONTRACT
    )
    assert legacy["knowledge_units"] == [legacy_pack]
    with app.db.read() as db:
        legacy_manifest = db.get(ObjectRow, legacy["retrieval_manifest_id"]).payload
    assert legacy_manifest["evidence_budget"] == {
        "unit": "utf8_bytes",
        "limit": 12000,
        "selected_bytes": len(canonical(legacy_pack).encode()),
    }
    limited = await app.retrieval.retrieve(actor, request(source, max_evidence_tokens=100))
    assert limited["knowledge_units"] == []
    assert limited["uncovered_questions"] == ["no_authorized_reviewed_match"]
    with app.db.read() as db:
        manifest = db.get(ObjectRow, limited["retrieval_manifest_id"]).payload
    assert manifest["omitted_publications"] == [publication]
    assert manifest["evidence_budget"]["selected_bytes"] == 0


@pytest.mark.asyncio
async def test_unrelated_question_is_not_made_answerable_by_old_hints(env):
    from careerforge.services.policies import ensure_policies

    app, actor = env["app"], env["actor"]
    source, _, _ = await publish_fixture(env)
    question = "Explain quantum entanglement"
    result = await app.retrieval.retrieve(actor, request(source, question))
    with app.db.transaction() as db:
        policies = ensure_policies(db, actor, env["ns"])
    before = len(env["model"].calls)
    blocks = await app.answers.tutor(
        actor,
        question,
        result,
        policies,
        focus(source),
        None,
        answer_contract="atomic-answer-1.6.1",
    )
    assert blocks and all(block["kind"] == "evidence_gap" for block in blocks)
    assert len(env["model"].calls) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_turn_freezes_context_contract_and_unmarked_turn_preserves_legacy(
    env, legacy, monkeypatch
):
    from careerforge.contracts import TurnInput
    from careerforge.models import Conversation, Turn
    from careerforge.services.retrieval_context import RETRIEVAL_CONTEXT_CONTRACT

    app, actor = env["app"], env["actor"]
    source, _, _ = await publish_fixture(env)
    conversation = app.conversations.create(actor)
    with app.db.transaction() as db:
        db.get(Conversation, conversation).focus = focus(source)
    submitted = app.conversations.submit(
        actor,
        conversation,
        TurnInput(
            question="Explain parameter handling",
            expected_revision=1,
            idempotency_key="context-contract",
        ),
    )
    with app.db.transaction() as db:
        turn = db.get(Turn, submitted["turn_id"])
        assert turn.focus["retrieval_context_contract"] == RETRIEVAL_CONTEXT_CONTRACT
        if legacy:
            turn.focus = {
                key: value
                for key, value in turn.focus.items()
                if key != "retrieval_context_contract"
            }
    original_decide, original_retrieve = app.coordinator.decide, app.retrieval.retrieve
    seen = []

    async def decide(*args, **kwargs):
        seen.append(("decide", kwargs.get("retrieval_context_contract", "MISSING")))
        return await original_decide(*args, **kwargs)

    async def retrieve(*args, **kwargs):
        seen.append(("retrieve", kwargs.get("retrieval_context_contract", "MISSING")))
        return await original_retrieve(*args, **kwargs)

    monkeypatch.setattr(app.coordinator, "decide", decide)
    monkeypatch.setattr(app.retrieval, "retrieve", retrieve)
    await app.conversations.process(submitted["turn_id"])
    expected = None if legacy else RETRIEVAL_CONTEXT_CONTRACT
    assert seen == [("decide", expected), ("retrieve", expected)]


@pytest.mark.asyncio
async def test_unknown_context_version_fails_before_calls(env):
    from careerforge.errors import DomainError

    app, actor = env["app"], env["actor"]
    for operation in [
        app.retrieval.retrieve(actor, request("not-read"), retrieval_context_contract="unknown"),
        app.coordinator.decide(
            actor, NATIVE_QUESTION, focus(), 3, POLICIES, None, retrieval_context_contract="unknown"
        ),
    ]:
        with pytest.raises(DomainError) as failure:
            await operation
        assert failure.value.code == "RETRIEVAL_CONTEXT_CONTRACT"
    assert not env["model"].calls
