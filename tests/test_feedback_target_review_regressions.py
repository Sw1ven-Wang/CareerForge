"""Bounded synthetic regressions for the two independently reported F blockers."""

import inspect
from copy import deepcopy

import pytest
from conftest import publish_fixture
from sqlalchemy import select
from test_feedback_target_v1 import (
    CONTENT,
    answer,
    client,
    feedback,
    new_conversation,
    target_object,
)

from careerforge.contracts_feedback_target_v1 import (
    FEEDBACK_TARGET_CHOICE,
    FEEDBACK_TARGET_CONTRACT,
    AnswerFeedbackConversationInput,
)
from careerforge.errors import DomainError
from careerforge.models import Conversation, Feedback, ObjectRow, Turn
from careerforge.services.feedback_target_v1 import creation_request
from careerforge.services.learning_events import capture_dialogue_preferences_in
from careerforge.services.ordinary_checker import ordinary_checker_identity


async def completed_target(env, topics):
    source, _, _ = await publish_fixture(env)
    conv = await new_conversation(env, source)
    with env["app"].db.transaction() as db:
        row = db.get(Conversation, conv)
        row.focus = {**row.focus, "topic_keys": topics, "topic_hints": topics}
    target, _ = await answer(env, conv)
    with env["app"].db.read() as db:
        task = db.get(ObjectRow, target.manifest_id).payload["task_spec"]
        assert target.focus["topic_keys"] == topics
        assert task["topic_keys"] == [] and task["topic_hints"] == ["runtime.async"]
        assert task["project_source_ids"] == [source]
    return source, conv, target


@pytest.mark.asyncio
@pytest.mark.parametrize("topics", [[], ["code.python", "agent.tool_calling"]])
async def test_unqualified_target_uses_verified_answer_not_stale_topic_cardinality(env, topics):
    app, actor = env["app"], env["actor"]
    source, conv, target = await completed_target(env, topics)
    original = deepcopy(target.focus)
    # Later navigation cannot provide or replace the target's provenance.
    with app.db.transaction() as db:
        row = db.get(Conversation, conv)
        row.focus = {**row.focus, "topic_keys": ["agent.tool_calling"], "topic_hints": []}
    result = await feedback(env, target, consent=False)
    assert result["answer_feedback"]["accepted"]
    assert result["preference_clarification"] is None
    assert result["experience_id"] is None
    obj = target_object(env, result)
    assert obj.payload["scope"] == {"kind": "target_answer"}
    assert obj.payload["target"]["source_ids"] == [source]
    assert obj.payload["lifetime"] == "turn" and not obj.payload["durable_consent"]
    assert app.learning.preference_events(actor)["events"] == []
    following, _ = await answer(env, conv)
    assert following.focus["preferences"]["requested_style"] == "code_first"
    tutor = [call for call in env["model"].calls if call["role"] == "tutor"][-1]
    assert tutor["context"]["focus"]["preferences"]["requested_style"] == "code_first"
    with app.db.read() as db:
        assert db.get(Turn, target.id).focus == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content,kind",
    [
        ("这一次讲解 runtime.async 时先看代码。", "topic"),
        ("这一次讲解这个项目时先看代码。", "source"),
        ("这一次讲解这个主题时先看代码。", None),
        ("这一次先看代码，但 Python 不适用。", None),
        ("这一次先看代码，以后都先看架构。", None),
        ("这一次讲解 UnknownTopic 时先看代码。", None),
        ("这一次先看代码，适用于所有主题。", None),
    ],
)
async def test_explicit_scopes_exclusions_and_lifetimes_survive_new_preparation(env, content, kind):
    source, _, target = await completed_target(env, ["code.python", "agent.tool_calling"])
    result = await feedback(env, target, content=content)
    assert result["answer_feedback"]["accepted"] is (kind is not None)
    if kind is None:
        assert result["preference_clarification"] and result["answer_feedback"]["binding"] is None
    else:
        scope = target_object(env, result).payload["scope"]
        assert scope == {"kind": kind, "identifier": "runtime.async" if kind == "topic" else source}
    assert env["app"].learning.preference_events(env["actor"])["events"] == []


@pytest.mark.asyncio
async def test_legacy_feedback_still_clarifies_ambiguous_stale_topic_fallback(env):
    _, _, target = await completed_target(env, ["code.python", "agent.tool_calling"])
    async with client(env) as c:
        response = await c.post(
            "/api/v1/feedback",
            json={
                "turn_id": target.id,
                "signal": "explicit_feedback",
                "content": CONTENT,
                "consent": False,
            },
        )
    assert response.status_code == 201
    result = response.json()
    assert result["preference_clarification"] and result["preference_event_ids"] == []
    assert "answer_feedback" not in result


def drift_evolution(monkeypatch):
    original = inspect.getsource

    def changed(obj):
        text = original(obj)
        return (
            text + "\n# Simulated producer revision\n"
            if getattr(obj, "__name__", None) == "careerforge.services.evolution"
            else text
        )

    monkeypatch.setattr(inspect, "getsource", changed)


def test_evolution_drift_changes_only_new_feedback_identity(monkeypatch):
    choice = AnswerFeedbackConversationInput(contract=FEEDBACK_TARGET_CONTRACT)
    before, ordinary_before = creation_request(choice), ordinary_checker_identity()
    drift_evolution(monkeypatch)
    assert creation_request(choice) != before
    assert ordinary_checker_identity() == ordinary_before


def test_frozen_new_choice_rejects_evolution_drift_without_reclassifying_default(env, monkeypatch):
    app, actor = env["app"], env["actor"]
    choice = AnswerFeedbackConversationInput(contract=FEEDBACK_TARGET_CONTRACT)
    chosen = app.conversations.create_answer_feedback(actor, choice)
    legacy = app.conversations.create(actor)
    drift_evolution(monkeypatch)
    with pytest.raises(DomainError) as exc:
        app.conversations.detail(actor, chosen)
    assert exc.value.code == "FEEDBACK_TARGET_BINDING"
    assert FEEDBACK_TARGET_CHOICE not in app.conversations.detail(actor, legacy)["preferences"]


@pytest.mark.parametrize("prepare,feedback_id", [(False, "feedback"), (True, None)])
def test_target_scope_switch_cannot_append_generic_preferences(prepare, feedback_id):
    with pytest.raises(DomainError) as exc:
        capture_dialogue_preferences_in(
            None,
            None,
            None,
            None,
            content=CONTENT,
            feedback_id=feedback_id,
            prepare_only=prepare,
            target_answer_scope=True,
        )
    assert exc.value.code == "FEEDBACK_TARGET_BINDING"


@pytest.mark.asyncio
async def test_unqualified_preparation_requires_completed_target_provenance_first(env):
    app = env["app"]
    _, _, target = await completed_target(env, ["code.python", "agent.tool_calling"])
    with app.db.transaction() as db:
        db.get(Turn, target.id).manifest_id = None
    calls = len(env["model"].calls)
    async with client(env) as c:
        response = await c.post(
            "/api/v2/feedback/answer-continuation",
            json={
                "contract": FEEDBACK_TARGET_CONTRACT,
                "turn_id": target.id,
                "content": CONTENT,
                "consent": False,
                "idempotency_key": "invalid-target",
            },
        )
    assert response.status_code == 409
    assert len(env["model"].calls) == calls
    with app.db.read() as db:
        assert not db.scalar(select(Feedback))
        assert not db.scalar(select(ObjectRow).where(ObjectRow.kind == "answer_feedback_target"))
