"""Retained independent M3 review repros: crash windows, root guards and receipts."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_dialogue_actions import api_client, send
from test_session_controls_m3 import m3
from test_session_graph_m2 import prepare

from careerforge.contracts import ModelOutput
from careerforge.db import clock
from careerforge.errors import DomainError, GraphCheckpointUnavailable, PauseBoundary
from careerforge.models import AgentRun, FundingAgreement, ModelReceipt
from careerforge.models_dialogue import WorkflowExecution, WorkflowSession


@pytest.mark.asyncio
async def test_post_checkpoint_preack_crash_recovers(env, monkeypatch):
    app = m3(env)
    async with api_client(env) as api:
        path, wid, eid = await prepare(env, api)
        calls = len(env["model"].calls)
        await send(api, path, "/pause")
        adapter = app.session_control_graph
        original = adapter.checkpoint_pause

        def crash(*args, **kwargs):
            raise GraphCheckpointUnavailable("crash after persisted checkpoint")

        monkeypatch.setattr(adapter, "checkpoint_pause", crash)
        await app.execute_workflow(eid)
        with app.db.transaction() as db:
            wf = db.get(WorkflowSession, wid)
            assert wf.control_state == "pause_requested"
            assert not app.workflow_controls.current(db, wf).checkpoint_id
            db.get(WorkflowExecution, eid).lease_until = clock(db) - timedelta(seconds=1)
        monkeypatch.setattr(adapter, "checkpoint_pause", original)
        app.session_workflows.reap()
        await app.drain()
        with app.db.read() as db:
            wf = db.get(WorkflowSession, wid)
            assert wf.control_state == "paused"
            assert app.workflow_controls.current(db, wf).checkpoint_id
        assert len(env["model"].calls) == calls


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", ["deadline", "funding", "profile"])
async def test_original_root_resume_guards(env, guard):
    app = m3(env)
    async with api_client(env) as api:
        path, wid, eid = await prepare(env, api, drain=False)
        await send(api, path, "/pause")
        await app.drain()
        with app.db.transaction() as db:
            root = app.workflow_controls.roots(db, db.get(WorkflowSession, wid))[0]
            if guard == "deadline":
                root.deadline_at = clock(db) - timedelta(seconds=1)
            elif guard == "funding":
                db.get(FundingAgreement, root.funding_id).expires_at = clock(db) - timedelta(
                    seconds=1
                )
            else:
                root.input_manifest = {**root.input_manifest, "model_profile": "different"}
        result = (await send(api, path, "/resume")).json()["action_receipts"][0]
        assert result["status"] == "failed", result
        assert (
            result["failure_code"]
            == {
                "deadline": "WAITING_REAUTHORIZATION",
                "funding": "FUNDING_UNAVAILABLE",
                "profile": "MODEL_PROFILE_CHANGED",
            }[guard]
        )
        with app.db.read() as db:
            assert db.get(WorkflowSession, wid).control_state == "paused"
        assert not env["model"].calls


@pytest.mark.asyncio
async def test_cancel_rolls_back_with_receipt_savepoint(env, monkeypatch):
    app = m3(env)
    async with api_client(env) as api:
        path, wid, eid = await prepare(env, api, drain=False)
        with app.db.read() as db:
            roots = app.workflow_controls.roots(db, db.get(WorkflowSession, wid))
            before = [(r.id, r.status, r.budget_settled, r.epoch) for r in roots]
            f = db.get(FundingAgreement, env["funding"])
            funding = (f.calls_held, f.calls_consumed)
        original = app.workflows.emit_in

        def fail(db, conv, kind, *args, **kwargs):
            if kind == "workflow.cancel_requested":
                raise DomainError("TEST_ROLLBACK", "rollback")
            return original(db, conv, kind, *args, **kwargs)

        monkeypatch.setattr(app.workflows, "emit_in", fail)
        result = (await send(api, path, "/cancel")).json()
        assert result["action_receipts"][0]["failure_code"] == "TEST_ROLLBACK"
        with app.db.read() as db:
            wf = db.get(WorkflowSession, wid)
            assert wf.control_state == "active"
            assert [
                (r.id, r.status, r.budget_settled, r.epoch)
                for r in app.workflow_controls.roots(db, wf)
            ] == before
            f = db.get(FundingAgreement, env["funding"])
            assert (f.calls_held, f.calls_consumed) == funding
            assert db.get(WorkflowExecution, eid).status == "queued"


@pytest.mark.asyncio
@pytest.mark.parametrize("known", [True, False])
async def test_graph_pause_waits_for_active_child_receipt(env, known):
    app = m3(env)
    entered, release = asyncio.Event(), asyncio.Event()

    async def provider(request):
        env["model"].calls.append(request)
        entered.set()
        await release.wait()
        if not known:
            raise DomainError("NETWORK_OUTCOME_UNKNOWN", "synthetic unknown")
        return ModelOutput(
            text="synthetic reviewed response", usage={"prompt_tokens": 1}, synthetic=True
        )

    env["model"].complete = provider
    async with api_client(env) as api:
        path, wid, eid = await prepare(env, api, drain=False)
        root = app.runtime.create(
            env["actor"], env["ns"], env["funding"], "analysis", {}, 4, uuid4().hex
        )
        with app.db.transaction() as db:
            app.workflows.link_in(db, db.get(WorkflowSession, wid), db.get(AgentRun, root), "build")
        er = app.runtime.claim(root, "parent-worker")
        child = app.runtime.create_child(
            root, er, "parent-worker", "independent-analysis", "analysis", {}, 2
        )
        ec = app.runtime.claim(child, "child-worker")

        async def call():
            try:
                return await app.gateway.call(
                    child, ec, "child-worker", "paid", "analysis", "test", {}
                )
            except PauseBoundary:
                app.runtime.acknowledge_pause(child, ec, "child-worker")
            except DomainError:
                app.runtime.finish(child, ec, "child-worker", "failed", "NETWORK_OUTCOME_UNKNOWN")

        task = asyncio.create_task(call())
        await asyncio.wait_for(entered.wait(), 10)
        await send(api, path, "/pause")
        await app.execute_workflow(eid)
        with app.db.read() as db:
            wf = db.get(WorkflowSession, wid)
            assert app.workflow_controls.current(db, wf).checkpoint_id
            assert wf.control_state == "pause_requested"
        assert app.runtime.acknowledge_pause(root, er, "parent-worker")
        release.set()
        await task
        app.process_projections()
        with app.db.read() as db:
            wf = db.get(WorkflowSession, wid)
            assert wf.control_state == ("paused" if known else "pause_requested")
            row = db.scalar(select(ModelReceipt).where(ModelReceipt.run_id == child))
            assert row.status == ("completed" if known else "unknown")
            assert db.get(AgentRun, child).calls_consumed == 1
        if not known:
            result = (await send(api, path, "/resume")).json()["action_receipts"][0]
            assert result["status"] == "failed"
            assert app.runtime.claim(child, "unsafe-retry") is None
        assert len(env["model"].calls) == 1
