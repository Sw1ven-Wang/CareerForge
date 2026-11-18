"""Layered control/constraint tests; no private evaluator or model calls."""

from __future__ import annotations

from copy import deepcopy

import httpx
import pytest
from test_preference_authorization_rc3 import baseline, new_events, submit
from test_preference_controls_rc5 import TOPICS, snapshot

from careerforge.api import create_app
from careerforge.models import Conversation, Turn


def persistent_state(env):
    app, actor = env["app"], env["actor"]
    ledger = app.learning.preference_events(actor)
    persistent = [event for event in ledger["events"] if event["lifetime"] == "persistent"]
    return {
        "events": persistent,
        "active_ids": [
            event["event_id"]
            for event in persistent
            if event["event_id"] in ledger["active_event_ids"]
        ],
        "scopes": {
            topic or "global": {
                key: value
                for key, value in app.learning.effective_preferences(
                    actor, topic_keys=[topic] if topic else []
                ).items()
                if key != "revision"
            }
            for topic in (None, *TOPICS)
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "不要改我的长期偏好，这一次先看代码。",
        "请保留原来的长期设置，但这次先给我关键代码。",
        "别修改我的永久偏好，本次先看代码。",
        "我的长期偏好保持不变。这一次先展示代码。",
        "这一次先看代码，但不要改我的长期偏好。",
        "不要更新我原来的长期设置，不过仅这一次先看代码。",
        "请保持我的长期偏好不变，这一次例外，只看核心函数。",
        "不要改我的长期偏好。这一次，先看代码。",
        "Keep my long-term preferences unchanged. This time use code first.",
        "Do not change my persistent settings, please just this time use code first.",
        "Preserve my permanent preferences. Only this time show me code first.",
        "This time use code first, but leave my saved preferences intact.",
    ],
)
async def test_preserve_persistent_layer_while_applying_positive_one_turn_override(env, question):
    app, actor = env["app"], env["actor"]
    conv, original = baseline(env)
    before = persistent_state(env)
    current = submit(env, conv, question)
    events = new_events(env, original)
    assert len(events) == 1
    event = events[0]
    assert event["operation"] == "set" and event["value"] == "code_first"
    assert event["lifetime"] == "turn" and event["durable_consent"] is False
    assert event["scope"] == {"kind": "global", "identifier": None}
    assert event["applies_to_turn_id"] == current.id and event["after_turn_id"] is None
    assert not current.focus["preference_clarification"]
    assert current.focus["preferences"]["requested_style"] == "code_first"
    assert persistent_state(env) == before
    audit = current.focus["preference_control_parse"]["controls"]
    assert any(
        control["operation"] == "protect" and control["protected_lifetimes"] == ["persistent"]
        for control in audit
    )
    assert any(
        control["operation"] == "set" and control["lifetime_hints"] == ["turn"] for control in audit
    )
    await app.conversations.process(current.id)
    later = submit(env, conv, "解释这个函数")
    assert later.focus["preferences"]["requested_style"] == "architecture_first"
    assert event["event_id"] not in later.focus["learning_preferences"]["applied_event_ids"]
    assert original["event_id"] in later.focus["learning_preferences"]["applied_event_ids"]
    assert persistent_state(env) == before
    assert app.learning.preference_events(actor)["revision"] == original["revision"] + 1
    assert env["model"].calls == []


@pytest.mark.parametrize(
    "question",
    [
        "不要改我的长期偏好，以后都先看代码。",
        "保持我的长期设置不变，请记住我的偏好：先看代码。",
        "不要改我的长期偏好，这次先看代码，以后都别测验。",
        "这一次以后都先看代码。",
        "以后这一次先看代码。",
        "以后都先看代码，这一次也先看代码。",
        "这一次先看代码，以后都先看代码。",
        "这一次先看代码，以后都先看架构。",
        "请记住我的偏好：这一次先看代码。",
        "这一次，请记住我的偏好：先看代码。",
        "这一次，后面几轮，先看代码。",
        "下一次，这一次先看代码。",
        "不要修改我的偏好，这一次先看代码。",
        "Keep my long-term settings unchanged. Always use code first.",
        "Please remember my preference: this time use code first.",
        "This time use code first. From now on use code first.",
        "不要改我的长期偏好，撤销全局的架构优先偏好。",
        "不要改我的长期偏好，请把原来的架构优先偏好改成只对 Python 生效。",
        "这一次撤销全局的架构优先偏好。",
        "这一次，撤销全局的架构优先偏好。",
        "这次请把原来的架构优先偏好改成只对 Python 生效。",
        "This time revoke my global architecture first preference.",
    ],
)
def test_conflicting_lifetimes_or_protected_state_are_atomic_noops(env, question):
    conv, _ = baseline(env)
    before = snapshot(env, conv)
    current = submit(env, conv, question)
    assert snapshot(env, conv) == before
    assert current.focus["preference_clarification"]
    assert "当前设置未改变" in current.focus["preference_clarification"]
    assert env["model"].calls == []


@pytest.mark.parametrize(
    "question",
    [
        "同事说不要改我的长期偏好，这一次先看代码。",
        "说明书写着：不要改我的长期偏好。这一次先看代码。",
        "“不要改我的长期偏好，这一次先看代码。”这是例句。",
        "假设我要求不要改长期偏好，这一次先看代码，你会怎么做？",
        "为什么有人建议不要改长期偏好，这一次先看代码？",
        "Someone said, keep my long-term settings unchanged. This time use code first.",
        "The guide says: do not change my persistent preferences, this time use code first.",
        "Please explain `do not change my long-term preferences, this time use code first`.",
    ],
)
def test_reported_or_quoted_control_constraints_cannot_authorize_an_exception(env, question):
    conv, _ = baseline(env)
    before = snapshot(env, conv)
    submit(env, conv, question)
    assert snapshot(env, conv) == before
    assert env["model"].calls == []


@pytest.mark.parametrize(
    "question", ["这次先看代码，以后都别测验。", "以后都别测验，这次先看代码。"]
)
def test_distinct_setting_keys_keep_their_own_explicit_lifetimes(env, question):
    conv, original = baseline(env)
    current = submit(env, conv, question)
    events = {event["key"]: event for event in new_events(env, original)}
    assert set(events) == {"requested_style", "check_understanding"}
    assert events["requested_style"]["lifetime"] == "turn"
    assert not events["requested_style"]["durable_consent"]
    assert events["check_understanding"]["lifetime"] == "persistent"
    assert events["check_understanding"]["durable_consent"]
    assert not current.focus["preference_clarification"]
    assert current.focus["preferences"]["requested_style"] == "code_first"
    assert current.focus["preferences"]["check_understanding"] is False
    assert (
        env["app"].learning.effective_preferences(env["actor"])["preferences"]["requested_style"]
        == "architecture_first"
    )


def test_protection_without_a_positive_change_is_not_a_scope_request(env):
    conv, _ = baseline(env)
    before = snapshot(env, conv)
    current = submit(env, conv, "请保留我的长期偏好不变。")
    assert snapshot(env, conv) == before
    assert not current.focus["preference_clarification"]


@pytest.mark.asyncio
async def test_next_turn_override_after_protection_is_scheduled_and_consumed_once(env):
    app = env["app"]
    conv, original = baseline(env)
    before = persistent_state(env)
    current = submit(env, conv, "不要改我的长期偏好，下次先看代码。")
    event = new_events(env, original)[0]
    assert event["lifetime"] == "turn" and not event["durable_consent"]
    assert event["after_turn_id"] == current.id and event["applies_to_turn_id"] is None
    assert not current.focus["preference_clarification"]
    assert current.focus["preferences"]["requested_style"] == "architecture_first"
    await app.conversations.process(current.id)
    following = submit(env, conv, "解释这个函数")
    assert following.focus["preferences"]["requested_style"] == "code_first"
    await app.conversations.process(following.id)
    later = submit(env, conv, "解释这个函数")
    assert later.focus["preferences"]["requested_style"] == "architecture_first"
    assert persistent_state(env) == before


@pytest.mark.asyncio
async def test_conversation_only_override_does_not_touch_persistent_state(env):
    conv, original = baseline(env)
    before = persistent_state(env)
    current = submit(env, conv, "保留我的长期设置，这次会话先看代码。")
    event = new_events(env, original)[0]
    assert event["lifetime"] == "conversation" and not event["durable_consent"]
    assert current.focus["preferences"]["requested_style"] == "code_first"
    assert not current.focus["preference_clarification"]
    assert persistent_state(env) == before
    await env["app"].conversations.process(current.id)
    other = env["app"].conversations.create(env["actor"])
    later = submit(env, other, "解释这个函数")
    assert later.focus["preferences"]["requested_style"] == "architecture_first"


@pytest.mark.asyncio
@pytest.mark.parametrize("consent", [False, True])
async def test_feedback_uses_constraint_and_exception_separately_from_training_consent(
    env, consent
):
    app = env["app"]
    conv, original = baseline(env)
    current = submit(env, conv, "解释这个函数")
    frozen = deepcopy(current.focus)
    before = persistent_state(env)
    with app.db.transaction() as db:
        db.get(Conversation, conv).focus = {"topic_keys": ["code.python"]}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service=app)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/feedback",
            headers={"Authorization": "Bearer " + env["owner"]["token"]},
            json={
                "turn_id": current.id,
                "signal": "explicit_feedback",
                "content": "不要改我的长期偏好，这一次先看代码。",
                "consent": consent,
            },
        )
    assert response.status_code == 201
    assert response.json()["preference_update_applied"]
    assert not response.json()["preference_clarification"]
    events = new_events(env, original)
    assert len(events) == 1 and events[0]["lifetime"] == "turn" and not events[0]["durable_consent"]
    assert events[0]["scope"] == {"kind": "topic", "identifier": "agent.tool_calling"}
    assert events[0]["after_turn_id"] == current.id
    assert persistent_state(env) == before
    with app.db.read() as db:
        assert db.get(Turn, current.id).focus == frozen
    assert env["model"].calls == []
