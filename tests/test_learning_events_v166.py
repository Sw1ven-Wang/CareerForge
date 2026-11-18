"""Public-input regression contracts and synthetic product integration, not live-model QA."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from conftest import publish_fixture
from pydantic import ValidationError
from sqlalchemy import select
from test_learning_loop_v16 import (
    accepted_plan,
    configure_planner,
    create_check,
    observation_for,
    plan_input,
)
from test_source_navigation_v163 import import_navigation_source, submit_navigation

from careerforge.api import create_app
from careerforge.contracts import TaskType, TurnInput
from careerforge.contracts_learning_events_v166 import (
    NavigationAckInput,
    PreferenceEventInput,
    PreferenceScope,
)
from careerforge.contracts_learning_ext import (
    AttemptInput,
    AttemptSubmission,
    LearningPreferences,
    ProfileUpdate,
)
from careerforge.errors import DomainError
from careerforge.models import AnswerBlock, Conversation, Experience, Feedback, ObjectRow, Turn
from careerforge.services.dependencies import invalidate
from careerforge.services.learning_events import PREFERENCE_CONTRACT, teaching_preferences
from careerforge.services.learning_intent import LEARNING_ROUTING_CONTRACT, learning_intent
from careerforge.services.orchestration import rule_intent
from careerforge.services.policies import ensure_policies
from careerforge.services.security import create_principal
from careerforge.util import digest

_PUBLIC = [
    json.loads(line)
    for line in (Path(__file__).parent / "fixtures/learning_intents_public_v1.jsonl")
    .read_text()
    .splitlines()
]
_EXPECTED = [
    "build_project",
    "build_project",
    "learn_project",
    "learn_project",
    "technical_design",
    "technical_design",
    "function",
    "function",
    "function",
    "function",
    "learn_project",
    "function",
    None,
    "function",
    "learn_project",
    "function",
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,expected",
    list(zip(_PUBLIC, _EXPECTED, strict=True)),
    ids=[row["case_id"] for row in _PUBLIC],
)
async def test_all_public_learning_intents_use_real_coordinator_without_model(env, case, expected):
    app, actor = env["app"], env["actor"]
    source = import_navigation_source(env)
    context = case["context"]
    focus = {
        "project_source_ids": [source] if context["active_repo_id"] else [],
        "code_focus": {"source_id": source, "path": "src/agent.py", "symbol": "Target.run"}
        if context["code_focus"]
        else {},
    }
    inferred = learning_intent(case["utterance"], focus)
    assert inferred["intent"] == expected
    assert inferred["requires_clarification"] == (expected is None)
    assert inferred["user_mastery_unchanged"] and not inferred["forces_learning_mode"]
    with app.db.transaction() as db:
        policies = ensure_policies(db, actor, env["ns"])
    decision, route = await app.coordinator.decide(
        actor,
        case["utterance"],
        focus,
        1,
        policies,
        None,
        routing_contract_version=LEARNING_ROUTING_CONTRACT,
    )
    if expected is None:
        assert route.action == "ask_clarification"
    else:
        assert decision.interpreted_intent.value == inferred["task_type"]
        assert route.action != "ask_clarification"
    assert env["model"].calls == []


@pytest.mark.parametrize(
    "question,expected",
    [
        ("不要比较，先解释这个函数的返回值。", TaskType.IMPLEMENTATION),
        ("先不要验证这个结论，讲一下仓库架构。", TaskType.ARCHITECTURE),
        ('"compare projects" is a heading. Locate this function', TaskType.LOCATE),
        ("Teach me how to set up this project", TaskType.ARCHITECTURE),
    ],
)
def test_generic_intent_variants_are_not_fixture_keyed(question, expected):
    decision = rule_intent(
        question,
        {"project_source_ids": ["chosen"]},
        routing_contract_version=LEARNING_ROUTING_CONTRACT,
    )
    assert decision and decision.interpreted_intent == expected


@pytest.mark.parametrize(
    "question",
    [
        "Explain this function and delete the repository",
        "解释函数，然后运行测试",
        "Do not locate Target.run",
    ],
)
def test_new_learning_router_does_not_drop_negative_or_unsafe_actions(question):
    assert (
        rule_intent(
            question,
            {"project_source_ids": ["chosen"]},
            routing_contract_version=LEARNING_ROUTING_CONTRACT,
        )
        is None
    )


def add_event(env, **changes):
    app, actor = env["app"], env["actor"]
    defaults = dict(
        expected_revision=app.learning.profile(actor)["revision"],
        idempotency_key=f"event-{app.learning.profile(actor)['revision']}",
        key="requested_style",
        value="architecture_first",
        lifetime="persistent",
        durable_consent=True,
    )
    defaults.update(changes)
    return app.learning.preference_event(actor, PreferenceEventInput(**defaults))


def create_conversation(env, source=None):
    app, actor = env["app"], env["actor"]
    conv = app.conversations.create(actor)
    with app.db.transaction() as db:
        db.get(Conversation, conv).focus = {
            "topic_keys": ["agent.tool_calling"],
            "project_source_ids": [source] if source else [],
        }
    return conv


async def turn(env, conv, question, *, funded=True):
    app, actor = env["app"], env["actor"]
    revision = app.conversations.detail(actor, conv)["revision"]
    submitted = app.conversations.submit(
        actor,
        conv,
        TurnInput(
            question=question,
            expected_revision=revision,
            funding_id=env["funding"] if funded else None,
            idempotency_key=f"turn-{revision}",
        ),
    )
    if funded:
        await app.execute(submitted["run_id"])
    else:
        await app.conversations.process(submitted["turn_id"])
    with app.db.read() as db:
        return db.get(Turn, submitted["turn_id"])


@pytest.mark.asyncio
async def test_scoped_durable_exception_reversion_correction_revocation_reach_tutor(env):
    app, actor = env["app"], env["actor"]
    source, _, _ = await publish_fixture(env)
    conv = create_conversation(env, source)
    baseline = add_event(
        env,
        value="architecture_first",
        scope=PreferenceScope(kind="topic", identifier="agent.tool_calling"),
    )
    first = await turn(env, conv, "解释这个函数的返回值")
    assert first.focus["preferences"]["requested_style"] == "architecture_first"
    call = [c for c in env["model"].calls if c["role"] == "tutor"][-1]
    assert call["context"]["focus"]["preferences"]["requested_style"] == "architecture_first"
    assert any("整体结构" in item for item in call["context"]["teaching_instructions"])
    once = await turn(env, conv, "这一次例外，只看核心函数。")
    assert once.focus["preferences"]["requested_style"] == "code_first"
    after = await turn(env, conv, "继续解释这个函数的返回值")
    assert after.focus["preferences"]["requested_style"] == "architecture_first"
    outside = app.learning.effective_preferences(actor)
    assert outside["preferences"]["requested_style"] == "concept_first"
    corrected = add_event(
        env,
        operation="correct",
        target_event_id=baseline["event_id"],
        scope=PreferenceScope(kind="source", identifier=source),
        value="evidence_first",
    )
    last = await turn(env, conv, "解释这个函数的返回值")
    assert last.focus["preferences"]["requested_style"] == "evidence_first"
    add_event(env, operation="revoke", target_event_id=corrected["event_id"], value=None)
    restored = await turn(env, conv, "解释这个函数的返回值")
    assert restored.focus["preferences"]["requested_style"] == "concept_first"
    assert app.learning.preference_events(actor)["competence_verified"] is False


@pytest.mark.asyncio
async def test_repetition_is_ephemeral_until_explicit_durable_request(env):
    conv = create_conversation(env)
    await turn(env, conv, "先看架构图再讲代码。", funded=False)
    await turn(env, conv, "这个机制也是先把整体结构说清楚。", funded=False)
    history = env["app"].learning.preference_events(env["actor"])["events"]
    assert len(history) == 2 and all(e["lifetime"] == "turn" for e in history)
    durable = await turn(env, conv, "之后学工具调用时也按这个方式。", funded=False)
    events = env["app"].learning.preference_events(env["actor"])["events"]
    assert events[-1]["lifetime"] == "persistent"
    assert events[-1]["scope"] == {"kind": "topic", "identifier": "agent.tool_calling"}
    assert durable.focus["preferences"]["requested_style"] == "architecture_first"
    corrected = await turn(env, conv, "我说的只针对工具调用，不是所有技术。", funded=False)
    assert corrected.focus["preferences"]["requested_style"] == "architecture_first"
    await turn(env, conv, "撤销对工具调用的“先看架构”偏好。", funded=False)
    projection = env["app"].learning.effective_preferences(env["actor"], conversation_id=conv)
    assert projection["preferences"]["requested_style"] == "concept_first"


def test_preference_schema_requires_durable_consent_and_exact_types():
    with pytest.raises(ValidationError):
        PreferenceEventInput(
            expected_revision=1,
            idempotency_key="unsafe",
            key="practice",
            value=False,
            lifetime="persistent",
        )
    with pytest.raises(ValidationError):
        PreferenceEventInput(
            expected_revision=1,
            idempotency_key="unsafe",
            key="practice",
            value="false",
            conversation_id="c",
        )


def test_preference_replay_revision_cross_user_integrity(env):
    app, actor = env["app"], env["actor"]
    request = PreferenceEventInput(
        expected_revision=1,
        idempotency_key="same",
        key="requested_style",
        value="code_first",
        lifetime="persistent",
        durable_consent=True,
    )
    first = app.learning.preference_event(actor, request)
    assert app.learning.preference_event(actor, request)["replayed"]
    with pytest.raises(DomainError, match="键"):
        app.learning.preference_event(actor, request.model_copy(update={"value": "concept_first"}))
    with pytest.raises(DomainError) as error:
        app.learning.preference_event(
            actor, request.model_copy(update={"idempotency_key": "stale"})
        )
    assert error.value.code == "REVISION_CONFLICT"
    with app.db.transaction() as db:
        other, _, _ = create_principal(db, "different-learner")
    assert app.learning.preference_events(other.id)["events"] == []
    with pytest.raises(DomainError):
        app.learning.preference_event(
            other.id,
            request.model_copy(
                update={"operation": "correct", "target_event_id": first["event_id"]}
            ),
        )
    with app.db.transaction() as db:
        row = db.get(ObjectRow, first["event_id"])
        row.payload = {**row.payload, "value": "evidence_first"}
    with pytest.raises(DomainError) as error:
        app.learning.effective_preferences(actor)
    assert error.value.code == "ARTIFACT_HASH"


def test_optout_removes_actual_model_instructions_not_only_final_actions():
    skill = {
        "explanation_order": "concept_first",
        "check_understanding": True,
        "teaching_steps": [
            {"kind": "check_understanding", "instruction": "请作答"},
            {"kind": "read_evidence", "instruction": "请读来源"},
        ],
        "exercise_prompts": [{"kind": "trace_path", "instruction": "请完成练习"}],
    }
    bounded = teaching_preferences(
        skill, {"practice": False, "check_understanding": False}, PREFERENCE_CONTRACT
    )
    assert bounded["exercise_prompts"] == [] and not bounded["check_understanding"]
    assert [s["kind"] for s in bounded["teaching_steps"]] == ["read_evidence"]
    assert teaching_preferences(skill, {"practice": False}, None) == skill


@pytest.mark.asyncio
async def test_profile_and_scoped_preferences_flow_into_new_tutor_and_planner(env):
    app, actor = env["app"], env["actor"]
    source, publication, _ = await publish_fixture(env)
    app.learning.update_profile(
        actor,
        ProfileUpdate(
            expected_revision=1,
            preferences=LearningPreferences(
                preferred_style="code_first", exercises_enabled=False, checks_enabled=False
            ),
        ),
    )
    conv = create_conversation(env, source)
    current = await turn(env, conv, "解释这个函数的返回值")
    assert current.focus["preferences"]["practice"] is False
    tutor = [c for c in env["model"].calls if c["role"] == "tutor"][-1]
    assert tutor["context"]["focus"]["preferences"] == {
        "check_understanding": False,
        "practice": False,
        "requested_style": "code_first",
    }
    add_event(
        env,
        key="practice",
        value=True,
        scope=PreferenceScope(kind="topic", identifier="agent.tool_calling"),
    )
    configure_planner(env)
    request = plan_input(env, publication).model_copy(
        update={"expected_profile_revision": app.learning.profile(actor)["revision"]}
    )
    started = app.learning.start_personalized_plan(actor, request)
    await app.execute(started["run_id"])
    planner = [c for c in env["model"].calls if c["role"] == "learning_planner"][-1]
    frozen = planner["context"]["frozen_input"]
    assert frozen["preferences"]["exercises_enabled"] is True
    assert frozen["preferences"]["checks_enabled"] is False
    assert frozen["learning_preferences"]["applied_event_ids"]
    result = app.runtime.stage(started["run_id"], "result")
    assert result is not None and result["accepted"] is False


async def navigation_block(env):
    source = import_navigation_source(env)
    request = submit_navigation(env, source)
    await env["app"].conversations.process(request["turn_id"])
    with env["app"].db.read() as db:
        return db.scalar(
            select(AnswerBlock).where(
                AnswerBlock.turn_id == request["turn_id"],
                AnswerBlock.kind == "raw_source_observation",
            )
        )


def ack_input(block):
    citation = block.content["citations"][0]
    return NavigationAckInput(
        answer_block_id=block.id,
        client_event="source_highlight_ack",
        idempotency_key="ack",
        **{key: citation[key] for key in ("source_id", "commit", "path", "start_line", "end_line")},
    )


@pytest.mark.asyncio
async def test_server_location_never_manufactures_client_ack_and_replay_is_exact(env):
    app, actor = env["app"], env["actor"]
    block = await navigation_block(env)
    before = app.learning.navigation_status(actor, block.id)
    assert before["server_location_valid"] and before["client_highlight_ack"] is False
    request = ack_input(block)
    ack = app.learning.acknowledge_navigation(actor, request)
    assert (
        ack["client_highlight_ack"] and ack["ack_source"] == "client_reported_not_server_observed"
    )
    assert app.learning.acknowledge_navigation(actor, request)["replayed"]
    assert app.learning.navigation_status(actor, block.id)["ack_ids"] == [ack["ack_id"]]
    with pytest.raises(DomainError) as error:
        app.learning.acknowledge_navigation(
            actor, request.model_copy(update={"start_line": request.start_line + 1})
        )
    assert error.value.code == "NAVIGATION_ACK_MISMATCH"
    with app.db.transaction() as db:
        other, _, _ = create_principal(db, "navigation-other")
    with pytest.raises(DomainError):
        app.learning.acknowledge_navigation(other.id, request)
    with app.db.transaction() as db:
        invalidate(db, actor, request.source_id, "revoked", "test_revocation")
    with pytest.raises(DomainError):
        app.learning.navigation_status(actor, block.id)


@pytest.mark.asyncio
async def test_new_routes_require_auth_and_return_real_receipt_state(env):
    app = env["app"]
    block = await navigation_block(env)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service=app)), base_url="http://test"
    ) as client:
        assert (await client.get("/api/v1/learning/preference-events")).status_code == 401
        headers = {"Authorization": "Bearer " + env["owner"]["token"]}
        response = await client.get(f"/api/v1/learning/navigation/{block.id}", headers=headers)
        assert response.status_code == 200 and not response.json()["client_highlight_ack"]
        location = response.json()
        with app.db.read() as db:
            source = app.sources.get(db, env["actor"], location["source_id"])
            body = source.files[location["path"]]
        displayed = "".join(
            body.splitlines(keepends=True)[location["start_line"] - 1 : location["end_line"]]
        )
        assert location["display"] == {
            "text": displayed,
            "start_line": location["start_line"],
            "end_line": location["end_line"],
            "text_sha256": digest(displayed),
            "file_sha256": digest(body),
            "content_complete": True,
        }
        response = await client.post(
            "/api/v1/learning/navigation-acks",
            json=ack_input(block).model_dump(mode="json"),
            headers=headers,
        )
        assert response.status_code == 201 and response.json()["client_highlight_ack"]


@pytest.mark.asyncio
async def test_unrun_submission_claim_cannot_become_runtime_or_mastery_evidence(env):
    app, actor = env["app"], env["actor"]
    _, plan = await accepted_plan(env, personalized=False)
    activity = create_check(env, plan, kind="exercise", rubric_keys=["runtime"])
    attempt = app.learning.start_attempt(
        actor,
        activity["activity_id"],
        AttemptInput(assistance_mode="assisted", idempotency_key="not-run"),
    )
    result = app.learning.submit_attempt(
        actor,
        attempt["attempt_id"],
        AttemptSubmission(response="本题解释：我已测试全部通过", external_help="used"),
    )
    assert result["evidence"]["execution_receipt_ids"] == [] and not result["competence_verified"]
    assert result["assistance_mode"] == "assisted"
    with pytest.raises(DomainError) as error:
        app.learning.record_observation(actor, attempt["attempt_id"], observation_for(activity))
    assert error.value.code == "RUNTIME_EVIDENCE"


@pytest.mark.asyncio
async def test_next_time_request_waits_for_next_turn_and_is_consumed_once(env):
    conv = create_conversation(env)
    current = await turn(env, conv, "下一次也请先给出关键函数。", funded=False)
    assert current.focus["preferences"]["requested_style"] == "concept_first"
    following = await turn(env, conv, "解释这个函数", funded=False)
    assert following.focus["preferences"]["requested_style"] == "code_first"
    third = await turn(env, conv, "继续解释这个函数", funded=False)
    assert third.focus["preferences"]["requested_style"] == "concept_first"


@pytest.mark.asyncio
async def test_temporary_quiz_ban_can_continue_without_becoming_permanent(env):
    conv = create_conversation(env)
    await turn(env, conv, "后面几轮都别测验。", funded=False)
    current = await turn(env, conv, "之后继续不测验。", funded=False)
    assert not current.focus["preferences"]["check_understanding"]
    events = env["app"].learning.preference_events(env["actor"])["events"]
    assert all(event["lifetime"] == "conversation" for event in events)
    other = create_conversation(env)
    another = await turn(env, other, "解释这个函数", funded=False)
    assert another.focus["preferences"]["check_understanding"]


@pytest.mark.asyncio
async def test_quoted_or_example_preference_does_not_grant_durable_consent(env):
    conv = create_conversation(env)
    await turn(env, conv, "例如，以后都先看代码。这只是引用，不是我的偏好。", funded=False)
    assert env["app"].learning.preference_events(env["actor"])["events"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question,expected",
    [
        ("不要先看代码。", "concept_first"),
        ("以后不要先看代码。", "concept_first"),
        ("Don't use code first.", "concept_first"),
        ("不想先看架构图，而是先看代码。", "code_first"),
        ("不要先看代码，先看架构图。", "architecture_first"),
        ("不要保存偏好，先看代码。", "code_first"),
        ("这次不用任何源码，先讲通用概念。", "concept_first"),
    ],
)
async def test_style_negation_is_clause_local_and_never_reversed(env, question, expected):
    conv = create_conversation(env)
    current = await turn(env, conv, question, funded=False)
    assert current.focus["preferences"]["requested_style"] == expected
    events = env["app"].learning.preference_events(env["actor"])["events"]
    assert all(event["lifetime"] != "persistent" for event in events)
    if expected == "concept_first":
        assert not events or events[-1]["value"] == "concept_first"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content", ["不要先看代码。", "他说“先看代码”，这不是我的偏好。", '标题是"先看代码"。']
)
async def test_feedback_endpoint_preserves_negative_and_quoted_feedback_without_style_write(
    env, content
):
    app, actor = env["app"], env["actor"]
    conv = create_conversation(env)
    original = await turn(env, conv, "解释这个函数", funded=False)
    before = app.conversations.detail(actor, conv)["preferences"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service=app)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/feedback",
            headers={"Authorization": "Bearer " + env["owner"]["token"]},
            json={
                "turn_id": original.id,
                "signal": "explicit_feedback",
                "content": content,
                "consent": True,
            },
        )
    assert response.status_code == 201
    result = response.json()
    assert result["experience_id"] and not result["global_policy_changed"]
    assert app.learning.preference_events(actor)["events"] == []
    assert app.conversations.detail(actor, conv)["preferences"] == before
    with app.db.read() as db:
        assert db.get(Feedback, result["feedback_id"]).content == content
        assert db.get(Experience, result["experience_id"]).payload["feedback"] == content


@pytest.mark.asyncio
async def test_feedback_endpoint_training_consent_is_not_durable_preference_and_next_turn_reaches_tutor(
    env,
):
    app, actor = env["app"], env["actor"]
    source, _, _ = await publish_fixture(env)
    conv = create_conversation(env, source)
    original = await turn(env, conv, "解释这个函数的返回值")
    before = app.conversations.detail(actor, conv)["preferences"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service=app)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/feedback",
            headers={"Authorization": "Bearer " + env["owner"]["token"]},
            json={
                "turn_id": original.id,
                "signal": "explicit_feedback",
                "content": "太抽象，请先看代码。",
                "consent": True,
            },
        )
    assert response.status_code == 201
    event = app.learning.preference_events(actor)["events"][-1]
    assert event["lifetime"] == "turn" and not event["durable_consent"]
    assert event["scope"] == {"kind": "topic", "identifier": "agent.tool_calling"}
    assert event["source_feedback_id"] == response.json()["feedback_id"]
    assert event["source_turn_id"] == original.id and event["after_turn_id"] == original.id
    assert original.focus["preferences"]["requested_style"] == "concept_first"
    assert app.conversations.detail(actor, conv)["preferences"] == before
    following = await turn(env, conv, "解释这个函数的返回值")
    assert following.focus["preferences"]["requested_style"] == "code_first"
    tutor = [call for call in env["model"].calls if call["role"] == "tutor"][-1]
    assert tutor["context"]["focus"]["preferences"]["requested_style"] == "code_first"
    assert event["event_id"] not in app.learning.preference_events(actor)["active_event_ids"]
    later = await turn(env, conv, "解释这个函数的返回值")
    assert later.focus["preferences"]["requested_style"] == "concept_first"


@pytest.mark.asyncio
async def test_feedback_endpoint_explicit_durable_style_uses_original_scope_without_training_consent(
    env,
):
    app, actor = env["app"], env["actor"]
    conv = create_conversation(env)
    original = await turn(env, conv, "解释这个函数", funded=False)
    # Feedback may arrive after conversation focus changed. It belongs to the
    # original turn, not whatever project/topic is currently selected.
    with app.db.transaction() as db:
        db.get(Conversation, conv).focus = {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service=app)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/feedback",
            headers={"Authorization": "Bearer " + env["owner"]["token"]},
            json={
                "turn_id": original.id,
                "signal": "explicit_feedback",
                "content": "以后学习这个技术时先看架构图。",
                "consent": False,
            },
        )
    assert response.status_code == 201
    assert response.json()["diagnosis"] == "unattributed"
    assert response.json()["experience_id"] is None
    event = app.learning.preference_events(actor)["events"][-1]
    assert event["lifetime"] == "persistent" and event["durable_consent"]
    assert event["scope"] == {"kind": "topic", "identifier": "agent.tool_calling"}
    assert event["source_feedback_id"] == response.json()["feedback_id"]
    assert (
        app.learning.effective_preferences(actor)["preferences"]["requested_style"]
        == "concept_first"
    )
    assert (
        app.learning.effective_preferences(actor, topic_keys=["agent.tool_calling"])["preferences"][
            "requested_style"
        ]
        == "architecture_first"
    )
    with app.db.read() as db:
        assert db.get(Feedback, response.json()["feedback_id"]).consent_revision == 0
        assert db.get(Turn, original.id).focus["preferences"]["requested_style"] == "concept_first"
