"""Synthetic/offline C08-01 integration, not the historical FB-T02 trajectory."""

import json
from copy import deepcopy

import httpx
import pytest
from conftest import publish_fixture
from sqlalchemy import select

from careerforge.api import create_app
from careerforge.contracts import FeedbackInput, TurnInput
from careerforge.contracts_feedback_target_v1 import (
    FEEDBACK_TARGET_CHOICE,
    FEEDBACK_TARGET_CONTRACT,
    FEEDBACK_TARGET_KEY,
    AnswerContinuationFeedbackInput,
)
from careerforge.errors import DomainError
from careerforge.models import (
    AgentRun,
    Conversation,
    Experience,
    Feedback,
    Idempotency,
    ObjectRow,
    SourceSet,
    StageArtifact,
    Turn,
)
from careerforge.util import fingerprint

QUESTION = "OpenAI Agents SDK 的函数工具异步调用具体如何执行？"
CONTENT = "讲得太抽象，我想直接看源码。"


def client(env):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service=env["app"])),
        base_url="http://test",
        headers={"Authorization": "Bearer " + env["owner"]["token"]},
    )


def set_focus(env, conv, source, topic="code.python"):
    with env["app"].db.transaction() as db:
        db.get(Conversation, conv).focus = {
            "topic_keys": [topic],
            "topic_hints": [topic],
            "retrieval_topic_keys": [],
            "task_type": "locate_symbol",
            "project_source_ids": [source],
        }


async def new_conversation(env, source):
    async with client(env) as c:
        response = await c.post(
            "/api/v2/conversations/answer-feedback", json={"contract": FEEDBACK_TARGET_CONTRACT}
        )
    assert response.status_code == 201, response.text
    conv = response.json()["conversation_id"]
    set_focus(env, conv, source)
    return conv


def submit(env, conv, question=QUESTION, key=None):
    app, actor = env["app"], env["actor"]
    revision = app.conversations.detail(actor, conv)["revision"]
    value = TurnInput(
        question=question,
        expected_revision=revision,
        funding_id=env["funding"],
        idempotency_key=key or f"turn-{revision}",
    )
    started = app.conversations.submit(actor, conv, value)
    return started, value


async def answer(env, conv, question=QUESTION):
    started, value = submit(env, conv, question)
    await env["app"].execute(started["run_id"])
    with env["app"].db.read() as db:
        turn = db.get(Turn, started["turn_id"])
        run = db.get(AgentRun, started["run_id"])
        assert turn.status == "completed", (turn.status, run.status)
    return turn, value


async def ready(env):
    source, _, _ = await publish_fixture(env)
    conv = await new_conversation(env, source)
    original, _ = await answer(env, conv)
    with env["app"].db.read() as db:
        selection = db.get(ObjectRow, original.manifest_id)
        assert original.focus["topic_keys"] == ["code.python"]
        assert selection.payload["task_spec"]["topic_hints"] == ["runtime.async"]
        assert selection.payload["task_spec"]["topic_keys"] == []
    assert env["app"].conversations.detail(env["actor"], conv)["focus"]["topic_keys"] == [
        "runtime.async"
    ]
    return source, conv, original


async def feedback(env, target, content=CONTENT, key="feedback", consent=False):
    async with client(env) as c:
        result = await c.post(
            "/api/v2/feedback/answer-continuation",
            json={
                "contract": FEEDBACK_TARGET_CONTRACT,
                "turn_id": target.id,
                "content": content,
                "consent": consent,
                "idempotency_key": key,
            },
        )
    assert result.status_code == 201, result.text
    return result.json()


def target_object(env, result):
    with env["app"].db.read() as db:
        return db.get(ObjectRow, result["answer_feedback"]["binding"]["manifest_id"])


@pytest.mark.asyncio
async def test_public_opt_in_same_answer_split_reaches_provider_once_and_reverts(env, tmp_path):
    app, actor = env["app"], env["actor"]
    source, conv, original = await ready(env)
    original_focus = deepcopy(original.focus)
    original_requests = deepcopy(env["model"].calls)
    with app.db.read() as db:
        old_run = db.get(AgentRun, original.run_id)
        original_input = deepcopy(old_run.input_manifest)
        original_hash = old_run.input_hash
    response = await feedback(env, original, consent=True)
    assert response["answer_feedback"]["accepted"] and not response["answer_feedback"]["delivered"]
    assert response["preference_event_ids"] == [] and response["experience_id"]
    binding = target_object(env, response)
    assert binding.payload["scope"] == {"kind": "target_answer"}
    assert binding.payload["lifetime"] == "turn" and not binding.payload["durable_consent"]
    assert binding.payload["target"]["context_only"]["soft_topic_hints"] == ["runtime.async"]
    assert binding.payload["target"]["source_ids"] == [source]
    assert app.learning.preference_events(actor)["events"] == []
    following, value = await answer(env, conv)
    assert following.focus["preferences"]["requested_style"] == "code_first"
    tutor = [call for call in env["model"].calls if call["role"] == "tutor"][-1]
    assert tutor["context"]["focus"]["preferences"]["requested_style"] == "code_first"
    token = tutor["context"]["focus"][FEEDBACK_TARGET_KEY]
    assert token["manifest_hash"] == following.focus[FEEDBACK_TARGET_KEY]["manifest_hash"]
    assert token["targets"] == [response["answer_feedback"]["binding"]]
    assert token["facts_approved"] is False and token["grants_permissions"] is False
    calls = len(env["model"].calls)
    replay = app.conversations.submit(actor, conv, value)
    assert replay["turn_id"] == following.id and len(env["model"].calls) == calls
    assert await feedback(env, original, consent=True) == response
    later, _ = await answer(env, conv)
    assert later.focus["preferences"]["requested_style"] == "concept_first"
    assert (
        FEEDBACK_TARGET_KEY
        not in [c for c in env["model"].calls if c["role"] == "tutor"][-1]["context"]["focus"]
    )
    with app.db.read() as db:
        assert db.get(Turn, original.id).focus == original_focus
        assert db.get(AgentRun, original.run_id).input_manifest == original_input
        assert db.get(AgentRun, original.run_id).input_hash == original_hash
        assert (
            len(
                list(
                    db.scalars(
                        select(Idempotency).where(
                            Idempotency.operation == "answer_feedback.consume.v1"
                        )
                    )
                )
            )
            == 1
        )
        assert db.get(Feedback, response["feedback_id"]).consent_revision == 1
        episode = db.get(Experience, response["experience_id"])
        assert episode.feedback_id == response["feedback_id"]
        assert episode.payload["turn_id"] == original.id
        assert (
            episode.payload["answer_feedback"]["binding"] == response["answer_feedback"]["binding"]
        )
        assert episode.payload["answer_feedback"]["lifetime"] == "turn"
        assert episode.payload["answer_feedback"]["durable_consent"] is False
        from careerforge.services.evolution_feedback import consent_bound_episodes

        episodes, witnesses = consent_bound_episodes(db, actor, env["ns"], [episode.id])
        assert (
            episodes == [episode.payload] and witnesses[0]["feedback_id"] == response["feedback_id"]
        )
    assert env["model"].calls[: len(original_requests)] == original_requests
    (tmp_path / "SYNTHETIC_FLOW.json").write_text(
        json.dumps(
            {
                "scope": "SYNTHETIC/OFFLINE, exact C08-01 question text; not original FB-T02 or native quality",
                "question": QUESTION,
                "target_binding": response["answer_feedback"]["binding"],
                "consumer_binding": token,
                "provider_preferences": tutor["context"]["focus"]["preferences"],
                "target_input_hash": original_hash,
                "original_request_hashes": [fingerprint(r) for r in original_requests],
                "old_requests_unchanged": True,
                "later_style": later.focus["preferences"]["requested_style"],
                "feedback_id": response["feedback_id"],
                "experience_id": response["experience_id"],
                "durable_consent": False,
                "one_use_claim_count": 1,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


@pytest.mark.asyncio
async def test_legacy_feedback_retains_pre_routing_topic_and_old_input_schema(env):
    app, actor = env["app"], env["actor"]
    source, _, _ = await publish_fixture(env)
    conv = app.conversations.create(actor)
    set_focus(env, conv, source)
    original, _ = await answer(env, conv)
    result = app.evolution.feedback(
        actor,
        FeedbackInput(
            turn_id=original.id, signal="explicit_feedback", content=CONTENT, consent=True
        ),
    )
    event = app.learning.preference_events(actor)["events"][-1]
    assert event["scope"] == {"kind": "topic", "identifier": "code.python"}
    assert event["contract_version"] == "learning-preferences-1.6.6"
    assert event["after_turn_id"] == original.id and event["applies_to_turn_id"] is None
    assert not event["durable_consent"] and event["lifetime"] == "turn"
    assert "answer_feedback" not in result
    following, _ = await answer(env, conv)
    assert following.focus["preferences"]["requested_style"] == "concept_first"
    assert FEEDBACK_TARGET_KEY not in following.focus
    assert set(FeedbackInput.model_fields) == {"turn_id", "signal", "content", "consent"}
    assert set(TurnInput.model_fields) == {
        "question",
        "expected_revision",
        "funding_id",
        "idempotency_key",
    }


@pytest.mark.asyncio
async def test_delayed_feedback_uses_target_not_navigation_and_other_topics_do_not_consume(env):
    app, actor = env["app"], env["actor"]
    source, conv, original = await ready(env)
    set_focus(env, conv, source, "agent.tool_calling")
    response = await feedback(env, original)
    binding = target_object(env, response)
    assert binding.payload["target"]["context_only"]["soft_topic_hints"] == ["runtime.async"]
    other, _ = await answer(env, conv, "解释函数返回的 value 参数")
    assert other.focus["preferences"]["requested_style"] == "concept_first"
    other_conv = await new_conversation(env, source)
    elsewhere, _ = await answer(env, other_conv)
    assert elsewhere.focus["preferences"]["requested_style"] == "concept_first"
    following, _ = await answer(env, conv)
    assert following.focus["preferences"]["requested_style"] == "code_first"
    assert (
        app.learning.effective_preferences(actor, conversation_id=conv)["preferences"][
            "requested_style"
        ]
        == "concept_first"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "named_topic,eligible",
    [("runtime.async", True), ("agent.tool_calling", False), ("code.python", False)],
)
async def test_named_scope_is_an_additional_constraint_never_replaced_by_hint(
    env, named_topic, eligible
):
    _, conv, original = await ready(env)
    response = await feedback(env, original, content=f"这一次讲解 {named_topic} 时先看代码。")
    binding = target_object(env, response)
    assert binding.payload["scope"] == {"kind": "topic", "identifier": named_topic}
    following, _ = await answer(env, conv)
    assert (following.focus["preferences"]["requested_style"] == "code_first") is eligible
    with env["app"].db.read() as db:
        consumed = db.scalar(
            select(Idempotency).where(
                Idempotency.operation == "answer_feedback.consume.v1", Idempotency.key == binding.id
            )
        )
        assert bool(consumed) is eligible


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "除了 Python，这次先看代码。",
        "只针对不存在的主题，这次先看代码。",
        "以后学习 Tool Calling 时先看代码。",
        "这整个会话先看代码。",
        "这一次先看代码，以后都先看架构。",
        "全局这次先看代码。",
        "这一次先看代码，不要练习。",
        "撤销全局的代码优先偏好。",
    ],
)
async def test_unrepresentable_scope_or_lifetime_clarifies_atomically(env, content):
    _, _, original = await ready(env)
    before = env["app"].learning.preference_events(env["actor"])
    result = await feedback(env, original, content=content, consent=True)
    assert result["preference_clarification"]
    assert not result["answer_feedback"]["accepted"]
    assert result["answer_feedback"]["binding"] is None
    assert env["app"].learning.preference_events(env["actor"]) == before
    with env["app"].db.read() as db:
        assert not db.scalar(select(ObjectRow).where(ObjectRow.kind == "answer_feedback_target"))


@pytest.mark.asyncio
async def test_training_consent_false_still_allows_only_new_temporary_binding(env):
    _, conv, original = await ready(env)
    result = await feedback(env, original, consent=False)
    assert result["experience_id"] is None
    assert target_object(env, result).payload["durable_consent"] is False
    following, _ = await answer(env, conv)
    assert following.focus["preferences"]["requested_style"] == "code_first"


@pytest.mark.asyncio
async def test_legacy_conversation_cannot_opt_in_by_new_feedback_or_forged_marker(env):
    app, actor = env["app"], env["actor"]
    source, _, _ = await publish_fixture(env)
    conv = app.conversations.create(actor)
    set_focus(env, conv, source)
    original, _ = await answer(env, conv)
    data = {
        "contract": FEEDBACK_TARGET_CONTRACT,
        "turn_id": original.id,
        "content": CONTENT,
        "consent": False,
        "idempotency_key": "x",
    }
    async with client(env) as c:
        assert (await c.post("/api/v2/feedback/answer-continuation", json=data)).status_code == 409
    with app.db.transaction() as db:
        row = db.get(Conversation, conv)
        row.preferences = {**row.preferences, FEEDBACK_TARGET_CHOICE: FEEDBACK_TARGET_CONTRACT}
    with pytest.raises(DomainError, match="追溯升级"):
        app.conversations.detail(actor, conv)
    async with client(env) as c:
        assert (await c.post("/api/v2/feedback/answer-continuation", json=data)).status_code == 409


@pytest.mark.asyncio
async def test_new_contract_requires_explicit_valid_choice_and_no_legacy_schema_widening(env):
    async with client(env) as c:
        assert (await c.post("/api/v2/conversations/answer-feedback", json={})).status_code == 422
        assert (
            await c.post("/api/v2/conversations/answer-feedback", json={"contract": "future"})
        ).status_code == 422
        assert (
            await c.post("/api/v1/conversations", json={"contract": FEEDBACK_TARGET_CONTRACT})
        ).status_code == 422
        assert (
            await c.post(
                "/api/v1/feedback",
                json={
                    "contract": FEEDBACK_TARGET_CONTRACT,
                    "turn_id": "x",
                    "signal": "explicit_feedback",
                    "content": CONTENT,
                    "consent": False,
                },
            )
        ).status_code == 422


@pytest.mark.asyncio
async def test_changed_project_rejected_at_admission_and_delayed_target_is_not_rebased(env):
    app = env["app"]
    source, conv, original = await ready(env)
    other_source, _, _ = await publish_fixture(
        env, key="other", repository="fixtures/other", commit="b" * 40
    )
    set_focus(env, conv, other_source, "runtime.async")
    result = await feedback(env, original)
    assert target_object(env, result).payload["target"]["source_ids"] == [source]
    calls = len(env["model"].calls)
    with pytest.raises(DomainError, match="切换来源"):
        submit(env, conv)
    assert len(env["model"].calls) == calls
    with app.db.read() as db:
        assert not db.scalar(
            select(Idempotency).where(Idempotency.operation == "answer_feedback.consume.v1")
        )


@pytest.mark.asyncio
async def test_multiple_pending_same_target_requires_explicit_resolution(env):
    _, conv, original = await ready(env)
    await feedback(env, original, key="one")
    await feedback(env, original, key="two", content="这次先看架构图。")
    calls = len(env["model"].calls)
    with pytest.raises(DomainError, match="多条"):
        submit(env, conv)
    assert len(env["model"].calls) == calls


@pytest.mark.asyncio
async def test_feedback_idempotency_conflict_does_not_append_or_reinterpret(env):
    _, _, original = await ready(env)
    result = await feedback(env, original)
    async with client(env) as c:
        response = await c.post(
            "/api/v2/feedback/answer-continuation",
            json={
                "contract": FEEDBACK_TARGET_CONTRACT,
                "turn_id": original.id,
                "content": "这次先看架构图。",
                "consent": False,
                "idempotency_key": "feedback",
            },
        )
    assert (
        response.status_code == 409 and response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    )
    assert (await feedback(env, original)) == result
    with env["app"].db.read() as db:
        assert len(list(db.scalars(select(Feedback)))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage",
    [
        "target_payload",
        "selection_payload",
        "selection_rehashed",
        "run_input",
        "missing_stage",
        "missing_receipt",
        "feedback_withdrawn",
        "source_revoked",
        "source_identity",
        "privacy_generation",
    ],
)
async def test_pending_binding_integrity_privacy_and_revocation_reject_before_new_turn(env, damage):
    from careerforge.models_privacy_ext import PrivacyState

    app, actor = env["app"], env["actor"]
    source, conv, original = await ready(env)
    result = await feedback(env, original)
    binding = target_object(env, result)
    with app.db.transaction() as db:
        if damage == "target_payload":
            row = db.get(ObjectRow, binding.id)
            row.payload = {**row.payload, "preferences": {"requested_style": "architecture_first"}}
        elif damage in {"selection_payload", "selection_rehashed"}:
            row = db.get(ObjectRow, original.manifest_id)
            row.payload = {**row.payload, "selected_source_ids": []}
            if damage == "selection_rehashed":
                row.content_hash = fingerprint(row.payload)
        elif damage == "run_input":
            row = db.get(AgentRun, original.run_id)
            row.input_manifest = {**row.input_manifest, "turn_id": "not-target"}
        elif damage == "missing_stage":
            row = db.scalar(
                select(StageArtifact).where(
                    StageArtifact.run_id == original.run_id,
                    StageArtifact.stage_key == "orchestration",
                )
            )
            db.delete(row)
        elif damage == "missing_receipt":
            row = db.scalar(
                select(Idempotency).where(Idempotency.operation == "answer_feedback.submit.v1")
            )
            db.delete(row)
        elif damage == "feedback_withdrawn":
            db.get(Feedback, result["feedback_id"]).content = "[用户已撤回优化授权并移除反馈正文]"
        elif damage == "source_revoked":
            db.get(ObjectRow, source).state = "revoked"
        elif damage == "source_identity":
            db.get(SourceSet, source).commit_sha = "f" * 40
        else:
            db.get(PrivacyState, actor).generation += 1
    calls = len(env["model"].calls)
    with pytest.raises(DomainError):
        submit(env, conv)
    assert len(env["model"].calls) == calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage", ["projection", "turn_marker", "run_marker", "consume_receipt", "feedback_withdrawn"]
)
async def test_frozen_consumer_tamper_rejected_before_provider_and_not_reconsumed(env, damage):
    from careerforge.services.feedback_target_v1 import validate_run_in

    app, actor = env["app"], env["actor"]
    _, conv, original = await ready(env)
    result = await feedback(env, original)
    started, value = submit(env, conv)
    with app.db.transaction() as db:
        turn = db.get(Turn, started["turn_id"])
        if damage == "projection":
            turn.focus = {
                **turn.focus,
                "preferences": {
                    **turn.focus["preferences"],
                    "requested_style": "architecture_first",
                },
            }
        elif damage == "turn_marker":
            turn.focus = {key: val for key, val in turn.focus.items() if key != FEEDBACK_TARGET_KEY}
        elif damage == "run_marker":
            run = db.get(AgentRun, started["run_id"])
            run.input_manifest = {
                key: val for key, val in run.input_manifest.items() if key != FEEDBACK_TARGET_KEY
            }
        elif damage == "consume_receipt":
            row = db.scalar(
                select(Idempotency).where(Idempotency.operation == "answer_feedback.consume.v1")
            )
            db.delete(row)
        elif damage == "run_authority_stripping":
            from careerforge.services.security import create_principal

            other, other_ns, _ = create_principal(db, "other-run-owner")
            run = db.get(AgentRun, started["run_id"])
            run.principal_id = other.id
            run.namespace_id = other_ns.id
            run.workflow = "research"
            run.input_manifest = {}
            run.input_hash = fingerprint({})
            turn.run_id = None
            turn.focus = {key: val for key, val in turn.focus.items() if key != FEEDBACK_TARGET_KEY}
        else:
            db.get(Feedback, result["feedback_id"]).content = "[withdrawn]"
    calls = len(env["model"].calls)
    with app.db.read() as db, pytest.raises(DomainError):
        validate_run_in(db, db.get(AgentRun, started["run_id"]))
    with pytest.raises(DomainError):
        app.conversations.submit(actor, conv, value)
    assert len(env["model"].calls) == calls


@pytest.mark.asyncio
async def test_source_switch_during_routing_is_rejected_before_tutor_egress(env, monkeypatch):
    app = env["app"]
    _, conv, original = await ready(env)
    other_source, _, _ = await publish_fixture(
        env, key="other", repository="fixtures/other", commit="b" * 40
    )
    await feedback(env, original)
    started, _ = submit(env, conv)
    decide = app.coordinator.decide

    async def switch_source(*args, **kwargs):
        intent, decision = await decide(*args, **kwargs)
        return intent.model_copy(
            update={"project_source_ids": [other_source]}
        ), decision.model_copy(update={"source_version_refs": [other_source]})

    monkeypatch.setattr(app.coordinator, "decide", switch_source)
    calls = len(env["model"].calls)
    await app.execute(started["run_id"])
    assert not any(c["role"] == "tutor" for c in env["model"].calls[calls:])
    with app.db.read() as db:
        assert db.get(Turn, started["turn_id"]).status == "interrupted"
        assert db.get(AgentRun, started["run_id"]).status == "failed"


@pytest.mark.asyncio
async def test_same_admission_concurrently_claims_exactly_once(env):
    from concurrent.futures import ThreadPoolExecutor

    app, actor = env["app"], env["actor"]
    _, conv, original = await ready(env)
    await feedback(env, original)
    revision = app.conversations.detail(actor, conv)["revision"]
    value = TurnInput(
        question=QUESTION,
        expected_revision=revision,
        funding_id=env["funding"],
        idempotency_key="concurrent",
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: app.conversations.submit(actor, conv, value), range(2)))
    assert results[0]["turn_id"] == results[1]["turn_id"]
    with app.db.read() as db:
        assert (
            len(
                list(
                    db.scalars(
                        select(Idempotency).where(
                            Idempotency.operation == "answer_feedback.consume.v1"
                        )
                    )
                )
            )
            == 1
        )


@pytest.mark.asyncio
async def test_missing_consume_receipt_cannot_resurrect_spent_feedback(env):
    app = env["app"]
    _, conv, original = await ready(env)
    await feedback(env, original)
    await answer(env, conv)
    with app.db.transaction() as db:
        db.delete(
            db.scalar(
                select(Idempotency).where(Idempotency.operation == "answer_feedback.consume.v1")
            )
        )
    with pytest.raises(DomainError):
        submit(env, conv)


@pytest.mark.asyncio
async def test_other_actor_and_missing_completed_target_are_rejected(env):
    from careerforge.services.security import create_principal

    app, actor = env["app"], env["actor"]
    source, conv, original = await ready(env)
    with app.db.transaction() as db:
        other, _, _ = create_principal(db, "other")
    value = AnswerContinuationFeedbackInput(
        contract=FEEDBACK_TARGET_CONTRACT,
        turn_id=original.id,
        content=CONTENT,
        consent=False,
        idempotency_key="other",
    )
    with pytest.raises(DomainError):
        app.evolution.feedback_for_answer(other.id, value)
    pending, _ = submit(env, conv, question="解释另一个返回值")
    value = value.model_copy(update={"turn_id": pending["turn_id"]})
    with pytest.raises(DomainError):
        app.evolution.feedback_for_answer(actor, value)
    with app.db.read() as db:
        assert not db.scalar(select(Feedback))


@pytest.mark.asyncio
async def test_real_feedback_withdrawal_fences_target_and_existing_experience_reader(env):
    from careerforge.services.evolution_feedback import consent_bound_episodes

    app, actor = env["app"], env["actor"]
    _, conv, original = await ready(env)
    result = await feedback(env, original, consent=True)
    app.evolution.revoke_feedback(actor, result["feedback_id"])
    calls = len(env["model"].calls)
    with pytest.raises(DomainError):
        submit(env, conv)
    with app.db.read() as db, pytest.raises(DomainError) as exc:
        consent_bound_episodes(db, actor, env["ns"], [result["experience_id"]])
    assert exc.value.code == "EXPERIENCE_CONSENT"
    assert len(env["model"].calls) == calls


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["selection", "orchestration", "turn_authority"])
async def test_producer_rejects_missing_target_evidence_without_partial_feedback(env, missing):
    app = env["app"]
    _, _, original = await ready(env)
    with app.db.transaction() as db:
        if missing == "selection":
            db.get(Turn, original.id).manifest_id = None
        elif missing == "orchestration":
            db.delete(
                db.scalar(
                    select(StageArtifact).where(
                        StageArtifact.run_id == original.run_id,
                        StageArtifact.stage_key == "orchestration",
                    )
                )
            )
        else:
            row = db.get(Turn, original.id)
            row.focus = {key: val for key, val in row.focus.items() if key != FEEDBACK_TARGET_KEY}
    async with client(env) as c:
        response = await c.post(
            "/api/v2/feedback/answer-continuation",
            json={
                "contract": FEEDBACK_TARGET_CONTRACT,
                "turn_id": original.id,
                "content": CONTENT,
                "consent": True,
                "idempotency_key": "missing",
            },
        )
    assert response.status_code == 409
    with app.db.read() as db:
        assert not db.scalar(select(Feedback))
        assert not db.scalar(select(Experience))


@pytest.mark.asyncio
async def test_named_scope_cannot_be_dropped_from_consuming_selection(env, monkeypatch):
    app = env["app"]
    _, conv, original = await ready(env)
    await feedback(env, original, content="这一次讲解 runtime.async 时先看代码。")
    started, _ = submit(env, conv)
    retrieve = app.retrieval.retrieve

    async def missing_named_scope(*args, **kwargs):
        evidence = await retrieve(*args, **kwargs)
        # Synthetic damage at the actual selection boundary. Changing router
        # hints alone is not absence: retrieval can legitimately recover the
        # named hint from the unchanged question. No historical record is used.
        with app.db.transaction() as db:
            selection = db.get(ObjectRow, evidence["retrieval_manifest_id"])
            selection.payload = {
                **selection.payload,
                "task_spec": {
                    **selection.payload["task_spec"],
                    "topic_keys": [],
                    "topic_hints": ["code.python"],
                },
            }
            selection.content_hash = fingerprint(selection.payload)
        return evidence

    monkeypatch.setattr(app.retrieval, "retrieve", missing_named_scope)
    calls = len(env["model"].calls)
    await app.execute(started["run_id"])
    assert not any(c["role"] == "tutor" for c in env["model"].calls[calls:])
    with app.db.read() as db:
        assert db.get(AgentRun, started["run_id"]).status == "failed"


def test_old_model_focus_adds_no_fields_and_reads_no_feedback_state():
    from careerforge.services.feedback_target_v1 import model_context_in

    class NoReads:
        def __getattr__(self, name):
            raise AssertionError("Legacy provider projection must not inspect new feedback state")

    assert (
        model_context_in(
            NoReads(),
            "actor",
            {"preferences": {"requested_style": "concept_first"}},
            ("old", 1, "worker"),
            "old-selection",
        )
        == {}
    )
