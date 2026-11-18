"""Named scope and personal-consent regressions using real state/API paths."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select
from test_preference_authorization_rc3 import baseline, new_events, submit

from careerforge.api import create_app
from careerforge.models import AnswerBlock, Conversation, Experience, Feedback, Turn
from careerforge.services.dependencies import register
from careerforge.services.security import create_principal
from careerforge.services.taxonomy import topic_object_id


@pytest.mark.parametrize(
    "question",
    [
        "我说的只针对 Python，不是工具调用。",
        "这个偏好仅限于 Python，工具调用不适用。",
        "I meant only Python, not Tool Calling.",
        "Apply it only for Python, not for Tool Calling.",
    ],
)
def test_named_correction_moves_to_authorized_named_topic_never_current_focus(env, question):
    conv, original = baseline(env)
    submit(env, conv, question)
    events = new_events(env, original)
    assert len(events) == 1 and events[0]["operation"] == "correct"
    assert events[0]["scope"] == {"kind": "topic", "identifier": "code.python"}
    assert events[0]["target_event_id"] == original["event_id"]
    assert (
        env["app"].learning.effective_preferences(env["actor"], topic_keys=["code.python"])[
            "preferences"
        ]["requested_style"]
        == "architecture_first"
    )
    assert (
        env["app"].learning.effective_preferences(env["actor"], conversation_id=conv)[
            "preferences"
        ]["requested_style"]
        == "concept_first"
    )
    assert env["model"].calls == []


@pytest.mark.parametrize(
    "question",
    [
        "以后学习 Python 都先看代码，不要用于工具调用。",
        "今后学 Python 时先看代码，工具调用不适用。",
        "From now on use code first for Python, not for Tool Calling.",
        "For Python, from now on use code first. Do not apply it to Tool Calling.",
        "Always use code first for Python rather than Tool Calling.",
    ],
)
def test_positive_named_scope_and_negative_exclusion_do_not_borrow_focus(env, question):
    conv, original = baseline(env)
    submit(env, conv, question)
    events = new_events(env, original)
    assert len(events) == 1 and events[0]["operation"] == "set"
    assert events[0]["lifetime"] == "persistent" and events[0]["durable_consent"]
    assert events[0]["scope"] == {"kind": "topic", "identifier": "code.python"}
    assert (
        env["app"].learning.effective_preferences(env["actor"], topic_keys=["code.python"])[
            "preferences"
        ]["requested_style"]
        == "code_first"
    )
    assert (
        env["app"].learning.effective_preferences(env["actor"], conversation_id=conv)[
            "preferences"
        ]["requested_style"]
        == "architecture_first"
    )


@pytest.mark.parametrize(
    "question",
    [
        "如果有人要求以后都先看代码，你会怎么回答？",
        "假设我以后都先看代码，你觉得如何？",
        "同事说以后都先看代码，这是他的要求。",
        "李雷说以后都先看代码。",
        "如果以后学习 Python 都先看代码，会怎么样？",
        "If someone asks for code first from now on, what would you say?",
        "Suppose I said from now on use code first for Python.",
        "Imagine a user requests code first from now on.",
        "My colleague said from now on use code first for Python.",
        "Alice asked you to use code first from now on.",
        "有人要求“以后都先看代码”，我是在转述。",
        "这是`以后学习 Python 都先看代码`这个命令名。",
    ],
)
def test_hypothetical_report_or_quote_never_authorizes_personal_change(env, question):
    conv, original = baseline(env)
    current = submit(env, conv, question)
    assert new_events(env, original) == []
    assert current.focus["preferences"]["requested_style"] == "architecture_first"
    assert env["app"].learning.profile(env["actor"])["revision"] == original["revision"]
    assert env["model"].calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "以后学习 Python 和 Go 都先看代码。",
        "From now on use code first for Python and Go.",
        "以后学习 Python 都先看代码，不要用于 Python。",
        "以后所有技术都先看代码，但不要用于工具调用。",
        "以后都先看代码，不要用于工具调用。",
        "以后学习 Zig 都先看代码。",
        "From now on use code first for Python and Zig.",
        "我说的只针对 Zig，不是工具调用。",
        "以后只在 'Python' 先看代码。",
        "以后学习 'Python' 都先看代码。",
    ],
)
async def test_conflicting_unknown_or_unresolved_scope_is_clarified_without_mutation(env, question):
    conv, original = baseline(env)
    current = submit(env, conv, question)
    assert new_events(env, original) == []
    assert current.focus["preference_clarification"]
    await env["app"].conversations.process(current.id)
    with env["app"].db.read() as db:
        block = db.scalar(select(AnswerBlock).where(AnswerBlock.turn_id == current.id))
        assert (
            block.content["action_key"] == "clarify"
            and not block.content["preference_update_applied"]
        )
    assert env["app"].learning.profile(env["actor"])["revision"] == original["revision"]
    assert env["model"].calls == []


def test_explicit_deictic_and_global_requests_still_work(env):
    conv, original = baseline(env)
    submit(env, conv, "以后学习这个主题时先看代码。")
    event = new_events(env, original)[0]
    assert event["scope"] == {"kind": "topic", "identifier": "agent.tool_calling"}
    other, prior = baseline(env)
    submit(env, other, "以后所有技术都先看代码。")
    event = new_events(env, prior)[0]
    assert event["scope"] == {"kind": "global", "identifier": None}


def test_multiple_current_topics_cannot_silently_promote_to_global(env):
    conv, original = baseline(env)
    with env["app"].db.transaction() as db:
        db.get(Conversation, conv).focus = {"topic_keys": ["agent.tool_calling", "code.python"]}
    current = submit(env, conv, "以后都先看代码。")
    assert new_events(env, original) == [] and current.focus["preference_clarification"]


def test_activity_negation_is_not_misread_as_excluding_its_topic(env):
    conv, original = baseline(env)
    submit(env, conv, "以后学习 Python 都不要测验。")
    event = new_events(env, original)[0]
    assert event["scope"] == {"kind": "topic", "identifier": "code.python"}
    assert event["key"] == "check_understanding" and event["value"] is False


@pytest.mark.parametrize(
    "question,topic_key",
    [
        ("以后学习 Go 都先看代码，不要用于工具调用。", "code.go"),
        ("From now on use code first for code.python, not for Tool Calling.", "code.python"),
    ],
)
def test_scope_uses_taxonomy_titles_and_canonical_keys_without_literal_input_rules(
    env, question, topic_key
):
    conv, original = baseline(env)
    submit(env, conv, question)
    events = new_events(env, original)
    assert len(events) == 1 and events[0]["scope"] == {"kind": "topic", "identifier": topic_key}


def test_direct_explanation_request_is_not_misread_as_someone_elses_report(env):
    conv, original = baseline(env)
    submit(env, conv, "这个机制也是先把整体结构说清楚。")
    events = new_events(env, original)
    assert len(events) == 1
    assert events[0]["key"] == "requested_style" and events[0]["value"] == "architecture_first"
    assert events[0]["lifetime"] == "turn" and not events[0]["durable_consent"]


def test_unavailable_private_topic_is_not_used_as_authorized_scope(env):
    # A schema row alone or another user's private topic cannot grant scope.
    from careerforge.models import Topic

    with env["app"].db.transaction() as db:
        other, ns, _ = create_principal(db, "other-private-scope")
        db.add(
            Topic(key="private.quartz", title="QuartzXYZ", aliases=["QuartzXYZ"], facet_schema={})
        )
        register(
            db,
            other.id,
            ns.id,
            "topic_version",
            {"key": "private.quartz", "title": "QuartzXYZ", "aliases": ["QuartzXYZ"]},
            topic_object_id("private.quartz"),
        )
    conv, original = baseline(env)
    current = submit(env, conv, "以后学习 QuartzXYZ 都先看代码。")
    assert new_events(env, original) == [] and current.focus["preference_clarification"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content,changed",
    [
        ("以后学习 Python 都先看代码，不要用于工具调用。", True),
        ("如果有人要求以后都先看代码，你会怎么回答？", False),
        ("以后学习 Python 和 Go 都先看代码。", False),
    ],
)
async def test_feedback_endpoint_uses_identical_scope_and_personal_consent_rules(
    env, content, changed
):
    conv, original = baseline(env)
    current = submit(env, conv, "解释这个函数")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service=env["app"])), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/feedback",
            headers={"Authorization": "Bearer " + env["owner"]["token"]},
            json={
                "turn_id": current.id,
                "signal": "explicit_feedback",
                "content": content,
                "consent": True,
            },
        )
    assert response.status_code == 201
    result = response.json()
    assert result["preference_update_applied"] is changed
    events = new_events(env, original)
    if changed:
        assert len(events) == 1 and events[0]["scope"] == {
            "kind": "topic",
            "identifier": "code.python",
        }
    else:
        assert events == []
    with env["app"].db.read() as db:
        assert db.get(Feedback, result["feedback_id"]).content == content
        assert db.get(Experience, result["experience_id"]).payload["feedback"] == content
        assert (
            db.get(Turn, current.id).focus["preferences"]["requested_style"] == "architecture_first"
        )
