"""Preserve conservative verification routing, rather than override a model choice."""

from copy import deepcopy

import pytest
from conftest import publish_fixture
from sqlalchemy import select
from test_retrieval_intent_mechanisms_v161 import active_run

from careerforge.contracts import ModelOutput, TaskType, TurnInput
from careerforge.errors import DomainError
from careerforge.models import AgentRun, FundingAgreement, ModelReceipt, Turn
from careerforge.services.learning_intent import (
    LEARNING_ROUTING_CONTRACT,
    SCOPED_QUESTION_ROUTING_CONTRACT,
)
from careerforge.services.orchestration import (
    ROUTING_CONTRACT_VERSION,
    _editing_action_language,
    rule_intent,
)
from careerforge.util import canonical, fingerprint

POLICIES = {
    "routing_policy": {
        "policy_id": "verification-contract-test",
        "content": {"keyword_rules": [], "ambiguity_threshold": 0.72},
    }
}


@pytest.mark.parametrize(
    "question",
    [
        "Do not edit artifacts. Verify the selected range claim.",
        "Without practicing, verify the documented transition.",
        "Don't verify the range claim; locate the function.",
        'The sample says "verify the claim". Explain the identifier.',
        "Verify the condition, then modify the configuration.",
        "不要改文件。核实这条边界声明。",
    ],
)
def test_negated_quoted_and_multiple_actions_still_require_resolution(question):
    assert rule_intent(question, {"project_source_ids": ["source"]}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resolution,confidence,proposed,expected",
    [
        ("resolved", 0.95, "retrieve_and_tutor", "invoke_tutor"),
        ("missing", 0.95, "retrieve_and_tutor", "ask_clarification"),
        ("ambiguous", 0.95, "retrieve_and_tutor", "ask_clarification"),
        ("resolved", 0.60, "retrieve_and_tutor", "ask_clarification"),
        ("resolved", 0.95, "ask_clarification", "ask_clarification"),
    ],
)
async def test_verification_respects_each_independent_model_gate(
    env, resolution, confidence, proposed, expected
):
    app, actor = env["app"], env["actor"]
    source, _, _ = await publish_fixture(env, repository="scope/codec")
    focus = {"project_source_ids": [source], "topic_keys": ["agent.tool_calling"]}
    env["model"].overrides["coordinator"] = lambda _: ModelOutput(
        structured={
            "interpreted_intent": "verify_claim",
            "project_source_ids": [source],
            "reference_resolution": resolution,
            "confidence": confidence,
            "proposed_next_action": proposed,
        },
        synthetic=True,
    )
    intent, decision = await app.coordinator.decide(
        actor,
        "Do not edit artifacts. Verify the selected range claim in scope/codec.",
        focus,
        1,
        POLICIES,
        active_run(env, "verification-contract", cap=1),
    )
    assert intent.interpreted_intent == TaskType.VERIFY
    assert decision.action == expected
    assert intent.project_source_ids == decision.source_version_refs == [source]
    assert intent.retrieval_topic_keys == ["agent.tool_calling"]
    calls = [call for call in env["model"].calls if call["role"] == "coordinator"]
    assert len(calls) == 1
    assert {s["id"] for s in calls[0]["context"]["authorized_sources"]} == {source}


@pytest.mark.asyncio
async def test_verification_model_cannot_expand_explicit_source_or_skip_missing_budget(env):
    app, actor = env["app"], env["actor"]
    first, _, _ = await publish_fixture(env, "first", "scope/codec")
    second, _, _ = await publish_fixture(env, "second", "scope/other")
    focus = {"project_source_ids": [first]}
    env["model"].overrides["coordinator"] = lambda _: ModelOutput(
        structured={
            "interpreted_intent": "verify_claim",
            "project_source_ids": [second],
            "reference_resolution": "resolved",
            "confidence": 0.95,
            "proposed_next_action": "retrieve_and_tutor",
        },
        synthetic=True,
    )
    question = "Do not edit artifacts. Verify the selected range claim."
    intent, decision = await app.coordinator.decide(
        actor, question, focus, 1, POLICIES, active_run(env, "source-gate", cap=1)
    )
    assert decision.action == "ask_clarification"
    assert intent.project_source_ids == [first]
    before = len(env["model"].calls)
    _, decision = await app.coordinator.decide(actor, question, focus, 1, POLICIES, None)
    assert decision.action == "ask_clarification"
    assert len(env["model"].calls) == before


EDIT_FORMS = [
    "edit",
    "editing",
    "edited",
    "edits",
    "change",
    "changing",
    "changed",
    "changes",
    "modify",
    "modifying",
    "modified",
    "modifies",
    "alter",
    "altering",
    "altered",
    "alters",
    "revise",
    "revising",
    "revised",
    "revises",
    "adjust",
    "adjusting",
    "adjusted",
    "adjusts",
    "update",
    "updating",
    "updated",
    "updates",
    "amend",
    "amending",
    "amended",
    "amends",
    "rewrite",
    "rewriting",
    "rewrote",
    "rewritten",
    "rewrites",
    "patch",
    "patching",
    "patched",
    "patches",
    "refactor",
    "refactoring",
    "refactored",
    "refactors",
]


@pytest.mark.parametrize("verb", EDIT_FORMS)
def test_editing_family_abstains_only_when_used_as_a_separate_action(verb):
    question = f"Verify the reported condition; then {verb} the configuration."
    assert rule_intent(question, {}) is None
    assert rule_intent(f"Do not {verb} the configuration. Verify the condition.", {}) is None
    quoted = f'Explain the word "{verb}".'
    assert _editing_action_language(quoted) == quoted
    assert rule_intent(quoted, {}).interpreted_intent == TaskType.IMPLEMENTATION
    identifier = f"Verify the {verb}_setting identifier."
    assert _editing_action_language(identifier) == identifier
    assert rule_intent(identifier, {}).interpreted_intent == TaskType.VERIFY


@pytest.mark.parametrize(
    "word", ["modification", "modification_state", "changeset", "updated_at", "unmodified"]
)
def test_edit_action_normalization_does_not_consume_nouns_or_identifiers(word):
    question = f"Explain the {word} field."
    assert _editing_action_language(question) == question
    assert rule_intent(question, {}).interpreted_intent == TaskType.IMPLEMENTATION


def test_edit_family_version_branch_keeps_unmarked_legacy_behavior():
    question = "Verify the condition, then modify the configuration."
    assert rule_intent(question, {}) is None
    legacy = rule_intent(question, {}, routing_contract_version=None)
    assert legacy.interpreted_intent == TaskType.VERIFY
    assert legacy.proposed_next_action == "retrieve_and_tutor"
    with pytest.raises(DomainError) as error:
        rule_intent(question, {}, routing_contract_version="unknown")
    assert error.value.code == "ROUTING_CONTRACT_VERSION"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "routing_contract",
    [SCOPED_QUESTION_ROUTING_CONTRACT, LEARNING_ROUTING_CONTRACT, ROUTING_CONTRACT_VERSION, None],
    ids=["current", "queued-1.6.6", "queued-1.6.3", "queued-unmarked"],
)
async def test_new_turns_freeze_contract_and_old_queued_turns_keep_their_path(
    env, routing_contract
):
    app, actor = env["app"], env["actor"]
    question = "Verify the condition, then modify the configuration."
    conversation = app.conversations.create(actor)
    request = app.conversations.submit(
        actor,
        conversation,
        TurnInput(
            question=question,
            expected_revision=1,
            funding_id=env["funding"],
            idempotency_key="editing-contract",
        ),
    )
    with app.db.transaction() as db:
        turn = db.get(Turn, request["turn_id"])
        assert turn.focus["routing_contract_version"] == SCOPED_QUESTION_ROUTING_CONTRACT
        assert LEARNING_ROUTING_CONTRACT == "intent-actions-1.6.6"
        assert ROUTING_CONTRACT_VERSION == "intent-actions-1.6.3"
        if routing_contract != SCOPED_QUESTION_ROUTING_CONTRACT:
            # Synthetic queued input only. Pin the historical routing marker and
            # omit newer admission fields, rather than testing an old expectation
            # against a newly admitted 1.6.6 turn.
            focus = deepcopy(turn.focus)
            for key in (
                "retrieval_context_contract",
                "learning_preferences",
                "learning_intent",
                "preference_event_ids",
                "preference_clarification",
                "preference_feedback_only",
            ):
                focus.pop(key, None)
            focus["preferences"] = app.conversations.detail(actor, conversation)["preferences"]
            if routing_contract is None:
                focus.pop("routing_contract_version")
            else:
                focus["routing_contract_version"] = routing_contract
            turn.focus = focus
        frozen_focus = canonical(turn.focus)
        run = db.get(AgentRun, request["run_id"])
        frozen_run = (canonical(run.input_manifest), run.input_hash)
    env["model"].overrides["coordinator"] = lambda _: ModelOutput(
        structured={
            "interpreted_intent": "verify_claim",
            "reference_resolution": "ambiguous",
            "confidence": 0.5,
            "proposed_next_action": "ask_clarification",
        },
        synthetic=True,
    )
    epoch = app.runtime.claim(request["run_id"], "editing-worker")
    await app.conversations.process(
        request["turn_id"], (request["run_id"], epoch, "editing-worker")
    )
    result = app.runtime.stage(request["run_id"], "orchestration")
    unmarked = routing_contract is None
    assert result["decision"]["action"] == ("invoke_tutor" if unmarked else "ask_clarification")
    calls = [call for call in env["model"].calls if call["role"] == "coordinator"]
    assert len(calls) == (0 if unmarked else 1)
    if calls:
        assert calls[0]["context"]["question"] == question
        assert "routing_contract_version" not in calls[0]["context"]["focus"]
    with app.db.read() as db:
        assert db.get(AgentRun, request["run_id"]).calls_consumed == (0 if unmarked else 1)
        receipts = db.scalars(
            select(ModelReceipt).where(ModelReceipt.run_id == request["run_id"])
        ).all()
        assert len(receipts) == len(calls)
        before = [(r.id, r.request_hash, canonical(r.response)) for r in receipts]
        if calls:
            assert receipts[0].request_hash == fingerprint(calls[0])
        funding_consumed = db.get(FundingAgreement, env["funding"]).calls_consumed
    await app.conversations.process(
        request["turn_id"], (request["run_id"], epoch, "editing-worker")
    )
    assert [call for call in env["model"].calls if call["role"] == "coordinator"] == calls
    with app.db.read() as db:
        assert canonical(db.get(Turn, request["turn_id"]).focus) == frozen_focus
        run = db.get(AgentRun, request["run_id"])
        assert (canonical(run.input_manifest), run.input_hash) == frozen_run
        assert run.calls_consumed == (0 if unmarked else 1)
        assert db.get(FundingAgreement, env["funding"]).calls_consumed == funding_consumed
        receipts = db.scalars(
            select(ModelReceipt).where(ModelReceipt.run_id == request["run_id"])
        ).all()
        assert [(r.id, r.request_hash, canonical(r.response)) for r in receipts] == before


@pytest.mark.asyncio
async def test_unknown_routing_version_rejects_before_model_spend(env):
    with pytest.raises(DomainError) as error:
        await env["app"].coordinator.decide(
            env["actor"],
            "Verify the condition, then modify settings.",
            {},
            1,
            POLICIES,
            active_run(env, "unknown-edit-contract", cap=1),
            routing_contract_version="unknown",
        )
    assert error.value.code == "ROUTING_CONTRACT_VERSION"
    assert env["model"].calls == []
