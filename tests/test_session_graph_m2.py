"""Session graph domain authority and durable approval, synthetic providers only."""

from dataclasses import replace

import pytest
from sqlalchemy import select
from test_dialogue_actions import api_client, open_dialogue, send
from test_dialogue_workflow_e2e import fixed_source
from test_learning_loop_v16 import configure_planner

from careerforge.models import AgentRun, LearningPlan, Outbox
from careerforge.models_dialogue import WorkflowExecution, WorkflowRunLink, WorkflowSession


def enable(env):
    app = env["app"]
    app.settings = replace(app.settings, workflow_engine="langgraph")
    app.db.settings = app.settings
    configure_planner(env)


async def prepare(env, api, *, drain=True):
    enable(env)
    source = fixed_source(env)
    path = await open_dialogue(api)
    await send(api, path, "我想学习 agent.tool_calling")
    reply = (
        await send(api, path, "/build", source_id=source, authorization_ref=env["funding"])
    ).json()
    assert reply["action_receipts"][0]["status"] == "applied", reply
    execution_id = reply["action_receipts"][0]["execution_id"]
    if drain:
        await env["app"].drain()
    return path, reply["workflow_id"], execution_id


async def test_graph_waits_for_actual_action_then_finishes(env):
    app = env["app"]
    async with api_client(env) as api:
        path, workflow_id, execution_id = await prepare(env, api)
        status = (await api.get(path + "/workflow-status")).json()
        assert status["milestone"] == "awaiting_plan_acceptance", status
        assert status["execution_state"] == "waiting_user"
        with app.db.read() as db:
            execution = db.get(WorkflowExecution, execution_id)
            assert execution.status == "waiting" and execution.wait_kind == "approval"
            proposal = db.get(WorkflowSession, workflow_id).target["proposal"]
            assert db.get(LearningPlan, proposal["plan_id"]).active_version_id is None
        approved = (await send(api, path, "接受学习计划")).json()
        assert approved["action_receipts"][0]["status"] == "applied", approved
        await app.drain()
        with app.db.read() as db:
            execution = db.get(WorkflowExecution, execution_id)
            assert execution.status == "completed", execution.last_error_code
            assert execution.acceptance_receipt_id == approved["receipt_id"]
            assert (
                db.get(LearningPlan, proposal["plan_id"]).active_version_id
                == proposal["version_id"]
            )
            assert (
                len(
                    list(
                        db.scalars(
                            select(WorkflowRunLink).where(
                                WorkflowRunLink.workflow_id == workflow_id,
                                WorkflowRunLink.milestone == "plan",
                            )
                        )
                    )
                )
                == 1
            )


async def test_old_milestone_wakes_do_not_approve_and_flag_rollback_keeps_owner(env):
    app = env["app"]
    async with api_client(env) as api:
        path, workflow_id, execution_id = await prepare(env, api)
        app.settings = replace(app.settings, workflow_engine="legacy")
        app.db.settings = app.settings
        with app.db.transaction() as db:
            links = list(
                db.scalars(
                    select(WorkflowRunLink).where(WorkflowRunLink.workflow_id == workflow_id)
                )
            )
            for link in links:
                db.add(
                    Outbox(
                        kind="workflow.milestone",
                        aggregate_id=link.run_id,
                        payload={"approved": True},
                    )
                )
            app.session_workflows.wake_in(db, db.get(WorkflowSession, workflow_id), kind="old_plan")
        calls = len(env["model"].calls)
        await app.drain()
        assert len(env["model"].calls) == calls
        with app.db.read() as db:
            assert db.get(WorkflowExecution, execution_id).status == "waiting"
            assert not db.get(WorkflowSession, workflow_id).target.get("accepted_plan")
        assert (await api.get(path + "/workflow-status")).json()[
            "waiting_user_action"
        ] == "accept_plan"


@pytest.mark.parametrize("phase", ["builds", "approval"])
async def test_session_pause_resume_preserves_wait_and_receipts(env, phase):
    app = env["app"]
    async with api_client(env) as api:
        path, workflow_id, execution_id = await prepare(env, api, drain=phase == "approval")
        paused = (await send(api, path, "/pause")).json()
        assert paused["action_receipts"][0]["status"] == "applied", paused
        await app.drain()
        with app.db.read() as db:
            assert db.get(WorkflowExecution, execution_id).status == "paused"
        resumed = (await send(api, path, "/resume")).json()
        assert resumed["action_receipts"][0]["status"] == "applied", resumed
        await app.drain()
        with app.db.read() as db:
            assert db.get(WorkflowExecution, execution_id).status == "waiting"
            assert not db.get(WorkflowSession, workflow_id).target.get("accepted_plan")
            assert (
                len(
                    list(
                        db.scalars(
                            select(WorkflowRunLink).where(
                                WorkflowRunLink.workflow_id == workflow_id,
                                WorkflowRunLink.milestone == "plan",
                            )
                        )
                    )
                )
                == 1
            )


async def test_exhausted_auto_plan_authorization_recovers_from_durable_explicit_request(
    env, monkeypatch
):
    from careerforge.errors import DomainError
    from careerforge.models_dialogue import WorkflowPlanRequest

    app = env["app"]
    original = app.continuation.propose_plan
    denied = False

    def exhausted(db, actor, conv, workflow, explicit_funding=None):
        nonlocal denied
        if not denied:
            denied = True
            raise DomainError("BUDGET_EXHAUSTED", "Synthetic exhausted authorization")
        return original(db, actor, conv, workflow, explicit_funding)

    monkeypatch.setattr(app.continuation, "propose_plan", exhausted)
    async with api_client(env) as api:
        path, workflow_id, execution_id = await prepare(env, api)
        with app.db.read() as db:
            execution = db.get(WorkflowExecution, execution_id)
            assert (execution.status, execution.wait_kind) == ("waiting", "authorization")
            assert not list(
                db.scalars(
                    select(WorkflowRunLink).where(
                        WorkflowRunLink.workflow_id == workflow_id,
                        WorkflowRunLink.milestone == "plan",
                    )
                )
            )
        replacement = app.runtime.create_funding(env["actor"], env["ns"], 100)
        reply = (await send(api, path, "/plan", authorization_ref=replacement)).json()
        assert reply["action_receipts"][0]["status"] == "applied", reply
        with app.db.read() as db:
            request = db.scalar(
                select(WorkflowPlanRequest).where(WorkflowPlanRequest.execution_id == execution_id)
            )
            assert request.funding_id == replacement
            assert request.action_receipt_id == reply["receipt_id"] and request.status == "pending"
        await app.drain()
        with app.db.read() as db:
            request = db.get(WorkflowPlanRequest, request.id)
            assert request.status == "applied"
            assert db.get(AgentRun, request.plan_run_id).funding_id == replacement
            assert db.get(WorkflowExecution, execution_id).wait_kind == "approval"


async def test_revoked_pending_plan_request_cannot_spend_then_new_request_can_recover(
    env, monkeypatch
):
    from careerforge.models_dialogue import WorkflowPlanRequest
    from careerforge.services.workflow_continuation import _wait

    app = env["app"]
    original = app.continuation.propose_plan

    def require_authorization(db, actor, conv, workflow, explicit_funding=None):
        _wait(workflow, "building", "authorize_plan")
        return {"status": "waiting_confirmation"}

    monkeypatch.setattr(app.continuation, "propose_plan", require_authorization)
    async with api_client(env) as api:
        path, _, execution_id = await prepare(env, api)
        monkeypatch.setattr(app.continuation, "propose_plan", original)
        rejected = app.runtime.create_funding(env["actor"], env["ns"], 100)
        await send(api, path, "/plan", authorization_ref=rejected)
        app.runtime.revoke_funding(env["actor"], rejected)
        await app.drain()
        with app.db.read() as db:
            request = db.scalar(
                select(WorkflowPlanRequest).where(WorkflowPlanRequest.execution_id == execution_id)
            )
            assert request.status == "rejected" and request.plan_run_id is None
            assert db.get(WorkflowExecution, execution_id).wait_kind == "authorization"
        valid = app.runtime.create_funding(env["actor"], env["ns"], 100)
        await send(api, path, "/plan", authorization_ref=valid)
        await app.drain()
        with app.db.read() as db:
            assert db.get(WorkflowExecution, execution_id).wait_kind == "approval"
            plans = list(
                db.scalars(
                    select(WorkflowPlanRequest).where(
                        WorkflowPlanRequest.execution_id == execution_id,
                        WorkflowPlanRequest.status == "applied",
                    )
                )
            )
            assert len(plans) == 1 and plans[0].funding_id == valid


async def test_revoked_source_does_not_erase_other_reviewed_partial_evidence(env):
    from careerforge.models import ObjectRow

    app = env["app"]
    enable(env)
    first = fixed_source(env, "still-readable")
    revoked = fixed_source(env, "revoked-source", "b" * 40)
    async with api_client(env) as api:
        path = await open_dialogue(api)
        await send(api, path, "我想学习 agent.tool_calling")
        response = (
            await send(
                api, path, "/build", source_ids=[first, revoked], authorization_ref=env["funding"]
            )
        ).json()
        execution_id = response["action_receipts"][0]["execution_id"]
        with app.db.transaction() as db:
            db.get(ObjectRow, revoked).state = "revoked"
        await app.drain()
        status = (await api.get(path + "/workflow-status")).json()
        assert status["coverage"] == "partial" and status["publication_count"] > 0, status
        with app.db.read() as db:
            execution = db.get(WorkflowExecution, execution_id)
            assert (execution.status, execution.wait_kind) == ("waiting", "approval"), (
                execution.last_error_code
            )


@pytest.mark.parametrize("pending", [True, False])
def test_legacy_aggregate_cannot_propose_early_or_again(env, monkeypatch, pending):
    from types import SimpleNamespace

    import careerforge.services.workflow_continuation as continuation

    app = env["app"]
    run = SimpleNamespace(id="synthetic-run", input_hash="synthetic-hash", status="succeeded")
    other = SimpleNamespace(status="queued" if pending else "succeeded")
    workflow = SimpleNamespace(
        id="legacy-workflow",
        control_state="active",
        target={
            "publication_ids": ["existing-reviewed"],
            "build_results_finalized": not pending,
        },
    )
    monkeypatch.setattr(continuation, "_runs_for", lambda *args: [run, other])
    monkeypatch.setattr(
        app.continuation,
        "propose_plan",
        lambda *args: pytest.fail("must not propose before aggregation or on finalized replay"),
    )
    with app.db.read() as db:
        app.continuation._observe(db, env["actor"], None, workflow, "build", run)


async def test_graph_activity_stream_pages_and_reconnects_without_losing_answer_events(env):
    import json

    app = env["app"]
    async with api_client(env) as api:
        path, workflow_id, _ = await prepare(env, api)
        approved = (await send(api, path, "接受学习计划")).json()
        await app.drain()
        with app.db.transaction() as db:
            workflow = db.get(WorkflowSession, workflow_id)
            conv = app.workflows.conversation(db, env["actor"], workflow.conversation_id, lock=True)
            for index in range(170):
                app.workflows.emit_in(
                    db,
                    conv,
                    "workflow.stage.completed",
                    f"paging-proof:{index}",
                    workflow,
                    stage="proof",
                )
        cursor, rows, pages = approved["cursor"], [], 0
        while pages < 10:
            response = await api.get(
                path + "/activity-events",
                params={"once": True},
                headers={"Last-Event-ID": str(cursor)},
            )
            batch = [
                json.loads(line[6:])
                for line in response.text.splitlines()
                if line.startswith("data: ")
            ]
            if not batch:
                break
            pages += 1
            assert all(row["seq"] > cursor for row in batch)
            rows.extend(batch)
            cursor = batch[-1]["seq"]
        assert pages >= 2 and len(rows) >= 170
        assert len({row["seq"] for row in rows}) == len(rows)
        assert any("answer_event" in row for row in rows)


@pytest.mark.parametrize("operation", ["wake", "pause", "plan_request", "acceptance"])
async def test_cached_execution_cannot_reopen_terminal_state_on_wake(env, operation):
    from sqlalchemy import update

    app = env["app"]
    async with api_client(env) as api:
        _, workflow_id, execution_id = await prepare(env, api, drain=False)
    with app.db.transaction() as db:
        workflow = db.get(WorkflowSession, workflow_id)
        cached = db.get(WorkflowExecution, execution_id)
        generation = cached.wake_generation
        # Database terminal commit is newer than this Session's identity map.
        db.execute(
            update(WorkflowExecution)
            .where(WorkflowExecution.id == execution_id)
            .values(status="failed")
            .execution_options(synchronize_session=False)
        )
        assert cached.status == "queued"
        if operation == "wake":
            app.session_workflows.wake_in(db, workflow, cached, "stale-wake")
        elif operation == "pause":
            app.session_workflows.pause_in(db, workflow)
        else:
            from types import SimpleNamespace

            from careerforge.errors import DomainError

            with pytest.raises(DomainError) as error:
                app.session_workflows.record_action_in(
                    db,
                    None,
                    workflow,
                    SimpleNamespace(id="unused"),
                    0,
                    "propose_plan" if operation == "plan_request" else "accept_plan",
                    SimpleNamespace(authorization_ref=env["funding"]),
                    {"status": "queued"}
                    if operation == "plan_request"
                    else {"version_id": "unused"},
                )
            assert error.value.code == "TERMINAL_WORKFLOW"
        assert cached.status == "failed" and cached.wake_generation == generation


@pytest.mark.parametrize("limit", [0, 1])
async def test_bounded_local_drain_preserves_unattempted_delivery(env, limit):
    app = env["app"]
    async with api_client(env) as api:
        _, _, execution_id = await prepare(env, api, drain=False)
    with app.db.read() as db:
        pending = [
            (row.id, row.kind, row.aggregate_id)
            for row in db.scalars(
                select(Outbox).where(
                    Outbox.kind.in_(["run", "workflow.execute"]), Outbox.delivered.is_(False)
                )
            )
        ]
    await app.drain(limit=limit)
    with app.db.read() as db:
        execution = db.get(WorkflowExecution, execution_id)
        for delivery_id, kind, _ in pending:
            if limit == 0 or kind == "run":
                assert not db.get(Outbox, delivery_id).delivered
        assert execution.status == ("queued" if limit == 0 else "waiting")
    await app.drain()
    with app.db.read() as db:
        assert db.get(WorkflowExecution, execution_id).wait_kind == "approval"
