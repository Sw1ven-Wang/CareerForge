"""Explicit focus changes are distinct from retrieval fallback scope expansion."""

import pytest
from conftest import publish_fixture

from careerforge.services.orchestration import _explicit_scope_switch, rule_intent
from careerforge.services.policies import ensure_policies


@pytest.mark.parametrize(
    "question",
    [
        "Verify this & edit the code.",
        "Verify this + write the replacement.",
        "Verify this / run the checks.",
        "Compare implementations followed by deleting the old one.",
        "解释调用路径 & 修改实现。",
    ],
)
def test_compound_imperative_separators_abstain(question):
    assert rule_intent(question, {}) is None


@pytest.mark.parametrize(
    "question",
    [
        "Verify the x/y ratio.",
        "Explain a + b arithmetic.",
        "Explain a/b/c.py.",
    ],
)
def test_symbol_and_path_separators_are_not_actions(question):
    assert rule_intent(question, {}) is not None


@pytest.mark.parametrize(
    "question",
    [
        "Do not switch focus to fixtures/second. Explain this code.",
        'Explain "switch focus to fixtures/second".',
        "Switch to fixtures/second and fixtures/first.",
        "Switch to first.",
    ],
)
def test_scope_change_rejects_negated_quoted_multiple_or_short_targets(question):
    assert _explicit_scope_switch(question) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "Switch focus to fixtures/second and explain the returned value.",
        "请切换到 fixtures/second 解释返回值。",
    ],
)
async def test_explicit_full_repository_switch_changes_only_source_focus(env, question):
    app, actor = env["app"], env["actor"]
    first, _, _ = await publish_fixture(env, "first", "fixtures/first", "a" * 40)
    second, _, _ = await publish_fixture(env, "second", "fixtures/second", "b" * 40)
    with app.db.transaction() as db:
        policies = ensure_policies(db, actor, env["ns"])
    intent, _ = await app.coordinator.decide(
        actor,
        question,
        {"project_source_ids": [first], "topic_keys": ["agent.tool_calling"]},
        1,
        policies,
        None,
    )
    assert intent.project_source_ids == [second]
    assert intent.retrieval_topic_keys == ["agent.tool_calling"]
    assert "explicit_source_focus_change" in intent.rationale_codes


@pytest.mark.asyncio
async def test_unknown_explicit_scope_switch_cannot_widen_or_erase_focus(env):
    app, actor = env["app"], env["actor"]
    first, _, _ = await publish_fixture(env)
    with app.db.transaction() as db:
        policies = ensure_policies(db, actor, env["ns"])
    intent, decision = await app.coordinator.decide(
        actor,
        "Switch to outside/unknown and explain the state.",
        {"project_source_ids": [first]},
        1,
        policies,
        None,
    )
    assert intent.project_source_ids == [first]
    assert decision.action == "ask_clarification"


@pytest.mark.asyncio
@pytest.mark.parametrize("question,clarify", [
    ("https://github.com/earendil-works/pi   我要学这个的架构", False),
    ("https://github.com/earendil-works/pi 讲解另一个版本的架构", True),
])
async def test_repository_url_does_not_expand_selected_snapshot(env, question, clarify):
    app, actor = env["app"], env["actor"]
    first, _, _ = await publish_fixture(env, "first", "earendil-works/pi", "a" * 40)
    await publish_fixture(env, "second", "earendil-works/pi", "b" * 40)
    with app.db.transaction() as db:
        policies = ensure_policies(db, actor, env["ns"])
    from careerforge.services.learning_intent import SCOPED_QUESTION_ROUTING_CONTRACT
    intent, decision = await app.coordinator.decide(actor, question, {"project_source_ids": [first]}, 1, policies, None, routing_contract_version=SCOPED_QUESTION_ROUTING_CONTRACT)
    assert intent.project_source_ids == [first]
    assert (decision.action == "ask_clarification") == clarify
