"""Session graph dual-commit recovery and stale-worker fencing, synthetic only."""

from datetime import timedelta

import pytest
from sqlalchemy import select
from test_dialogue_actions import api_client, send
from test_session_graph_m2 import prepare

from careerforge.db import clock
from careerforge.errors import DomainError, GraphCheckpointUnavailable, LeaseLost
from careerforge.models import AgentRun, LearningPlan, ModelReceipt
from careerforge.models_dialogue import WorkflowExecution, WorkflowRunLink, WorkflowSession
from careerforge.workflows.session_graph import SessionGraphAdapter


def expire(app, execution_id):
    with app.db.transaction() as db:
        db.get(WorkflowExecution, execution_id).lease_until = clock(db) - timedelta(seconds=1)
    app.session_workflows.reap()


def plan_ids(app, workflow_id):
    with app.db.read() as db:
        return list(
            db.scalars(
                select(WorkflowRunLink.run_id).where(
                    WorkflowRunLink.workflow_id == workflow_id,
                    WorkflowRunLink.milestone == "plan",
                )
            )
        )


@pytest.mark.parametrize("stage", ["ensure_plan", "observe_plan"])
async def test_committed_plan_or_proposal_recovers_without_duplicate_model_work(
    env, monkeypatch, stage
):
    app = env["app"]
    async with api_client(env) as api:
        path, workflow_id, execution_id = await prepare(env, api, drain=False)
        adapter = app.session_graph = SessionGraphAdapter(app)
        crashed = False
        async with adapter.checkpointers.open() as saver:
            original = saver.aput

            async def crash(config, checkpoint, metadata, new_versions):
                nonlocal crashed
                if (
                    not crashed
                    and checkpoint.get("channel_values", {}).get("current_stage") == stage
                ):
                    crashed = True
                    raise GraphCheckpointUnavailable(
                        "synthetic checkpoint outage after domain commit"
                    )
                return await original(config, checkpoint, metadata, new_versions)

            monkeypatch.setattr(saver, "aput", crash)
            await app.drain()
        assert crashed
        with app.db.read() as db:
            row = db.get(WorkflowExecution, execution_id)
            assert row.status == "running"
            if stage == "observe_plan":
                assert db.get(WorkflowSession, workflow_id).target.get("proposal")
            receipts = list(db.scalars(select(ModelReceipt.id)))
        before = plan_ids(app, workflow_id)
        assert len(before) == 1
        expire(app, execution_id)
        await app.drain()
        assert plan_ids(app, workflow_id) == before
        with app.db.read() as db:
            row = db.get(WorkflowExecution, execution_id)
            assert (row.status, row.wait_kind) == ("waiting", "approval"), row.last_error_code
            assert list(db.scalars(select(ModelReceipt.id))) == receipts
        approved = (await send(api, path, "接受学习计划")).json()
        assert approved["action_receipts"][0]["status"] == "applied"
        await app.execute_workflow(execution_id)
        with app.db.read() as db:
            assert db.get(WorkflowExecution, execution_id).status == "completed"


async def test_acceptance_commits_before_wake_processing_and_replay_does_not_accept_twice(env):
    app = env["app"]
    async with api_client(env) as api:
        path, workflow_id, execution_id = await prepare(env, api)
        revision = (await api.get(path)).json()["revision"]
        first = (
            await send(api, path, "接受学习计划", key="accept-before-worker", revision=revision)
        ).json()
        assert first["action_receipts"][0]["status"] == "applied", first
        with app.db.read() as db:
            workflow = db.get(WorkflowSession, workflow_id)
            proposal = workflow.target["proposal"]
            assert db.get(WorkflowExecution, execution_id).status == "queued"
            plan = db.get(LearningPlan, proposal["plan_id"])
            assert plan.active_version_id == proposal["version_id"]
            plan_revision = plan.revision
            run_ids = list(db.scalars(select(AgentRun.id)))
        calls = len(env["model"].calls)
        second = (
            await send(api, path, "接受学习计划", key="accept-before-worker", revision=revision)
        ).json()
        assert second == first
        await app.execute_workflow(execution_id)
        await app.execute_workflow(execution_id)
        assert len(env["model"].calls) == calls
        with app.db.read() as db:
            assert db.get(WorkflowExecution, execution_id).status == "completed"
            assert db.get(LearningPlan, proposal["plan_id"]).revision == plan_revision
            assert list(db.scalars(select(AgentRun.id))) == run_ids


@pytest.mark.parametrize(
    "corruption", ["wrong_binding", "missing_identity", "completed_without_acceptance"]
)
async def test_checkpoint_cannot_override_identity_or_domain_approval(env, corruption):
    app = env["app"]
    async with api_client(env) as api:
        _, workflow_id, execution_id = await prepare(env, api)
        with app.db.transaction() as db:
            app.session_workflows.wake_in(db, db.get(WorkflowSession, workflow_id), kind="test")
        epoch, generation = app.session_workflows.claim(execution_id, "corrupt-checkpoint")
        with app.db.read() as db:
            execution = db.get(WorkflowExecution, execution_id)
        adapter = (
            SessionGraphAdapter(app) if corruption == "missing_identity" else app.session_graph
        )
        async with adapter.checkpointers.open() as saver:
            graph = adapter.graph(saver, execution, epoch, "corrupt-checkpoint")
            config = {"configurable": {"thread_id": execution_id}}
            snapshot = await graph.aget_state(config)
            values = {} if corruption == "missing_identity" else dict(snapshot.values)
            if corruption == "wrong_binding":
                values["goal_binding_hash"] = "foreign-binding"
            values["current_stage"] = "completed"
            await graph.aupdate_state(config, values, as_node="completed")
        with pytest.raises(DomainError) as error:
            await adapter.execute(execution_id, epoch, "corrupt-checkpoint", generation)
        assert error.value.code == (
            "GRAPH_APPROVAL" if corruption == "completed_without_acceptance" else "GRAPH_STATE"
        )
        with app.db.read() as db:
            assert not db.get(WorkflowSession, workflow_id).target.get("accepted_plan")
            assert db.get(WorkflowExecution, execution_id).acceptance_receipt_id is None


async def test_stale_worker_cannot_write_domain_or_wait_after_reclaim(env):
    app = env["app"]
    async with api_client(env) as api:
        _, _, execution_id = await prepare(env, api, drain=False)
    old_epoch, old_generation = app.session_workflows.claim(execution_id, "old-worker")
    expire(app, execution_id)
    new_epoch, generation = app.session_workflows.claim(execution_id, "new-worker")
    assert new_epoch == old_epoch + 1
    with pytest.raises(LeaseLost):
        with app.session_workflows.transaction(execution_id, old_epoch, "old-worker"):
            pytest.fail("stale worker must never enter a domain transaction")
    with pytest.raises(LeaseLost):
        app.session_workflows.project_wait(
            execution_id, old_epoch, "old-worker", old_generation, "approval"
        )
    # Completion may reject explicitly or no-op, but cannot touch the new owner.
    try:
        app.session_workflows.finish(execution_id, old_epoch, "old-worker", old_generation)
    except LeaseLost:
        pass
    with app.db.read() as db:
        row = db.get(WorkflowExecution, execution_id)
        assert (row.status, row.epoch, row.worker_id) == ("running", new_epoch, "new-worker")
    app.session_workflows.project_wait(execution_id, new_epoch, "new-worker", generation, "builds")
    assert not env["model"].calls


async def test_wake_racing_wait_projection_preserves_unhandled_generation(env):
    app = env["app"]
    async with api_client(env) as api:
        _, workflow_id, execution_id = await prepare(env, api, drain=False)
    epoch, captured = app.session_workflows.claim(execution_id, "racing-worker")
    with app.db.transaction() as db:
        workflow = db.get(WorkflowSession, workflow_id)
        app.session_workflows.wake_in(db, workflow, kind="new-domain-event")
        row = db.get(WorkflowExecution, execution_id)
        assert row.status == "running" and row.worker_id == "racing-worker"
        assert row.wake_generation == captured + 1
    app.session_workflows.project_wait(execution_id, epoch, "racing-worker", captured, "builds")
    with app.db.read() as db:
        row = db.get(WorkflowExecution, execution_id)
        assert row.status == "queued"
        assert row.handled_generation == captured
        assert row.wake_generation == captured + 1
        assert row.worker_id is None and row.lease_until is None
    await app.execute_workflow(execution_id)
    with app.db.read() as db:
        row = db.get(WorkflowExecution, execution_id)
        assert (row.status, row.wait_kind) == ("waiting", "builds")
        assert row.handled_generation == row.wake_generation == captured + 1
