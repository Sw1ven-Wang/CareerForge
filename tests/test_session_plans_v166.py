"""Single-session budget/confirmation proofs using a synthetic planner, no live model."""

from copy import deepcopy

import httpx
import pytest
from conftest import publish_fixture
from sqlalchemy import select

from careerforge.api import create_app
from careerforge.contracts import ModelOutput
from careerforge.contracts_learning_ext import ActivityInput, PersonalizedPlanInput
from careerforge.contracts_session_plan_v166 import SessionPlanInput, SessionPreferences
from careerforge.errors import DomainError
from careerforge.models import AgentRun, LearningPlan, RunEvent
from careerforge.models_learning_ext import PlanProposalManifest
from careerforge.services.dependencies import invalidate
from careerforge.services.security import create_principal
from careerforge.services.session_plans import SESSION_PLAN_CONTRACT
from careerforge.util import fingerprint


def request(env, publications, *, total=12, key="session", preferences=None):
    return SessionPlanInput(
        namespace_id=env["ns"],
        funding_id=env["funding"] if publications else None,
        publication_ids=publications,
        learning_intent="function",
        title="阅读固定代码并核对返回值",
        total_minutes=total,
        expected_profile_revision=env["app"].learning.profile(env["actor"])["revision"],
        preferences=preferences or SessionPreferences(),
        idempotency_key=key,
    )


def planner(env, minutes=None, *, max_modules=None):
    def complete(model_request):
        frozen = model_request["context"]["frozen_input"]
        units = frozen["units"]
        if max_modules:
            units = units[:max_modules]
        modules = []
        for index, unit in enumerate(units):
            modules.append(
                {
                    "key": f"m{index}",
                    "publication_id": unit["publication_id"],
                    "focus_fact_ids": [unit["facts"][0]["fact_atom_id"]],
                    "prerequisite_keys": [f"m{index - 1}"] if index else [],
                    "steps": ["read_reviewed", "explain_conditions"],
                    "rubric_keys": ["evidence_and_version", "conditions"],
                    "estimated_minutes": (minutes or [5] * len(units))[index],
                    "task": None,
                    "rationale_codes": ["focus_on_function"],
                }
            )
        return ModelOutput(
            structured={"modules": modules, "uncovered_goal_requirements": []}, synthetic=True
        )

    env["model"].overrides["learning_planner"] = complete


@pytest.mark.asyncio
async def test_total_budget_is_enforced_before_persisting_any_plan(env):
    app, actor = env["app"], env["actor"]
    _, first, _ = await publish_fixture(env, "first")
    _, second, _ = await publish_fixture(env, "second", "fixtures/second", "b" * 40)
    planner(env, [8, 8])
    started = app.learning.start_session_plan(actor, request(env, [first, second], total=12))
    await app.execute(started["run_id"])
    with app.db.read() as db:
        run = db.get(AgentRun, started["run_id"])
        assert run.status == "budget_stopped"
        finished = db.scalar(
            select(RunEvent).where(RunEvent.run_id == run.id, RunEvent.event_type == "run.finished")
        )
        assert finished.payload["code"] == "PLAN_SESSION_BUDGET"
        assert list(db.scalars(select(LearningPlan))) == []
    assert app.runtime.stage(started["run_id"], "result") is None
    assert app.learning.profile(actor)["active_plan_id"] is None


@pytest.mark.asyncio
async def test_draft_schedule_requires_acceptance_and_one_session_optout_survives(env):
    app, actor = env["app"], env["actor"]
    _, first, _ = await publish_fixture(env)
    planner(env, [10])
    before = app.learning.profile(actor)
    value = request(
        env,
        [first],
        preferences=SessionPreferences(
            practice=False,
            check_understanding=False,
            diagnostics_enabled=False,
            requested_style="architecture_first",
        ),
    )
    started = app.learning.start_session_plan(actor, value)
    assert app.learning.start_session_plan(actor, value)["replayed"]
    await app.execute(started["run_id"])
    result = app.runtime.stage(started["run_id"], "result")
    assert result["session_contract"] == SESSION_PLAN_CONTRACT
    assert result["planned_minutes"] == 10 <= result["total_minutes"] == 12
    assert result["schedule"][0]["start_offset_minutes"] == 0
    assert result["schedule"][0]["end_offset_minutes"] == 10
    assert not result["accepted"] and app.learning.profile(actor)["active_plan_id"] is None
    with pytest.raises(DomainError):
        app.learning.create_activity(actor, ActivityInput(module_id=result["module_ids"][0]))
    detail = app.learning.detail(actor, result["plan_id"])
    assert detail["versions"][0]["schedule"] == result["schedule"]
    accepted = app.learning.accept_plan(actor, result["plan_id"], result["version_id"], 1)
    assert accepted["schedule_status"] == "accepted"
    assert accepted["active_module_id"] == result["module_ids"][0]
    assert accepted["session_schedule"] == result["schedule"]
    assert app.learning.profile(actor)["preferences"] == before["preferences"]
    with pytest.raises(DomainError) as error:
        app.learning.create_activity(actor, ActivityInput(module_id=result["module_ids"][0]))
    assert error.value.code == "LEARNING_OPT_OUT"
    with pytest.raises(DomainError):
        app.learning.accept_plan(actor, result["plan_id"], result["version_id"], 1)


@pytest.mark.asyncio
async def test_omitted_sources_are_reported_in_bounded_schedule(env):
    app, actor = env["app"], env["actor"]
    _, first, _ = await publish_fixture(env, "first")
    _, second, _ = await publish_fixture(env, "second", "fixtures/second", "c" * 40)
    planner(env, [7], max_modules=1)
    started = app.learning.start_session_plan(actor, request(env, [first, second]))
    await app.execute(started["run_id"])
    result = app.runtime.stage(started["run_id"], "result")
    assert result["unplanned_publication_ids"] == [second]
    assert result["planned_minutes"] == 7


def test_empty_knowledge_draft_is_unbuildable_idempotent_and_never_spends(env):
    app, actor = env["app"], env["actor"]
    value = request(env, [])
    first = app.learning.start_session_plan(actor, value)
    assert first["status"] == "unbuildable" and not first["can_accept"]
    assert first["uncovered_requirements"][0]["reason"] == "no_reviewed_knowledge"
    assert first["model_calls"] == 0 and env["model"].calls == []
    assert app.learning.start_session_plan(actor, value)["replayed"]
    assert app.learning.session_plan_gap(actor, first["draft_id"]) == first
    with pytest.raises(DomainError):
        app.learning.accept_plan(actor, first["draft_id"], first["draft_id"], 1)
    with app.db.read() as db:
        assert list(db.scalars(select(LearningPlan))) == []
        assert list(db.scalars(select(AgentRun))) == []
    with pytest.raises(DomainError):
        app.learning.start_session_plan(actor, value.model_copy(update={"total_minutes": 18}))


@pytest.mark.asyncio
async def test_unpublished_or_revoked_evidence_never_becomes_buildable(env):
    app, actor = env["app"], env["actor"]
    _, publication, _ = await publish_fixture(env)
    before = len(env["model"].calls)
    with pytest.raises(DomainError):
        app.learning.start_session_plan(actor, request(env, ["unpublished-unit-id"]))
    with app.db.transaction() as db:
        invalidate(db, actor, publication, "revoked", "test_revocation")
    with pytest.raises(DomainError):
        app.learning.start_session_plan(actor, request(env, [publication]))
    assert len(env["model"].calls) == before


@pytest.mark.asyncio
async def test_schedule_tamper_cannot_be_accepted_even_with_updated_manifest_hash(env):
    app, actor = env["app"], env["actor"]
    _, publication, _ = await publish_fixture(env)
    planner(env, [10])
    started = app.learning.start_session_plan(actor, request(env, [publication]))
    await app.execute(started["run_id"])
    result = app.runtime.stage(started["run_id"], "result")
    with app.db.transaction() as db:
        row = db.scalar(
            select(PlanProposalManifest).where(PlanProposalManifest.run_id == started["run_id"])
        )
        changed = deepcopy(row.payload)
        changed["session_schedule"][0]["estimated_minutes"] = 1
        row.payload, row.content_hash = changed, fingerprint(changed)
    with pytest.raises(DomainError) as error:
        app.learning.accept_plan(actor, result["plan_id"], result["version_id"], 1)
    assert error.value.code == "PLAN_SESSION_BUDGET"


@pytest.mark.asyncio
async def test_session_endpoint_and_foreign_draft_access(env):
    app = env["app"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service=app)), base_url="http://test"
    ) as client:
        headers = {"Authorization": "Bearer " + env["owner"]["token"]}
        response = await client.post(
            "/api/v1/learning/session-plan-proposals",
            json=request(env, []).model_dump(mode="json"),
            headers=headers,
        )
        assert response.status_code == 202 and response.json()["status"] == "unbuildable"
        draft = response.json()["draft_id"]
        assert (
            await client.get(f"/api/v1/learning/session-plan-gaps/{draft}", headers=headers)
        ).status_code == 200
    with app.db.transaction() as db:
        other, _, _ = create_principal(db, "session-other")
    with pytest.raises(DomainError):
        app.learning.session_plan_gap(other.id, draft)


def test_legacy_plan_input_contract_does_not_gain_session_fields():
    fields = PersonalizedPlanInput.model_json_schema()["properties"]
    assert "total_minutes" not in fields and "preferences" not in fields


@pytest.mark.asyncio
async def test_reordered_session_inherits_validated_schedule_without_accepting(env):
    app, actor = env["app"], env["actor"]
    _, publication, _ = await publish_fixture(env)
    planner(env, [10])
    started = app.learning.start_session_plan(actor, request(env, [publication]))
    await app.execute(started["run_id"])
    result = app.runtime.stage(started["run_id"], "result")
    app.learning.accept_plan(actor, result["plan_id"], result["version_id"], 1)
    revised = app.learning.revise_plan(
        actor, result["plan_id"], 2, "保留单次顺序", result["module_ids"]
    )
    detail = app.learning.detail(actor, result["plan_id"])
    assert detail["active_version_id"] == result["version_id"]
    assert detail["versions"][-1]["planned_minutes"] == 10
    assert detail["versions"][-1]["schedule"] == result["schedule"]
    assert not detail["versions"][-1]["accepted"]
    accepted = app.learning.accept_plan(
        actor, result["plan_id"], revised["version_id"], revised["expected_revision"]
    )
    assert accepted["schedule_status"] == "accepted"


@pytest.mark.asyncio
async def test_too_short_session_creates_gap_without_model_call(env):
    app, actor = env["app"], env["actor"]
    _, publication, _ = await publish_fixture(env)
    calls = len(env["model"].calls)
    result = app.learning.start_session_plan(actor, request(env, [publication], total=3))
    assert result["status"] == "unbuildable" and result["model_calls"] == 0
    assert result["uncovered_requirements"][0]["reason"] == "time_budget_below_minimum_module"
    assert len(env["model"].calls) == calls
