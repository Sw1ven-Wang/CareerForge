"""M3 control gates, isolated synthetic providers; not native model acceptance."""

from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select
from test_dialogue_actions import api_client, send
from test_session_graph_m2 import prepare

from careerforge.db import clock
from careerforge.errors import GraphCheckpointUnavailable
from careerforge.models import AgentRun, Conversation, ModelReceipt, Outbox
from careerforge.models_dialogue import WorkflowExecution, WorkflowSession
from careerforge.services.runtime import TERMINAL


def m3(env):
    app = env["app"]
    app.settings = replace(app.settings, session_graph_version="knowledge-session-v2")
    app.db.settings = app.settings
    return app


@pytest.mark.parametrize("drain", [False, True])
async def test_pause_requires_persisted_graph_checkpoint_then_resume_preserves_approval(env, drain):
    app = m3(env)
    async with api_client(env) as api:
        path, workflow_id, execution_id = await prepare(env, api, drain=drain)
        calls = len(env["model"].calls)
        result = (await send(api, path, "/pause", key="pause-1")).json()
        action = result["action_receipts"][0]
        assert action["status"] == "applied", result
        assert action["control_state"] == "pause_requested"
        assert (await api.get(path + "/workflow-status")).json()[
            "control_state"
        ] == "pause_requested"
        await app.drain()
        with app.db.read() as db:
            workflow = db.get(WorkflowSession, workflow_id)
            command = app.workflow_controls.current(db, workflow)
            execution = db.get(WorkflowExecution, execution_id)
            assert workflow.control_state == "paused"
            assert command.checkpoint_id and command.status == "applied"
            assert (execution.status, execution.wait_kind) == ("paused", "control")
            assert not workflow.target.get("accepted_plan")
        assert len(env["model"].calls) == calls
        resume = (await send(api, path, "/resume")).json()
        assert resume["action_receipts"][0]["control_state"] == "resume_requested", resume
        await app.drain()
        with app.db.read() as db:
            workflow = db.get(WorkflowSession, workflow_id)
            assert workflow.control_state == "active"
            assert not workflow.target.get("accepted_plan")
            assert db.get(WorkflowExecution, execution_id).wait_kind == "approval"
        # A second pause/resume uses another control interrupt, not the prior command.
        assert (await send(api, path, "/pause")).json()["action_receipts"][0]["status"] == "applied"
        await app.drain()
        assert (await send(api, path, "/resume")).json()["action_receipts"][0][
            "status"
        ] == "applied"
        await app.drain()
        with app.db.read() as db:
            assert not db.get(WorkflowSession, workflow_id).target.get("accepted_plan")


async def test_saver_failure_never_projects_paused_and_recovery_deduplicates(env, monkeypatch):
    app = m3(env)
    async with api_client(env) as api:
        path, workflow_id, execution_id = await prepare(env, api)
        adapter = app.session_control_graph
        calls = len(env["model"].calls)
        await send(api, path, "/pause")
        async with adapter.checkpointers.open() as saver:
            original = saver.aput

            async def crash(*args, **kwargs):
                raise GraphCheckpointUnavailable("synthetic saver outage")

            monkeypatch.setattr(saver, "aput", crash)
            await app.execute_workflow(execution_id)
            with app.db.read() as db:
                assert db.get(WorkflowSession, workflow_id).control_state == "pause_requested"
                assert (
                    app.workflow_controls.current(
                        db, db.get(WorkflowSession, workflow_id)
                    ).checkpoint_id
                    is None
                )
            monkeypatch.setattr(saver, "aput", original)
        with app.db.transaction() as db:
            db.get(WorkflowExecution, execution_id).lease_until = clock(db) - timedelta(seconds=1)
        app.session_workflows.reap()
        await app.drain()
        with app.db.read() as db:
            assert db.get(WorkflowSession, workflow_id).control_state == "paused"
        assert len(env["model"].calls) == calls


async def test_cancel_is_terminal_and_late_events_cannot_restart_or_refocus(env):
    app = m3(env)
    async with api_client(env) as api:
        path, workflow_id, execution_id = await prepare(env, api, drain=False)
        revision = (await api.get(path)).json()["revision"]
        first = (await send(api, path, "/cancel", key="cancel-once", revision=revision)).json()
        assert first["action_receipts"][0]["control_state"] == "cancelled", first
        assert (
            await send(api, path, "/cancel", key="cancel-once", revision=revision)
        ).json() == first
        with app.db.transaction() as db:
            old = db.get(WorkflowSession, workflow_id)
            app.session_workflows.wake_in(db, old, kind="late_approval")
            runs = list(db.scalars(select(AgentRun)))
            assert all(run.status == "cancelled" for run in runs)
            db.add(
                Outbox(
                    kind="workflow.execute", aggregate_id=execution_id, payload={"approved": True}
                )
            )
        await app.drain()
        assert not env["model"].calls
        reply = (await send(api, path, "/resume")).json()
        assert reply["action_receipts"][0]["failure_code"] == "TERMINAL_WORKFLOW"
        with app.db.read() as db:
            assert db.get(WorkflowExecution, execution_id).status == "cancelled"
            assert not list(db.scalars(select(ModelReceipt)))


async def test_focus_switch_does_not_resume_and_old_wake_does_not_take_focus(env):
    app = m3(env)
    async with api_client(env) as api:
        path, original, execution_id = await prepare(env, api)
        await send(api, path, "/pause")
        await app.drain()
        newer = (await send(api, path, "我想学习 Python")).json()["workflow_id"]
        assert newer != original
        with app.db.transaction() as db:
            app.session_workflows.wake_in(db, db.get(WorkflowSession, original), kind="late_build")
        await app.drain()
        with app.db.read() as db:
            assert db.get(Conversation, path.split("/")[-1]).focus["active_workflow_id"] == newer
            assert db.get(WorkflowSession, original).control_state == "paused"
        reply = (await send(api, path, "继续原来的任务")).json()
        assert reply["workflow_id"] == original, reply
        await app.drain()
        with app.db.read() as db:
            assert db.get(WorkflowSession, original).control_state == "active"
            assert db.get(WorkflowExecution, execution_id).wait_kind == "approval"


async def test_v1_execution_pause_uses_original_persisted_checkpoint_without_topology_upgrade(env):
    app = env["app"]
    async with api_client(env) as api:
        path, workflow_id, execution_id = await prepare(env, api, drain=False)
        response = (await send(api, path, "/pause")).json()
        assert response["action_receipts"][0]["control_state"] == "pause_requested", response
        assert (await api.get(path + "/workflow-status")).json()[
            "control_state"
        ] == "pause_requested"
        await app.drain()
        with app.db.read() as db:
            workflow = db.get(WorkflowSession, workflow_id)
            execution = db.get(WorkflowExecution, execution_id)
            assert (
                workflow.control_state == "paused"
                and execution.graph_version == "knowledge-session-v1"
            )
            assert app.workflow_controls.current(db, workflow).checkpoint_id
        assert not env["model"].calls
        await send(api, path, "/resume")
        await app.drain()
        with app.db.read() as db:
            assert db.get(WorkflowExecution, execution_id).wait_kind == "approval"


async def test_generic_resume_and_targeted_api_never_redirect_to_ancestor(env):
    app = m3(env)
    async with api_client(env) as api:
        path, original, _ = await prepare(env, api)
        await send(api, path, "/pause")
        await app.drain()
        newer = (await send(api, path, "我想学习 Python")).json()["workflow_id"]
        response = (await send(api, path, "/resume")).json()
        assert response["action_receipts"][0]["failure_code"] == "PAUSE_NOT_ACKNOWLEDGED", response
        with app.db.read() as db:
            current = db.get(WorkflowSession, newer)
            revision = current.revision
            assert db.get(WorkflowSession, original).control_state == "paused"
            assert db.get(Conversation, path.split("/")[-1]).focus["active_workflow_id"] == newer
        response = (
            await api.post(
                f"/workflows/{newer}/resume",
                json={"expected_revision": revision, "idempotency_key": "resume-exact-child"},
            )
        ).json()
        assert response["action_receipts"][0]["failure_code"] == "PAUSE_NOT_ACKNOWLEDGED", response


@pytest.mark.parametrize("intent", ["slash", "targeted", "original"])
async def test_v1_active_child_resolves_target_before_control_handler(env, intent):
    app = env["app"]
    async with api_client(env) as api:
        path, original, _ = await prepare(env, api)
        await send(api, path, "/pause")
        await app.drain()
        child = (await send(api, path, "我想学习 Python")).json()["workflow_id"]
        if intent == "targeted":
            with app.db.read() as db:
                revision = db.get(WorkflowSession, child).revision
            result = (
                await api.post(
                    f"/workflows/{child}/resume",
                    json={"expected_revision": revision, "idempotency_key": "resume-child"},
                )
            ).json()
        else:
            result = (
                await send(api, path, "/resume" if intent == "slash" else "继续原来的任务")
            ).json()
        outcome = result["action_receipts"][0]
        if intent == "original":
            assert (
                outcome["workflow_id"] == original
                and outcome["control_state"] == "resume_requested"
            ), result
            assert outcome["command_id"]
        else:
            assert (
                outcome["status"] == "failed"
                and outcome["failure_code"] == "PAUSE_NOT_ACKNOWLEDGED"
            ), result
            with app.db.read() as db:
                assert (
                    db.get(Conversation, path.split("/")[-1]).focus["active_workflow_id"] == child
                )
                assert db.get(WorkflowSession, original).control_state == "paused"


async def test_completion_pause_race_retains_verified_terminal_graph_and_control(env, monkeypatch):
    from careerforge.contracts_conversation_actions_v1 import DialogueMessage

    app = m3(env)
    async with api_client(env) as api:
        path, workflow_id, execution_id = await prepare(env, api)
        cid = path.split("/")[-1]
        with app.db.read() as db:
            revision = db.get(Conversation, cid).revision
        accepted = app.dialogue.dispatch(
            env["actor"],
            cid,
            DialogueMessage(
                text="accept_plan", expected_focus_revision=revision, idempotency_key="only-accept"
            ),
            forced_actions=[("accept_plan", {})],
        )
        assert accepted["action_receipts"][0]["status"] == "applied"
        original = app.session_workflows.complete_checkpoint
        called = False

        def pause_before_domain_finish(*args, **kwargs):
            nonlocal called
            if not called:
                called = True
                with app.db.read() as db:
                    revision = db.get(Conversation, cid).revision
                paused = app.dialogue.dispatch(
                    env["actor"],
                    cid,
                    DialogueMessage(
                        text="/pause",
                        expected_focus_revision=revision,
                        idempotency_key="raced-terminal-pause",
                    ),
                )
                assert paused["action_receipts"][0]["control_state"] == "pause_requested"
            return original(*args, **kwargs)

        monkeypatch.setattr(
            app.session_workflows, "complete_checkpoint", pause_before_domain_finish
        )
        await app.drain()
        assert called
        with app.db.read() as db:
            execution = db.get(WorkflowExecution, execution_id)
            assert execution.status == "completed" and execution.terminal_checkpoint_id
            assert execution.last_error_code is None
            assert db.get(WorkflowSession, workflow_id).control_state == "paused"
        resumed = (await send(api, path, "/resume")).json()
        assert resumed["action_receipts"][0]["control_state"] == "active"
        with app.db.read() as db:
            assert db.get(WorkflowExecution, execution_id).status == "completed"
        assert (await send(api, path, "/cancel")).json()["action_receipts"][0][
            "control_state"
        ] == "cancelled"
        with app.db.read() as db:
            assert db.get(WorkflowExecution, execution_id).status == "completed"


async def test_historical_v1_pause_without_checkpoint_resumes_through_original_start(env):
    app = env["app"]
    async with api_client(env) as api:
        path, workflow_id, execution_id = await prepare(env, api, drain=False)
        with app.db.transaction() as db:
            conv = db.get(Conversation, path.split("/")[-1])
            workflow = db.get(WorkflowSession, workflow_id)
            app.dialogue._pause_in(db, conv, workflow)
        app.process_projections()
        with app.db.read() as db:
            workflow = db.get(WorkflowSession, workflow_id)
            assert workflow.control_state == "paused" and workflow.control_generation == 0
        response = (await send(api, path, "/resume")).json()
        assert response["action_receipts"][0]["control_state"] == "resume_requested", response
        await app.drain()
        with app.db.read() as db:
            assert db.get(WorkflowSession, workflow_id).control_state == "active"
            assert db.get(WorkflowExecution, execution_id).wait_kind == "approval"


@pytest.mark.parametrize("version", ["knowledge-session-v1", "knowledge-session-v2"])
@pytest.mark.parametrize("historical", [False, True])
async def test_completed_graph_controls_live_tutor_preserving_terminal_history(
    env, version, historical, monkeypatch
):
    from careerforge.contracts_conversation_actions_v1 import DialogueMessage

    app = env["app"]
    app.settings = replace(app.settings, session_graph_version=version)
    app.db.settings = app.settings
    async with api_client(env) as api:
        path, workflow_id, execution_id = await prepare(env, api)
        cid = path.split("/")[-1]
        with app.db.read() as db:
            revision = db.get(Conversation, cid).revision
        accepted = app.dialogue.dispatch(
            env["actor"],
            cid,
            DialogueMessage(
                text="accept_plan", expected_focus_revision=revision, idempotency_key="only-accept"
            ),
            forced_actions=[("accept_plan", {})],
        )
        assert accepted["action_receipts"][0]["status"] == "applied"
        await app.drain()
        with app.db.transaction() as db:
            row = db.get(WorkflowExecution, execution_id)
            assert row.status == "completed" and row.terminal_checkpoint_id
            if historical:
                row.terminal_checkpoint_id = None
        taught = (await send(api, path, "/teach", authorization_ref=env["funding"])).json()
        assert taught["action_receipts"][0]["status"] == "applied", taught
        pause = (await send(api, path, "/pause")).json()
        assert pause["action_receipts"][0]["status"] == "applied", pause
        if historical:
            from contextlib import asynccontextmanager

            adapter = (
                app.session_graph
                if version == "knowledge-session-v1"
                else app.session_control_graph
            )
            original_open = adapter.checkpointers.open

            @asynccontextmanager
            async def unavailable():
                raise GraphCheckpointUnavailable("offline")
                yield  # pragma: no cover

            monkeypatch.setattr(adapter.checkpointers, "open", unavailable)
            await app.execute_workflow(execution_id)
            with app.db.transaction() as db:
                row = db.get(WorkflowExecution, execution_id)
                assert row.status == "completed" and row.terminal_checkpoint_id is None
                assert db.get(WorkflowSession, workflow_id).control_state == "pause_requested"
                for outbox in db.scalars(select(Outbox).where(Outbox.aggregate_id == execution_id)):
                    outbox.delivered = True
            app.session_workflows.reap()
            with app.db.read() as db:
                assert db.scalar(
                    select(Outbox.id).where(
                        Outbox.aggregate_id == execution_id, Outbox.delivered.is_(False)
                    )
                )
            monkeypatch.setattr(adapter.checkpointers, "open", original_open)
        await app.drain()
        with app.db.read() as db:
            workflow = db.get(WorkflowSession, workflow_id)
            row = db.get(WorkflowExecution, execution_id)
            command = app.workflow_controls.current(db, workflow)
            assert workflow.control_state == "paused"
            assert row.status == "completed" and row.terminal_checkpoint_id
            assert command.checkpoint_id == row.terminal_checkpoint_id
            assert all(
                run.status in TERMINAL | {"paused"} for run in app.dialogue._roots_in(db, workflow)
            )
            assert any(run.status == "paused" for run in app.dialogue._roots_in(db, workflow))
        resumed = (await send(api, path, "/resume")).json()
        assert resumed["action_receipts"][0]["control_state"] == "active", resumed
        # Cancellation needs no terminal checkpoint read, including migrated rows.
        with app.db.transaction() as db:
            if historical:
                db.get(WorkflowExecution, execution_id).terminal_checkpoint_id = None
        cancelled = (await send(api, path, "/cancel")).json()
        assert cancelled["action_receipts"][0]["control_state"] == "cancelled", cancelled
        with app.db.read() as db:
            workflow = db.get(WorkflowSession, workflow_id)
            assert db.get(WorkflowExecution, execution_id).status == "completed"
            assert all(run.status in TERMINAL for run in app.dialogue._roots_in(db, workflow))
