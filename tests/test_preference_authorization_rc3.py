"""Public independent reproductions and generic authorization-boundary variants."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select
from test_learning_events_v166 import add_event, create_conversation

from careerforge.api import create_app
from careerforge.contracts import TurnInput
from careerforge.contracts_learning_events_v166 import PreferenceScope
from careerforge.models import AnswerBlock, Experience, Feedback, Turn


def baseline(env):
    event = add_event(env, value="architecture_first")
    return create_conversation(env), event


def submit(env, conv, text):
    revision = env["app"].conversations.detail(env["actor"], conv)["revision"]
    result = env["app"].conversations.submit(
        env["actor"],
        conv,
        TurnInput(
            question=text, expected_revision=revision, idempotency_key=f"rc3-turn-{revision}"
        ),
    )
    with env["app"].db.read() as db:
        return db.get(Turn, result["turn_id"])


def new_events(env, original):
    return [
        event
        for event in env["app"].learning.preference_events(env["actor"])["events"]
        if event["revision"] > original["revision"]
    ]


@pytest.mark.parametrize(
    "question",
    [
        "不要撤销我以前设置的偏好。",
        "我不想让你撤销之前的设置，只问这个函数是什么意思。",
        "请勿取消之前的偏好。",
        "禁止撤销这个设置。",
        "我不希望忘掉这个设置。",
        "Do not revoke my preference.",
        "Don't remove the preference.",
        "Never forget my preference.",
        "不要只针对当前技术。",
        "Don't apply it only to this topic.",
        "不要取消测验设置。",
    ],
)
def test_negated_preference_operations_do_not_change_any_event_or_revision(env, question):
    conv, original = baseline(env)
    submit(env, conv, question)
    assert new_events(env, original) == []
    assert env["app"].learning.profile(env["actor"])["revision"] == original["revision"]
    assert (
        env["app"].learning.effective_preferences(env["actor"])["preferences"]["requested_style"]
        == "architecture_first"
    )
    assert env["model"].calls == []


@pytest.mark.parametrize(
    "quoted",
    [
        "`以后都先看代码`",
        "``以后都先看代码``",
        '"以后都先看代码"',
        "'以后都先看代码'",
        "“以后都先看代码”",
        "‘以后都先看代码’",
        "「以后都先看代码」",
        "『以后都先看代码』",
        "《以后都先看代码》",
        "```text\n以后都先看代码\n```",
        "~~~text\n以后都先看代码\n~~~",
        "`以后都先看代码",
        "“以后都先看代码",
        "```text\n以后都先看代码",
        '"以后都先看代码',
        "'don't revoke; 以后都先看代码'",
    ],
)
def test_quoted_preference_names_never_supply_value_or_durable_consent(env, quoted):
    conv, original = baseline(env)
    submit(env, conv, f"命令名称是 {quoted}，请解释它。")
    assert new_events(env, original) == []
    assert (
        env["app"].learning.effective_preferences(env["actor"])["preferences"]["requested_style"]
        == "architecture_first"
    )
    assert env["model"].calls == []


@pytest.mark.parametrize(
    "question",
    [
        "名称是 `以后`，先看代码。",
        "以后再研究这个函数。先看代码。",
        "以后不要先看架构，先看代码。",
        "不要保存这个偏好，以后先看代码。",
    ],
)
@pytest.mark.asyncio
async def test_unrelated_quoted_or_negated_future_text_cannot_promote_one_turn_style(env, question):
    conv, original = baseline(env)
    current = submit(env, conv, question)
    events = new_events(env, original)
    assert len(events) == 1 and events[0]["key"] == "requested_style"
    assert events[0]["value"] == "code_first" and events[0]["lifetime"] == "turn"
    assert not events[0]["durable_consent"]
    assert current.focus["preferences"]["requested_style"] == "code_first"
    await env["app"].conversations.process(current.id)
    later = submit(env, conv, "解释这个函数")
    assert later.focus["preferences"]["requested_style"] == "architecture_first"


def test_persistence_is_bound_per_setting_not_borrowed_from_other_clause(env):
    conv, original = baseline(env)
    submit(env, conv, "以后都别测验，先看代码。")
    events = {event["key"]: event for event in new_events(env, original)}
    assert events["requested_style"]["lifetime"] == "turn"
    assert not events["requested_style"]["durable_consent"]
    assert events["check_understanding"]["lifetime"] == "persistent"
    assert events["check_understanding"]["durable_consent"]


def test_positive_revoke_still_works_and_negative_quiz_clause_cannot_redirect_target(env):
    conv, original = baseline(env)
    submit(env, conv, "取消这个偏好，不要取消测验设置。")
    events = new_events(env, original)
    assert len(events) == 1 and events[0]["operation"] == "revoke"
    assert (
        events[0]["key"] == "requested_style"
        and events[0]["target_event_id"] == original["event_id"]
    )


def test_affirmative_alternative_can_apply_without_negated_revocation(env):
    conv, original = baseline(env)
    submit(env, conv, "不要撤销偏好，这一次先看代码。")
    events = new_events(env, original)
    assert len(events) == 1 and events[0]["operation"] == "set"
    assert events[0]["lifetime"] == "turn" and events[0]["value"] == "code_first"


@pytest.mark.asyncio
async def test_positive_scoped_correction_and_durable_style_are_not_disabled(env):
    conv, original = baseline(env)
    current = submit(env, conv, "我说的只针对工具调用，不是所有技术。")
    corrected = new_events(env, original)
    assert len(corrected) == 1 and corrected[0]["operation"] == "correct"
    assert corrected[0]["scope"] == {"kind": "topic", "identifier": "agent.tool_calling"}
    await env["app"].conversations.process(current.id)
    submit(env, conv, "以后学习工具调用都先看代码，不是所有技术。")
    durable = new_events(env, original)[-1]
    assert durable["operation"] == "set" and durable["lifetime"] == "persistent"
    assert durable["scope"] == {"kind": "topic", "identifier": "agent.tool_calling"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "不要撤销我以前设置的偏好。",
        "命令名称是 `以后都先看代码`，请解释它。",
        "Do not revoke my preference. The value is 'from now on code first'.",
    ],
)
async def test_actual_feedback_api_uses_the_same_quote_and_negation_boundary(env, content):
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
    assert response.status_code == 201 and response.json()["experience_id"]
    assert new_events(env, original) == []
    assert env["app"].learning.profile(env["actor"])["revision"] == original["revision"]
    assert env["model"].calls == []


def two_scopes(env, *, global_style="evidence_first"):
    topic = add_event(
        env,
        value="architecture_first",
        scope=PreferenceScope(kind="topic", identifier="agent.tool_calling"),
    )
    global_event = add_event(env, value=global_style)
    return create_conversation(env), topic, global_event


@pytest.mark.parametrize(
    "question",
    [
        "撤销对工具调用的“先看架构”偏好。",
        "撤销对工具调用的'先看架构'偏好。",
        "撤销对 Tool Calling 的 `architecture_first` 偏好。",
    ],
)
def test_named_scope_and_existing_value_select_exact_event_not_newest_global(env, question):
    conv, topic, global_event = two_scopes(env)
    submit(env, conv, question)
    events = new_events(env, global_event)
    assert len(events) == 1 and events[0]["target_event_id"] == topic["event_id"]
    assert events[0]["scope"] == topic["scope"]
    effective = env["app"].learning.effective_preferences(env["actor"], conversation_id=conv)
    assert effective["preferences"]["requested_style"] == "evidence_first"
    assert global_event["event_id"] in effective["applied_event_ids"]
    assert env["model"].calls == []


def test_same_value_in_two_scopes_still_requires_exact_scope(env):
    conv, topic, global_event = two_scopes(env, global_style="architecture_first")
    submit(env, conv, "撤销对工具调用的“先看架构”偏好。")
    events = new_events(env, global_event)
    assert len(events) == 1 and events[0]["target_event_id"] == topic["event_id"]
    assert (
        global_event["event_id"]
        in env["app"].learning.effective_preferences(env["actor"])["applied_event_ids"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "撤销这个偏好。",
        "撤销对工具调用的“先看代码”偏好。",
        "撤销对数据库技术的“先看架构”偏好。",
        "撤销“一个不存在的名称”偏好。",
        "撤销对工具调用的‘一个不存在的风格’偏好。",
    ],
)
async def test_missing_or_ambiguous_target_changes_nothing_and_asks_without_model(env, question):
    conv, _, latest = two_scopes(env)
    current = submit(env, conv, question)
    assert new_events(env, latest) == []
    assert current.focus["preference_clarification"]
    await env["app"].conversations.process(current.id)
    with env["app"].db.read() as db:
        block = db.scalar(select(AnswerBlock).where(AnswerBlock.turn_id == current.id))
        assert block.content["action_key"] == "clarify"
        assert not block.content["preference_update_applied"]
        assert "当前设置未改变" in block.content["text"]
    assert env["app"].learning.profile(env["actor"])["revision"] == latest["revision"]
    assert env["model"].calls == []


def test_explicit_global_or_event_identifier_is_not_replaced_by_current_topic(env):
    conv, _, global_event = two_scopes(env)
    submit(env, conv, "撤销全局的证据优先设置，先证据再结论的那个。")
    events = new_events(env, global_event)
    assert len(events) == 1 and events[0]["target_event_id"] == global_event["event_id"]
    other, topic, latest = two_scopes(env)
    submit(env, other, "撤销偏好事件 " + topic["event_id"])
    events = new_events(env, latest)
    assert len(events) == 1 and events[0]["target_event_id"] == topic["event_id"]


def test_single_quotes_without_chinese_spacing_are_still_data(env):
    conv, original = baseline(env)
    submit(env, conv, "命令名为'以后都先看代码'。")
    assert new_events(env, original) == []
    assert env["model"].calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_feedback_api_reports_target_resolution_and_preserves_feedback_experience(
    env, explicit
):
    app, actor = env["app"], env["actor"]
    conv, topic, latest = two_scopes(env)
    current = submit(env, conv, "解释这个函数")
    content = "撤销对工具调用的“先看架构”偏好。" if explicit else "撤销这个偏好。"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service=app)), base_url="http://test"
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
    assert result["feedback_id"] and result["experience_id"]
    assert result["preference_update_applied"] is explicit
    events = new_events(env, latest)
    if explicit:
        assert len(events) == 1 and events[0]["target_event_id"] == topic["event_id"]
        assert result["preference_event_ids"] == [events[0]["event_id"]]
        assert result["preference_clarification"] is None
    else:
        assert events == [] and result["preference_event_ids"] == []
        assert "当前设置未改变" in result["preference_clarification"]
        assert app.learning.profile(actor)["revision"] == latest["revision"]
    with app.db.read() as db:
        assert db.get(Feedback, result["feedback_id"]).content == content
        assert db.get(Experience, result["experience_id"]).payload["feedback"] == content
        assert (
            db.get(Turn, current.id).focus["preferences"]["requested_style"] == "architecture_first"
        )
    assert env["model"].calls == []
