"""Synthetic engineering control/rollback checks, not native acceptance."""

from dataclasses import replace

import pytest
from sqlalchemy import select
from test_dialogue_actions import api_client
from test_evolution_graph_m6 import candidate_session

from careerforge.config import Settings
from careerforge.contracts_evolution_graph import EvolutionSessionInput
from careerforge.errors import DomainError
from careerforge.models import AgentRun, Conversation, ModelReceipt, Outbox
from careerforge.models_dialogue import WorkflowEvolutionRequest, WorkflowExecution, WorkflowSession


async def control(env, wid, kind, key):
    app = env["app"]
    with app.db.read() as db:
        revision = db.get(WorkflowSession, wid).revision
    async with api_client(env) as api:
        result = await api.post(
            f"/workflows/{wid}/{kind}",
            json={"expected_revision": revision, "idempotency_key": key},
        )
    assert result.status_code == 202, result.text
    return result.json()["action_receipts"][0]


def test_new_evolution_admission_defaults_off_and_requires_persistent_saver_outside_test():
    assert Settings().evolution_graph_enabled is False
    with pytest.raises(DomainError, match="checkpoint"):
        Settings(mode="development", evolution_graph_enabled=True).validate()


@pytest.mark.asyncio
async def test_admission_flag_rollback_does_not_disable_frozen_execution_or_receipt(env):
    app = env["app"]
    conv, _, value, admission = await candidate_session(env)
    app.settings = replace(app.settings, evolution_graph_enabled=False)
    assert app.evolution_workflows.submit(env["actor"], conv, value) == admission
    with pytest.raises(DomainError) as err:
        app.evolution_workflows.submit(
            env["actor"], conv, value.model_copy(update={"idempotency_key": "new-disabled"})
        )
    assert err.value.code == "EVOLUTION_ADMISSION_DISABLED"
    await app.drain()
    detail = app.evolution_workflows.detail(env["actor"], conv, admission["workflow_id"])
    assert detail["wait_kind"] == "replay_authorization"
    rejected = EvolutionSessionInput(
        contract="evolution-dialogue-v1",
        idempotency_key="reject-frozen",
        workflow_id=detail["workflow_id"],
        expected_workflow_revision=detail["workflow_revision"],
        intent={"type": "reject", "reason": "Explicit caller rejection"},
    )
    app.evolution_workflows.submit(env["actor"], conv, rejected)
    await app.drain()
    assert (
        app.evolution_workflows.detail(env["actor"], conv, admission["workflow_id"])["status"]
        == "completed"
    )


@pytest.mark.asyncio
async def test_unfunded_pause_resume_cancel_preserves_zero_run_and_focus(env):
    app = env["app"]
    conv, _, _, admission = await candidate_session(env, funding=False)
    await app.drain()
    with app.db.read() as db:
        focus = db.get(Conversation, conv).focus
        runs = set(db.scalars(select(AgentRun.id)))
        calls = set(db.scalars(select(ModelReceipt.id)))
    wid, eid = admission["workflow_id"], admission["execution_id"]
    assert (await control(env, wid, "pause", "evo-pause"))["control_state"] == "pause_requested"
    await app.drain()
    with app.db.read() as db:
        w = db.get(WorkflowSession, wid)
        command = app.workflow_controls.current(db, w)
        assert w.control_state == "paused" and command.checkpoint_id
        assert db.get(WorkflowExecution, eid).status == "paused"
    assert (await control(env, wid, "resume", "evo-resume"))["status"] == "applied"
    await app.drain()
    assert app.evolution_workflows.detail(env["actor"], conv, wid)["wait_kind"] == "authorization"
    assert (await control(env, wid, "cancel", "evo-cancel"))["status"] == "applied"
    with app.db.transaction() as db:
        app.evolution_workflows.wake_in(db, db.get(WorkflowSession, wid), kind="late-milestone")
        db.add(Outbox(kind="workflow.execute", aggregate_id=eid, payload={"approved": True}))
    await app.drain()
    with app.db.read() as db:
        assert db.get(WorkflowExecution, eid).status == "cancelled"
        assert db.get(WorkflowEvolutionRequest, admission["request_id"]).status == "cancelled"
        assert db.get(Conversation, conv).focus == focus
        assert set(db.scalars(select(AgentRun.id))) == runs
        assert set(db.scalars(select(ModelReceipt.id))) == calls
    assert (await control(env, wid, "resume", "evo-late-resume"))[
        "failure_code"
    ] == "TERMINAL_WORKFLOW"


@pytest.mark.asyncio
async def test_withdrawal_fences_paused_unfunded_session_without_resume(env):
    app = env["app"]
    conv, feedback, _, admission = await candidate_session(env, funding=False)
    await app.drain()
    await control(env, admission["workflow_id"], "pause", "withdraw-pause")
    await app.drain()
    before = len(env["model"].calls)
    app.evolution.revoke_feedback(env["actor"], feedback["feedback_id"])
    await app.drain()
    app.evolution.finish_feedback_withdrawal(env["actor"], feedback["feedback_id"])
    await app.drain()
    with app.db.read() as db:
        execution = db.get(WorkflowExecution, admission["execution_id"])
        request = db.get(WorkflowEvolutionRequest, admission["request_id"])
        assert (execution.status, execution.last_error_code) == ("failed", "EXPERIENCE_CONSENT")
        assert (request.status, request.run_id) == ("cancelled", None)
    assert len(env["model"].calls) == before
    assert (await control(env, admission["workflow_id"], "resume", "withdraw-resume"))[
        "failure_code"
    ] == "TERMINAL_WORKFLOW"


@pytest.mark.asyncio
async def test_paid_candidate_pause_after_receipt_resumes_original_budget_and_deadline(
    env, monkeypatch
):
    app = env["app"]
    conv, _, _, admission = await candidate_session(env)
    original = app.evolution.invoke_reflection
    paused = False

    async def pause_after_receipt(run, epoch, worker, *, replay_only=False):
        nonlocal paused
        output = await original(run, epoch, worker, replay_only=replay_only)
        if not replay_only and not paused:
            paused = True
            result = await control(env, admission["workflow_id"], "pause", "paid-pause")
            assert result["status"] == "applied"
        return output

    monkeypatch.setattr(app.evolution, "invoke_reflection", pause_after_receipt)
    await app.drain()
    detail = app.evolution_workflows.detail(env["actor"], conv, admission["workflow_id"])
    with app.db.read() as db:
        run = db.get(AgentRun, detail["run_id"])
        assert run.status == "paused" and run.calls_consumed == 1
        assert db.get(WorkflowExecution, admission["execution_id"]).status == "paused"
        deadline, epoch, cap = run.deadline_at, run.epoch, run.call_cap
    result = await control(env, admission["workflow_id"], "resume", "paid-resume")
    assert result["status"] == "applied"
    await app.drain()
    detail = app.evolution_workflows.detail(env["actor"], conv, admission["workflow_id"])
    assert detail["wait_kind"] == "replay_authorization"
    with app.db.read() as db:
        current = db.get(AgentRun, run.id)
        assert current.status == "succeeded" and current.epoch > epoch
        assert (current.calls_consumed, current.call_cap, current.deadline_at) == (1, cap, deadline)
        assert len(list(db.scalars(select(ModelReceipt).where(ModelReceipt.run_id == run.id)))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["cached_body", "cached_candidate", "admission_body"])
async def test_session_projection_never_dumps_or_retargets_cached_results(env, corruption):
    from careerforge.models import Idempotency

    app = env["app"]
    conv, _, value, admission = await candidate_session(env)
    await app.drain()
    with app.db.transaction() as db:
        request = db.get(WorkflowEvolutionRequest, admission["request_id"])
        if corruption == "admission_body":
            receipt = db.get(Idempotency, request.command_receipt_id)
            receipt.result = {**receipt.result, "private_body": "not-a-reference"}
        else:
            request.result = {
                **request.result,
                (
                    "private_body" if corruption == "cached_body" else "candidate_id"
                ): "not-a-reference",
            }
    with pytest.raises(DomainError) as err:
        if corruption == "admission_body":
            app.evolution_workflows.submit(env["actor"], conv, value)
        else:
            app.evolution_workflows.detail(env["actor"], conv, admission["workflow_id"])
    assert err.value.code == "EVOLUTION_GRAPH_BINDING"
