"""Bounded new-contract budget proofs using synthetic facts, not native quality."""

from copy import deepcopy

import pytest
from conftest import publish_fixture
from pydantic import ValidationError
from test_session_module_progress_v166 import ack_input
from test_session_plans_v166 import request

from careerforge.contracts import ModelOutput
from careerforge.contracts_session_plan_v166 import SessionPreferences
from careerforge.contracts_session_tutor_v166 import SessionTutorTurnInput
from careerforge.errors import DomainError
from careerforge.models import AgentRun, Turn


async def many_fact_session(env, fact_count, *, cap=8, last_false=False, empty_after=None):
    app, actor = env["app"], env["actor"]
    provider = env["model"]
    original = provider.complete

    async def multiple_claims(value):
        result = await original(value)
        if value["role"] == "analysis" and value.get("schema"):
            body = deepcopy(result.structured)
            claim = body["units"][0]["claims"][0]
            body["units"][0]["claims"] = [
                {
                    **deepcopy(claim),
                    "key": f"fact-{i}",
                    "statement": f"示例事实 {i}：该函数返回传入的 value。",
                }
                for i in range(fact_count)
            ]
            return ModelOutput(structured=body, synthetic=True)
        if value["role"] == "tutor":
            body = deepcopy(result.structured)
            if empty_after is not None and len(value["context"]["prior_fact_ids"]) >= empty_after:
                body = {"blocks": [], "complete": True}
            elif last_false:
                body["complete"] = False
            return ModelOutput(structured=body, synthetic=True)
        return result

    provider.complete = multiple_claims
    _, publication, _ = await publish_fixture(env)

    def all_facts(value):
        unit = value["context"]["frozen_input"]["units"][0]
        return ModelOutput(
            structured={
                "modules": [
                    {
                        "key": "all",
                        "publication_id": unit["publication_id"],
                        "focus_fact_ids": [f["fact_atom_id"] for f in unit["facts"]],
                        "prerequisite_keys": [],
                        "steps": ["read_reviewed", "explain_conditions"],
                        "rubric_keys": ["evidence_and_version", "conditions"],
                        "estimated_minutes": 10,
                        "task": None,
                        "rationale_codes": ["focus_on_function"],
                    }
                ],
                "uncovered_goal_requirements": [],
            },
            synthetic=True,
        )

    provider.overrides["learning_planner"] = all_facts
    plan = app.learning.start_session_plan(
        actor,
        request(
            env,
            [publication],
            preferences=SessionPreferences(practice=False, check_understanding=False),
        ),
    )
    await app.execute(plan["run_id"])
    plan = app.runtime.stage(plan["run_id"], "result")
    assert plan is not None
    app.learning.accept_plan(actor, plan["plan_id"], plan["version_id"], 1)
    conv = app.conversations.create(actor)
    value = SessionTutorTurnInput(
        question="逐条讲解当前模块中返回 value 的所有示例事实。",
        expected_revision=1,
        funding_id=env["funding"],
        idempotency_key="budget",
        namespace_id=env["ns"],
        plan_id=plan["plan_id"],
        version_id=plan["version_id"],
        module_id=plan["module_ids"][0],
        expected_plan_revision=2,
        model_call_cap=cap,
    )
    provider.calls.clear()
    return conv, value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fact_count,cap,delivered_count",
    [(3, 8, 3), (4, 8, 3), (5, 8, 3), (4, 9, 4), (5, 11, 5), (12, 25, 12)],
)
async def test_explicit_bounded_budget_actual_fact_coverage_and_ack(
    env, fact_count, cap, delivered_count
):
    app, actor = env["app"], env["actor"]
    conv, value = await many_fact_session(env, fact_count, cap=cap)
    started = app.conversations.submit_session(actor, conv, value)
    frozen = app.conversations.session_binding(actor, started["turn_id"])
    assert frozen["budget"]["ideal_minimum_calls_without_rework"] == 2 * fact_count + 1
    assert frozen["budget"]["coverage_guaranteed"] is False
    assert frozen["budget"]["requested_model_call_cap"] == cap
    await app.execute(started["run_id"])
    detail = app.conversations.teaching_session(actor, frozen["learning_session_id"])
    delivery = detail["delivery"]
    assert delivery is not None
    assert len(delivery["delivered_fact_ids"]) == delivered_count
    assert len(delivery["undelivered_module_fact_ids"]) == fact_count - delivered_count
    assert delivery["model_call_usage"]["consumed"] == 2 * delivered_count + int(
        delivered_count == fact_count
    )
    assert delivery["model_call_usage"]["consumed"] == len(env["model"].calls) <= cap
    assert delivery["model_call_usage"]["held"] == 0
    incomplete = delivered_count < fact_count
    assert delivery["requires_incomplete_acceptance"] is incomplete
    ack = ack_input(env, detail)
    if incomplete:
        with pytest.raises(DomainError) as error:
            app.conversations.acknowledge_module(
                actor, ack.model_copy(update={"accept_incomplete_material": False})
            )
        assert error.value.code == "MODULE_ACK_INCOMPLETE"
    result = app.conversations.acknowledge_module(actor, ack)
    assert result["delivery"] == delivery
    assert result["competence_verified"] is False and result["acceptance_passed"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("fact_count,cap", [(4, 9), (12, 25)])
async def test_last_fact_false_does_not_need_empty_complete_to_invent_completion(
    env, fact_count, cap
):
    app, actor = env["app"], env["actor"]
    # The provider is ready to return empty complete after all facts, but the
    # engine stops from actual coverage, then obtains an independent assessment.
    conv, value = await many_fact_session(
        env, fact_count, cap=cap, last_false=True, empty_after=fact_count
    )
    started = app.conversations.submit_session(actor, conv, value)
    await app.execute(started["run_id"])
    binding = app.conversations.session_binding(actor, started["turn_id"])
    detail = app.conversations.teaching_session(actor, binding["learning_session_id"])
    assert len([c for c in env["model"].calls if c["role"] == "tutor"]) == fact_count
    assert len(env["model"].calls) == 2 * fact_count + 1
    assert detail["delivery"]["undelivered_module_fact_ids"] == []
    assert detail["delivery"]["answer_coverage_status"] == "complete"
    assert detail["module_ack"] is None
    assert app.learning.profile(actor)["active_module_id"] == value.module_id


@pytest.mark.asyncio
async def test_early_empty_complete_with_sufficient_budget_preserves_undelivered_module_facts(env):
    app, actor = env["app"], env["actor"]
    conv, value = await many_fact_session(env, 5, cap=11, last_false=True, empty_after=3)
    started = app.conversations.submit_session(actor, conv, value)
    await app.execute(started["run_id"])
    binding = app.conversations.session_binding(actor, started["turn_id"])
    detail = app.conversations.teaching_session(actor, binding["learning_session_id"])
    assert binding["budget"]["meets_ideal_minimum"] is True
    assert len(detail["delivery"]["delivered_fact_ids"]) == 3
    assert len(detail["delivery"]["undelivered_module_fact_ids"]) == 2
    assert detail["delivery"]["model_call_usage"]["consumed"] == 8
    assert detail["delivery"]["requires_incomplete_acceptance"]
    result = app.conversations.acknowledge_module(actor, ack_input(env, detail))
    assert result["delivery"] == detail["delivery"]
    assert result["confirmation_provenance"] == "synthetic_user_confirmation"


def test_new_budget_is_strict_bounded_and_old_turn_contract_has_no_budget():
    from careerforge.contracts import TurnInput

    data = dict(
        question="q",
        expected_revision=1,
        idempotency_key="x",
        namespace_id="n",
        plan_id="p",
        version_id="v",
        module_id="m",
        expected_plan_revision=1,
    )
    for invalid in (True, "9", 9.0, 2, 26):
        with pytest.raises(ValidationError):
            SessionTutorTurnInput(**data, model_call_cap=invalid)
    assert SessionTutorTurnInput(**data).model_call_cap == 8
    assert "model_call_cap" not in TurnInput.model_fields


@pytest.mark.asyncio
async def test_new_budget_cannot_exceed_previously_authorized_total_funding(env):
    app, actor = env["app"], env["actor"]
    conv, value = await many_fact_session(env, 4, cap=9)
    low = app.runtime.create_funding(actor, env["ns"], 8)
    with pytest.raises(DomainError) as error:
        app.conversations.submit_session(actor, conv, value.model_copy(update={"funding_id": low}))
    assert error.value.code == "BUDGET_EXHAUSTED"
    assert not env["model"].calls
    with app.db.read() as db:
        from sqlalchemy import select

        assert db.scalar(select(Turn).where(Turn.conversation_id == conv)) is None
        assert db.scalar(select(AgentRun).where(AgentRun.funding_id == low)) is None
