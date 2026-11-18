"""Explicit synthetic tests. Clock travel is test-only, never native evidence."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from test_build_execution_v164 import body_for
from test_dialogue_actions import api_client, open_dialogue, send
from test_dialogue_workflow_e2e import fixed_source

from careerforge.contracts import QueryPlan
from careerforge.contracts_conversation_actions_v1 import ConversationActionPlan, DialogueMessage
from careerforge.db import aware, clock
from careerforge.errors import DomainError, LeaseLost
from careerforge.models import AgentRun, FundingAgreement, ModelReceipt
from careerforge.models_dialogue import WorkflowExecution
from careerforge.services.build_execution import (
    BUILD_EXECUTION_CONTRACT,
    UNLIMITED_BUILD_EXECUTION,
    UNLIMITED_BUILD_WALL_TIME,
    plan_execution,
    validate_execution_manifest,
)
from careerforge.services.quote_correction import quote_correction_enabled


def new_body(env, **changes):
    funding = env["app"].runtime.create_funding(env["actor"], env["ns"], 48, hours=2)
    return body_for(
        env,
        funding_id=funding,
        build_wall_time_contract=UNLIMITED_BUILD_WALL_TIME,
        call_cap=32,
        planning_call_seconds=60,
        **changes,
    )


def test_default_serialization_has_no_new_field_and_plan_still_1800s(env):
    body = body_for(env)
    assert "build_wall_time_contract" not in body.model_dump(mode="json")
    msg = DialogueMessage(text="/build", expected_focus_revision=1, idempotency_key="k")
    assert "build_wall_time_contract" not in msg.model_dump(mode="json")
    app = env["app"]
    identifier = app.build.start(env["actor"], body)
    with app.db.read() as db:
        row = db.get(AgentRun, identifier)
        assert "build_wall_time_contract" not in row.input_manifest
        assert row.input_manifest["execution_contract_version"] == BUILD_EXECUTION_CONTRACT
        assert abs((aware(row.deadline_at) - aware(row.created_at)).total_seconds() - 1800) < 2


@pytest.mark.parametrize("value", [True, False, 0, "unlimited", "unlimited-build-time-v2"])
def test_only_exact_version_is_accepted(env, value):
    with pytest.raises(ValidationError):
        body_for(env, build_wall_time_contract=value)
    with pytest.raises(ValidationError):
        DialogueMessage(text="/build", expected_focus_revision=1, idempotency_key="k",
                        build_wall_time_contract=value)


def test_capacity_is_only_call_bounded_but_retry_and_rounds_still_bounded(env):
    body = new_body(env, duration_minutes=1, auto_evidence_retry=True)
    plan = plan_execution(body, False)
    assert plan["call_capacity"] == 32
    assert plan["estimated_seconds"] > 60
    assert plan["analysis_rounds"] <= 4 and plan["reviewer_rounds"] <= 3
    assert plan["evidence_retry_call_reserve"] == 12
    assert plan["planned_call_ceiling"] <= 32
    identifier = env["app"].build.start(env["actor"], body)
    with env["app"].db.read() as db:
        run = db.get(AgentRun, identifier)
        assert validate_execution_manifest(run.input_manifest, 32) == plan
        assert quote_correction_enabled(run.input_manifest)
        assert aware(run.deadline_at) == aware(db.get(FundingAgreement, body.funding_id).expires_at)


@pytest.mark.parametrize("change", ["missing_wall", "missing_execution", "old_execution", "old_plan", "missing_plan_wall", "reduced_capacity"])
def test_contract_pair_and_plan_cannot_be_forged(env, change):
    app = env["app"]
    identifier = app.build.start(env["actor"], new_body(env))
    with app.db.read() as db:
        manifest = deepcopy(db.get(AgentRun, identifier).input_manifest)
    if change == "missing_wall":
        manifest.pop("build_wall_time_contract")
    elif change == "missing_execution":
        manifest.pop("execution_contract_version")
    elif change == "old_execution":
        manifest["execution_contract_version"] = BUILD_EXECUTION_CONTRACT
    elif change == "old_plan":
        manifest["execution_plan"]["version"] = BUILD_EXECUTION_CONTRACT
    elif change == "missing_plan_wall":
        manifest["execution_plan"].pop("build_wall_time_contract")
    else:
        manifest["execution_plan"]["call_capacity"] = 29
    with pytest.raises(DomainError) as exc:
        validate_execution_manifest(manifest, 32)
    assert exc.value.code == "BUILD_EXECUTION_CONTRACT"


def advance_test_clock(monkeypatch, instant):
    # Synthetic boundary test only. Never patch gates or mutate an admitted deadline.
    monkeypatch.setattr("careerforge.db.clock", lambda db: instant)
    monkeypatch.setattr("careerforge.services.runtime.clock", lambda db: instant)


async def test_dispatch_after_30_minutes_keeps_original_funding_deadline(env, monkeypatch):
    app = env["app"]
    body = new_body(env)
    identifier = app.build.start(env["actor"], body)
    with app.db.read() as db:
        expiry = aware(db.get(FundingAgreement, body.funding_id).expires_at)
        instant = clock(db) + timedelta(minutes=31)
        frozen = deepcopy(db.get(AgentRun, identifier).input_manifest)
    advance_test_clock(monkeypatch, instant)
    epoch = app.runtime.claim(identifier, "worker")
    assert epoch is not None
    await app.gateway.call(identifier, epoch, "worker", "after-30m", "planner", "plan", {}, QueryPlan)
    with app.db.read() as db:
        row = db.get(AgentRun, identifier)
        funding = db.get(FundingAgreement, body.funding_id)
        assert aware(row.deadline_at) == aware(funding.expires_at) == expiry
        assert row.input_manifest == frozen
        assert row.calls_consumed == funding.calls_consumed == 1
        assert funding.call_cap == 48 and row.call_cap == 32
        assert list(db.scalars(select(ModelReceipt)))[0].status == "completed"


async def test_near_funding_expiry_refuses_before_dispatch_without_renewal(env, monkeypatch):
    app = env["app"]
    body = new_body(env)
    identifier = app.build.start(env["actor"], body)
    with app.db.read() as db:
        expiry = aware(db.get(FundingAgreement, body.funding_id).expires_at)
    advance_test_clock(monkeypatch, expiry - timedelta(seconds=100))
    epoch = app.runtime.claim(identifier, "worker")
    with pytest.raises(DomainError) as exc:
        await app.gateway.call(identifier, epoch, "worker", "near-expiry", "planner", "plan", {}, QueryPlan)
    assert exc.value.code == "FUNDING_TIME_BUDGET"
    assert not env["model"].calls
    with app.db.read() as db:
        assert not list(db.scalars(select(ModelReceipt)))
        assert aware(db.get(FundingAgreement, body.funding_id).expires_at) == expiry


async def test_expired_funding_refuses_claim_and_dispatch_without_mutating_expiry(env, monkeypatch):
    app = env["app"]
    body = new_body(env)
    identifier = app.build.start(env["actor"], body)
    with app.db.read() as db:
        expiry = aware(db.get(FundingAgreement, body.funding_id).expires_at)
    advance_test_clock(monkeypatch, expiry)
    assert app.runtime.claim(identifier, "worker") is None
    with pytest.raises(LeaseLost):
        await app.gateway.call(identifier, 1, "worker", "expired", "planner", "plan", {}, QueryPlan)
    with app.db.read() as db:
        assert aware(db.get(FundingAgreement, body.funding_id).expires_at) == expiry
        assert db.get(AgentRun, identifier).status == "failed"
        assert not list(db.scalars(select(ModelReceipt)))
    assert not env["model"].calls


def test_optin_cannot_change_existing_run_or_reserve_more_calls(env):
    app = env["app"]
    body = body_for(env)
    identifier = app.build.start(env["actor"], body)
    with app.db.read() as db:
        original = deepcopy(db.get(AgentRun, identifier).input_manifest)
        deadline = db.get(AgentRun, identifier).deadline_at
    with pytest.raises(DomainError) as exc:
        app.build.start(env["actor"], body.model_copy(update={"build_wall_time_contract": UNLIMITED_BUILD_WALL_TIME}))
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"
    with app.db.read() as db:
        assert db.get(AgentRun, identifier).input_manifest == original
        assert db.get(AgentRun, identifier).deadline_at == deadline


async def test_children_inherit_new_contract_deadline_and_normal_review_gates(env):
    app = env["app"]
    body = new_body(env)
    identifier = app.build.start(env["actor"], body)
    await app.drain()
    with app.db.read() as db:
        root = db.get(AgentRun, identifier)
        children = list(db.scalars(select(AgentRun).where(AgentRun.parent_id == identifier)))
        assert len(children) == 2
        assert root.status in {"succeeded", "partially_succeeded"}
        for child in children:
            assert child.input_manifest["execution_contract_version"] == UNLIMITED_BUILD_EXECUTION
            assert child.input_manifest["build_wall_time_contract"] == UNLIMITED_BUILD_WALL_TIME
            assert child.deadline_at == root.deadline_at
            assert child.input_manifest["quote_correction_contract"]["max_attempts"] == 1
        assert db.get(FundingAgreement, body.funding_id).calls_held == 0


async def test_dialogue_optin_is_caller_only_and_frozen_in_binding(env):
    env["app"].settings = replace(env["app"].settings, workflow_engine="langgraph")
    env["app"].db.settings = env["app"].settings
    source = fixed_source(env)
    with pytest.raises(ValidationError):
        ConversationActionPlan.model_validate({"conversation_id": "c", "expected_focus_revision": 1,
            "idempotency_key": "k", "actions": [{"step_id": "1", "action": {
                "type": "start_build", "build_wall_time_contract": UNLIMITED_BUILD_WALL_TIME}}]})
    async with api_client(env) as api:
        path = await open_dialogue(api)
        await send(api, path, "我想学习 agent.tool_calling")
        response = await send(api, path, "/build", source_id=source,
            auto_evidence_retry=True, build_call_cap=32, authorization_ref=env["funding"],
            build_wall_time_contract=UNLIMITED_BUILD_WALL_TIME)
        assert response.status_code == 202, response.text
        identifier = response.json()["run_refs"][0]
    with env["app"].db.read() as db:
        row = db.get(AgentRun, identifier)
        assert row.input_manifest["build_wall_time_contract"] == UNLIMITED_BUILD_WALL_TIME
        execution = db.scalar(select(WorkflowExecution))
        assert execution.binding_payload["admitted_scope"]["build_wall_time_contract"] == UNLIMITED_BUILD_WALL_TIME


async def test_unknown_attempt_never_retried_under_unlimited_contract(env):
    app = env["app"]
    identifier = app.build.start(env["actor"], new_body(env))
    epoch = app.runtime.claim(identifier, "worker")
    env["model"].overrides["planner"] = lambda request: TimeoutError("synthetic transport loss")
    with pytest.raises(TimeoutError):
        await app.gateway.call(identifier, epoch, "worker", "uncertain", "planner", "plan", {}, QueryPlan)
    with pytest.raises(DomainError):
        await app.gateway.call(identifier, epoch, "worker", "uncertain", "planner", "plan", {}, QueryPlan)
    with app.db.read() as db:
        assert db.scalar(select(ModelReceipt)).status == "unknown"
        assert db.get(AgentRun, identifier).calls_consumed == 1
    assert len(env["model"].calls) == 1


async def test_32_call_cap_is_not_removed_with_build_clock(env):
    app = env["app"]
    body = new_body(env)
    identifier = app.build.start(env["actor"], body)
    epoch = app.runtime.claim(identifier, "worker")
    for index in range(32):
        await app.gateway.call(identifier, epoch, "worker", f"call-{index}", "planner", "plan", {}, QueryPlan)
    with pytest.raises(DomainError) as exc:
        await app.gateway.call(identifier, epoch, "worker", "call-33", "planner", "plan", {}, QueryPlan)
    assert exc.value.code in {"BUILD_BUDGET", "MODEL_BUDGET"}
    with app.db.read() as db:
        funding = db.get(FundingAgreement, body.funding_id)
        assert funding.call_cap == 48 and funding.calls_consumed == 32
        assert len(list(db.scalars(select(ModelReceipt)))) == 32
    assert len(env["model"].calls) == 32


def test_short_remaining_funding_still_cannot_admit_new_build(env, monkeypatch):
    app = env["app"]
    body = new_body(env)
    with app.db.read() as db:
        expiry = aware(db.get(FundingAgreement, body.funding_id).expires_at)
    advance_test_clock(monkeypatch, expiry - timedelta(seconds=100))
    with pytest.raises(DomainError) as exc:
        app.build.start(env["actor"], body)
    assert exc.value.code == "FUNDING_TIME_BUDGET"
    with app.db.read() as db:
        assert not list(db.scalars(select(AgentRun)))
        assert db.get(FundingAgreement, body.funding_id).calls_held == 0
