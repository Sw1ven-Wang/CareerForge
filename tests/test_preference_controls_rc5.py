"""Independent positive-consent and projection contracts, using real admissions.

Synthetic public-language variations only. No holdout/evaluator data or native
provider calls. Assertions cover the ledger and all representative effective
scopes, rather than trusting acknowledgements or parser output alone.
"""

from __future__ import annotations

from copy import deepcopy

import httpx
import pytest
from test_learning_events_v166 import add_event, create_conversation
from test_preference_authorization_rc3 import baseline, new_events, submit

from careerforge.api import create_app
from careerforge.contracts import ImportSourceInput
from careerforge.contracts_learning_events_v166 import PreferenceScope
from careerforge.models import Conversation, Feedback, Turn

TOPICS = ("agent.tool_calling", "code.python", "code.go")


def snapshot(env, conv):
    app, actor = env["app"], env["actor"]
    projections = {
        "global": app.learning.effective_preferences(actor),
        "conversation": app.learning.effective_preferences(actor, conversation_id=conv),
    }
    for topic in TOPICS:
        projections[topic] = app.learning.effective_preferences(actor, topic_keys=[topic])
        projections["conversation:" + topic] = app.learning.effective_preferences(
            actor, conversation_id=conv, topic_keys=[topic]
        )
    return {"ledger": app.learning.preference_events(actor), "projections": projections}


def style(env, topic=None):
    return env["app"].learning.effective_preferences(
        env["actor"], topic_keys=[topic] if topic else []
    )["preferences"]["requested_style"]


@pytest.mark.parametrize(
    "question",
    [
        "为什么有人认为以后都先看代码更好？",
        "说明书写着以后都先看代码，这是什么意思？",
        "以后都先看代码？",
        "请以后先看代码，你觉得好吗？",
        "我希望以后都先看代码这个建议可行吗。",
        "请把原来的架构优先偏好改成只对 Python 生效吗。",
        "请撤销我的偏好好吗。",
        "同学建议，今后都先看代码。",
        "同学建议，请以后都先看代码。",
        "同学建议如下。以后都先看代码。",
        "今后都先看代码。这是一段说明书文字。",
        "以后都先看代码，这是转述。",
        "Please always use code first?",
        "My colleague wrote, please always use code first.",
        "Please always use code first. This is a quotation from the guide.",
        "Suppose I ask you to always use code first.",
        "假设有人说，请把原来的架构优先偏好改成只对 Python 生效。",
        "如果我是你，我希望以后都先看代码。",
        "说明文字是：\n以后每次讲解都先给我关键代码。",
        "The instructions read:\nPlease always use code first.",
        "不要撤销我以前设置的偏好。",
        "命令名称是 `以后都先看代码`，请解释它。",
        "以后都先看代码。不要修改我的偏好。",
        "Always use code first. Do not change my settings.",
        "我正在翻译：请撤销原来的架构优先偏好。",
        "撤销原来的架构优先偏好。这是一个例句。",
        "请记住我的偏好：“以后都先看代码”。",
        "同事说，先看代码。",
    ],
)
def test_nonconsent_leaves_entire_ledger_and_every_projection_unchanged(env, question):
    conv, original = baseline(env)
    before = snapshot(env, conv)
    submit(env, conv, question)
    assert snapshot(env, conv) == before
    assert new_events(env, original) == []
    assert env["model"].calls == []


@pytest.mark.parametrize(
    "question",
    [
        "以后每次讲解都先给我关键代码，再解释原理。",
        "请今后所有主题都先看代码。",
        "我的偏好是以后所有技术都先看代码。",
        "Please remember my preference: always use code first for all topics.",
        "I want you to always use code first for all topics.",
        "Please use code first for all topics from now on.",
    ],
)
def test_positive_personal_global_directives_persist_without_scope_question(env, question):
    conv, original = baseline(env)
    current = submit(env, conv, question)
    events = new_events(env, original)
    assert len(events) == 1
    event = events[0]
    assert event["operation"] == "set" and event["value"] == "code_first"
    assert event["lifetime"] == "persistent" and event["durable_consent"]
    assert event["scope"] == {"kind": "global", "identifier": None}
    assert not current.focus["preference_clarification"]
    assert style(env) == "code_first"
    for topic in TOPICS:
        assert style(env, topic) == "code_first"
    assert current.focus["preference_control_parse"]["controls"]
    assert env["model"].calls == []


@pytest.mark.parametrize(
    "question",
    [
        "以后学 Python 时请先给我源码，Tool Calling 不用这个偏好。",
        "我希望今后学习 Python 都先展示代码，工具调用不适用。",
        "I want you to always use code first for Python, not for Tool Calling.",
    ],
)
def test_named_positive_scope_overrides_old_focus_without_changing_other_scopes(env, question):
    conv, original = baseline(env)
    current = submit(env, conv, question)
    events = new_events(env, original)
    assert len(events) == 1 and events[0]["scope"] == {"kind": "topic", "identifier": "code.python"}
    assert events[0]["lifetime"] == "persistent" and events[0]["durable_consent"]
    assert not current.focus["preference_clarification"]
    assert style(env, "code.python") == "code_first"
    for topic in (None, "agent.tool_calling", "code.go"):
        assert style(env, topic) == "architecture_first"


@pytest.mark.parametrize("temporary_override", [False, True])
def test_only_python_and_keep_tool_conflict_is_explicit_and_completely_nonmutating(
    env, temporary_override
):
    conv, _ = baseline(env)
    if temporary_override:
        add_event(
            env,
            value="architecture_first",
            lifetime="conversation",
            conversation_id=conv,
            scope=PreferenceScope(kind="topic", identifier="agent.tool_calling"),
        )
    before = snapshot(env, conv)
    current = submit(
        env,
        conv,
        "请把原来的全局架构优先偏好改成只对 Python 生效，工具调用不要改。"
        if temporary_override
        else "请把原来的架构优先偏好改成只对 Python 生效，工具调用不要改。",
    )
    assert current.focus["preference_clarification"]
    assert "冲突" in current.focus["preference_clarification"]
    assert snapshot(env, conv) == before
    assert env["model"].calls == []


@pytest.mark.parametrize("override_style", ["code_first", "architecture_first"])
def test_independent_durable_tool_override_allows_exact_global_narrowing(env, override_style):
    conv, original = baseline(env)
    preserved = add_event(
        env,
        value=override_style,
        scope=PreferenceScope(kind="topic", identifier="agent.tool_calling"),
    )
    before = snapshot(env, conv)
    current = submit(env, conv, "请把原来的架构优先偏好改成只对 Python 生效，工具调用不要改。")
    assert not current.focus["preference_clarification"]
    events = new_events(env, preserved)
    assert len(events) == 1 and events[0]["operation"] == "correct"
    assert events[0]["target_event_id"] == original["event_id"]
    assert events[0]["scope"] == {"kind": "topic", "identifier": "code.python"}
    after = snapshot(env, conv)
    assert after["ledger"]["revision"] == before["ledger"]["revision"] + 1
    assert original["event_id"] not in after["ledger"]["active_event_ids"]
    assert preserved["event_id"] in after["ledger"]["active_event_ids"]
    assert style(env, "agent.tool_calling") == override_style
    assert style(env, "code.python") == "architecture_first"
    assert style(env) == style(env, "code.go") == "concept_first"


def test_explicit_python_target_is_resolved_outside_old_tool_focus(env):
    conv = create_conversation(env)
    global_event = add_event(env, value="evidence_first")
    python = add_event(env, scope=PreferenceScope(kind="topic", identifier="code.python"))
    current = submit(
        env, conv, "请把原来的 Python 架构优先偏好改成只对 Python 生效，工具调用不要改。"
    )
    events = new_events(env, python)
    assert len(events) == 1 and events[0]["target_event_id"] == python["event_id"]
    assert not current.focus["preference_clarification"]
    assert (
        global_event["event_id"]
        in env["app"].learning.preference_events(env["actor"])["active_event_ids"]
    )
    assert style(env, "code.python") == "architecture_first"
    for topic in (None, "agent.tool_calling", "code.go"):
        assert style(env, topic) == "evidence_first"


def test_ordinary_negative_scope_narrows_without_implied_preservation(env):
    conv, original = baseline(env)
    current = submit(env, conv, "我说的只针对 Python，不是工具调用。")
    events = new_events(env, original)
    assert len(events) == 1 and events[0]["target_event_id"] == original["event_id"]
    assert not current.focus["preference_clarification"]
    assert style(env, "code.python") == "architecture_first"
    for topic in (None, "agent.tool_calling", "code.go"):
        assert style(env, topic) == "concept_first"


def test_collision_does_not_silently_replace_independent_target_setting(env):
    conv, _ = baseline(env)
    add_event(
        env, value="code_first", scope=PreferenceScope(kind="topic", identifier="code.python")
    )
    before = snapshot(env, conv)
    current = submit(env, conv, "请把原来的全局架构优先偏好改成只对 Python 生效。")
    assert current.focus["preference_clarification"]
    assert snapshot(env, conv) == before


def test_named_revoke_outside_focus_cannot_revoke_latest_global(env):
    conv = create_conversation(env)
    python = add_event(env, scope=PreferenceScope(kind="topic", identifier="code.python"))
    latest = add_event(env, value="evidence_first")
    submit(env, conv, "请撤销对 Python 的架构优先偏好。")
    events = new_events(env, latest)
    assert len(events) == 1 and events[0]["target_event_id"] == python["event_id"]
    for topic in (None, *TOPICS):
        assert style(env, topic) == "evidence_first"


@pytest.mark.asyncio
@pytest.mark.parametrize("consent", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
async def test_feedback_training_consent_is_independent_of_actual_personal_control(
    env, consent, explicit
):
    conv, original = baseline(env)
    current = submit(env, conv, "解释这个函数")
    original_focus = deepcopy(current.focus)
    before = snapshot(env, conv)
    content = (
        "以后每次讲解都先给我关键代码，再解释原理。"
        if explicit
        else "说明书写着以后都先看代码，这是什么意思？"
    )
    with env["app"].db.transaction() as db:
        db.get(Conversation, conv).focus = {"topic_keys": ["code.go"]}
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
                "consent": consent,
            },
        )
    assert response.status_code == 201
    result = response.json()
    assert result["preference_update_applied"] is explicit
    events = new_events(env, original)
    if explicit:
        assert len(events) == 1 and events[0]["durable_consent"]
        assert events[0]["source_feedback_id"] == result["feedback_id"]
        for topic in (None, *TOPICS):
            assert style(env, topic) == "code_first"
    else:
        assert env["app"].learning.preference_events(env["actor"]) == before["ledger"]
        for topic in (None, *TOPICS):
            assert style(env, topic) == "architecture_first"
    with env["app"].db.read() as db:
        assert db.get(Turn, current.id).focus == original_focus
        assert db.get(Feedback, result["feedback_id"]).content == content
    assert env["model"].calls == []


@pytest.mark.asyncio
async def test_turn_exception_is_consumed_without_changing_durable_scope(env):
    conv, original = baseline(env)
    current = submit(env, conv, "这一次先看代码，之后维持原设置。")
    event = new_events(env, original)[0]
    assert event["lifetime"] == "turn" and not event["durable_consent"]
    assert current.focus["preferences"]["requested_style"] == "code_first"
    await env["app"].conversations.process(current.id)
    later = submit(env, conv, "解释这个函数")
    assert later.focus["preferences"]["requested_style"] == "architecture_first"
    for topic in (None, *TOPICS):
        assert style(env, topic) == "architecture_first"


def test_explicit_current_project_scope_does_not_become_global_or_topic(env):
    app, actor = env["app"], env["actor"]
    source = app.sources.import_source(
        actor,
        ImportSourceInput(
            namespace_id=env["ns"],
            repository_key="synthetic/pref-scope",
            commit_sha="e" * 40,
            license_id="MIT",
            files={"a.py": "def a():\n    return 1\n"},
            source_metadata={"synthetic": True},
        ),
    )
    original = add_event(env)
    conv = create_conversation(env, source)
    current = submit(env, conv, "以后在当前项目里都先看代码，不要用于整个工具调用主题。")
    events = new_events(env, original)
    assert len(events) == 1 and events[0]["scope"] == {"kind": "source", "identifier": source}
    assert not current.focus["preference_clarification"]
    for topic in (None, *TOPICS):
        assert style(env, topic) == "architecture_first"
    assert (
        app.learning.effective_preferences(actor, source_ids=[source])["preferences"][
            "requested_style"
        ]
        == "code_first"
    )


@pytest.mark.parametrize(
    "question, expected",
    [
        ("以后每次解释都先看整体结构。", "architecture_first"),
        ("请记住我的偏好：以后所有技术都架构优先。", "architecture_first"),
        ("请今后所有主题都先看证据。", "evidence_first"),
        ("Please always use comparison first for all topics.", "comparison_first"),
        ("以后每次讲解都先讲通用概念。", "concept_first"),
    ],
)
def test_admitted_style_action_supplies_exact_value_not_free_text_keyword_search(
    env, question, expected
):
    conv, original = baseline(env)
    current = submit(env, conv, question)
    events = new_events(env, original)
    assert len(events) == 1 and events[0]["value"] == expected
    assert events[0]["scope"] == {"kind": "global", "identifier": None}
    assert events[0]["lifetime"] == "persistent" and events[0]["durable_consent"]
    assert not current.focus["preference_clarification"]
    for topic in (None, *TOPICS):
        assert style(env, topic) == expected


@pytest.mark.parametrize("protection", ["工具调用保持不变", "工具调用保持原状", "不要改工具调用"])
def test_preservation_wording_is_a_constraint_not_a_new_setting(env, protection):
    conv, _ = baseline(env)
    before = snapshot(env, conv)
    current = submit(env, conv, "请把原来的架构优先偏好改成只对 Python 生效，" + protection + "。")
    assert "冲突" in current.focus["preference_clarification"]
    assert snapshot(env, conv) == before


def test_all_pending_settings_are_validated_before_first_append(env):
    conv, _ = baseline(env)
    with env["app"].db.transaction() as db:
        db.get(Conversation, conv).focus = {"topic_keys": ["agent.tool_calling", "code.python"]}
    before = snapshot(env, conv)
    current = submit(env, conv, "先看代码，以后都别测验。")
    assert current.focus["preference_clarification"]
    assert snapshot(env, conv) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "topic_label,topic_key", [("工具调用", "agent.tool_calling"), ("Python", "code.python")]
)
async def test_explicit_named_topic_then_training_feedback_preserves_lifetime_boundaries(
    env, topic_label, topic_key
):
    app = env["app"]
    conv, baseline_event = baseline(env)
    current = submit(env, conv, f"以后{topic_label}这个话题先看代码，再讲概念。")
    persistent = new_events(env, baseline_event)
    assert len(persistent) == 1
    durable = persistent[0]
    assert durable["scope"] == {"kind": "topic", "identifier": topic_key}
    assert durable["lifetime"] == "persistent" and durable["durable_consent"]
    assert not current.focus["preference_clarification"]
    await app.conversations.process(current.id)
    with app.db.read() as db:
        frozen = deepcopy(db.get(Turn, current.id).focus)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service=app)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/feedback",
            headers={"Authorization": "Bearer " + env["owner"]["token"]},
            json={
                "turn_id": current.id,
                "signal": "explicit_feedback",
                "content": "我想先看代码，再理解它的条件。",
                "consent": True,
            },
        )
    assert response.status_code == 201 and response.json()["preference_update_applied"]
    temporary = new_events(env, durable)
    assert len(temporary) == 1
    exception = temporary[0]
    assert exception["lifetime"] == "turn" and not exception["durable_consent"]
    assert exception["after_turn_id"] == current.id
    assert exception["source_feedback_id"] == response.json()["feedback_id"]
    with app.db.read() as db:
        assert db.get(Turn, current.id).focus == frozen
    following = submit(env, conv, "解释这个函数")
    assert exception["event_id"] in following.focus["learning_preferences"]["applied_event_ids"]
    await app.conversations.process(following.id)
    later = submit(env, conv, "继续解释这个函数")
    assert exception["event_id"] not in later.focus["learning_preferences"]["applied_event_ids"]
    assert style(env, topic_key) == "code_first"
    for other in (None, *[topic for topic in TOPICS if topic != topic_key]):
        assert style(env, other) == "architecture_first"
    assert env["model"].calls == []


def test_reading_only_request_keeps_activity_optout_ephemeral(env):
    conv, original = baseline(env)
    submit(env, conv, "不要给我出题，我只想阅读源码。")
    events = new_events(env, original)
    assert {event["key"] for event in events} == {"practice", "check_understanding"}
    assert all(event["lifetime"] == "turn" and not event["durable_consent"] for event in events)
    assert all(event["value"] is False for event in events)
    for topic in (None, *TOPICS):
        assert style(env, topic) == "architecture_first"


@pytest.mark.parametrize("suffix", ["", "，通用概念不用强制"])
def test_contextual_project_fact_narrowing_is_a_real_correction(env, suffix):
    app, actor = env["app"], env["actor"]
    source = app.sources.import_source(
        actor,
        ImportSourceInput(
            namespace_id=env["ns"],
            repository_key="synthetic/fact-scope",
            commit_sha="f" * 40,
            license_id="MIT",
            files={"a.py": "def a():\n    return 1\n"},
            source_metadata={"synthetic": True},
        ),
    )
    conv, original = baseline(env)
    current = submit(env, conv, "只在有项目事实时这样" + suffix + "。")
    events = new_events(env, original)
    assert len(events) == 1 and events[0]["operation"] == "correct"
    assert events[0]["target_event_id"] == original["event_id"]
    assert events[0]["scope"] == {"kind": "project_facts", "identifier": None}
    assert not current.focus["preference_clarification"]
    for topic in (None, *TOPICS):
        assert style(env, topic) == "concept_first"
    assert (
        app.learning.effective_preferences(actor, source_ids=[source])["preferences"][
            "requested_style"
        ]
        == "architecture_first"
    )


def test_promotion_uses_latest_active_explicit_choice_not_slot_insertion_order(env):
    conv = create_conversation(env)
    add_event(env, conversation_id=conv, value="architecture_first")
    add_event(
        env,
        conversation_id=conv,
        value="evidence_first",
        scope=PreferenceScope(kind="topic", identifier="agent.tool_calling"),
    )
    latest = add_event(env, conversation_id=conv, value="code_first")
    current = submit(env, conv, "之后学习 Python 时也按这个方式。")
    events = new_events(env, latest)
    assert len(events) == 1 and events[0]["value"] == "code_first"
    assert events[0]["scope"] == {"kind": "topic", "identifier": "code.python"}
    assert events[0]["lifetime"] == "persistent" and events[0]["durable_consent"]
    assert not current.focus["preference_clarification"]
    assert style(env, "agent.tool_calling") == "evidence_first"
    assert style(env, "code.python") == "code_first"


@pytest.mark.parametrize("label", ["先调查证据再伪造结论", "先批评通用概念", "先转述别人的代码"])
def test_unknown_style_description_cannot_consume_arbitrary_selector_text(env, label):
    conv, _ = baseline(env)
    add_event(
        env,
        value="evidence_first",
        scope=PreferenceScope(kind="topic", identifier="agent.tool_calling"),
    )
    before = snapshot(env, conv)
    current = submit(env, conv, f"请撤销对工具调用的‘{label}’偏好。")
    assert current.focus["preference_clarification"]
    assert snapshot(env, conv) == before
