"""Offline graph-engine engineering checks. No real provider/network calls."""

from dataclasses import replace

import pytest
from sqlalchemy import func, select
from test_dialogue_actions import api_client
from test_jd_lineage_v166 import configure_extractor

from careerforge.config import Settings
from careerforge.contracts_jd_v166 import JDExtractionInput, JDImportInput
from careerforge.errors import DomainError
from careerforge.models import AgentRun, ModelReceipt, ObjectRow, RunEvent
from careerforge.workflows.checkpointer import Checkpointers
from careerforge.workflows.runtime_adapter import GraphRuntimeAdapter


def start(env, engine="langgraph", key="graph-jd"):
    app = env["app"]
    app.settings = replace(app.settings, workflow_engine=engine)
    app.db.settings = app.settings
    configure_extractor(env)
    doc = app.jd.import_jd(
        env["actor"], JDImportInput(namespace_id=env["ns"], jd_id=key, raw_text="熟悉队列。")
    )
    body = JDExtractionInput(
        namespace_id=env["ns"],
        funding_id=env["funding"],
        jd_revision_id=doc["jd_revision_id"],
        idempotency_key=key,
    )
    run_id = app.jd.start_extraction(env["actor"], body)["run_id"]
    return run_id, body, doc


async def test_real_graph_calls_unchanged_domain_once_and_redacts_checkpoint(env):
    app = env["app"]
    run_id, body, doc = start(env)
    await app.drain()
    with app.db.read() as db:
        run = db.get(AgentRun, run_id)
        assert (run.status, run.workflow_engine, run.graph_version) == (
            "succeeded",
            "langgraph",
            "jd-extract-v1",
        )
        assert run.calls_consumed == 1
        assert db.scalar(select(func.count()).select_from(ModelReceipt)) == 1
        events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id)))
        assert sum(e.event_type == "workflow.stage.started" for e in events) == 1
        assert sum(e.event_type == "workflow.stage.completed" for e in events) == 1
    assert app.jd.start_extraction(env["actor"], body)["run_id"] == run_id
    await app.execute(run_id)
    assert len(env["model"].calls) == 1
    async with app.graph_runtime.checkpointers.open() as saver:
        saved = await saver.aget_tuple({"configurable": {"thread_id": run_id}})
    channels = saved.checkpoint["channel_values"]
    assert "熟悉队列" not in str(channels)
    assert channels["result_stage_key"] == "result"
    assert "requirements" not in channels
    async with api_client(env) as api:
        response = await api.get(f"/runs/{run_id}/graph")
        assert response.status_code == 200
        assert response.json()["current_stage"] == "completed"


async def test_flag_is_per_new_run_and_old_run_never_switches(env):
    app = env["app"]
    legacy, body, _ = start(env, "legacy")
    app.settings = replace(app.settings, workflow_engine="langgraph")
    app.db.settings = app.settings
    assert app.jd.start_extraction(env["actor"], body)["run_id"] == legacy
    await app.drain()
    with app.db.read() as db:
        run = db.get(AgentRun, legacy)
        assert run.workflow_engine == "legacy" and run.graph_version is None
    assert not hasattr(app, "graph_runtime")


async def test_graph_run_remains_graph_after_flag_rollback(env):
    app = env["app"]
    run_id, _, _ = start(env)
    app.settings = replace(app.settings, workflow_engine="legacy")
    app.db.settings = app.settings
    await app.drain()
    with app.db.read() as db:
        assert db.get(AgentRun, run_id).status == "succeeded"
    assert app.graph_runtime is not None


async def test_business_commit_without_checkpoint_reconciles_without_model_replay(env):
    app = env["app"]
    run_id, _, _ = start(env)
    epoch = app.runtime.claim(run_id, "worker")
    with app.db.read() as db:
        run = db.get(AgentRun, run_id)
    await app.jd.execute(run, epoch, "worker")
    assert len(env["model"].calls) == 1
    # Fresh adapter/saver models lost execution history, not a persistent restart claim.
    adapter = GraphRuntimeAdapter(app)
    await adapter.execute(run, epoch, "worker")
    await adapter.execute(run, epoch, "worker")
    assert len(env["model"].calls) == 1


async def test_revoked_jd_stops_before_any_graph_model_call(env):
    app = env["app"]
    run_id, _, doc = start(env)
    with app.db.transaction() as db:
        db.get(ObjectRow, doc["jd_revision_id"]).state = "revoked"
    await app.drain()
    assert not env["model"].calls
    with app.db.read() as db:
        assert db.get(AgentRun, run_id).status == "failed"


async def test_unknown_graph_version_fails_closed(env):
    app = env["app"]
    run_id, _, _ = start(env)
    with app.db.transaction() as db:
        db.get(AgentRun, run_id).graph_version = "future-v2"
    await app.drain()
    assert not env["model"].calls
    with app.db.read() as db:
        assert db.get(AgentRun, run_id).status == "failed"


async def test_corrupt_checkpoint_never_defaults_new_schema(env):
    app = env["app"]
    run_id, _, _ = start(env)
    epoch = app.runtime.claim(run_id, "worker")
    with app.db.read() as db:
        run = db.get(AgentRun, run_id)
    adapter = GraphRuntimeAdapter(app)
    async with adapter.checkpointers.open() as saver:
        graph = adapter.graph(saver, run, epoch, "worker")
        await graph.aupdate_state(
            {"configurable": {"thread_id": run_id}},
            {"workflow_run_id": run_id, "graph_version": "old", "state_schema_version": 0},
        )
    with pytest.raises(DomainError, match="Checkpoint"):
        await adapter.execute(run, epoch, "worker")
    assert not env["model"].calls


async def test_non_test_saver_never_silently_falls_back_to_memory():
    saver = Checkpointers(Settings(mode="development"))
    with pytest.raises(DomainError):
        async with saver.open():
            pytest.fail("must fail")
    assert await saver.health() == {"status": "unavailable"}


@pytest.mark.parametrize("engine", ["unknown", "LangGraph"])
def test_invalid_engine_rejected(engine):
    with pytest.raises(DomainError):
        Settings(mode="test", workflow_engine=engine).validate()


def test_graph_production_requires_postgres_checkpoint_config():
    with pytest.raises(DomainError):
        Settings(workflow_engine="langgraph").validate()


async def test_graph_checkpoint_route_enforces_owner(env):
    run_id, _, _ = start(env)
    from careerforge.services.security import create_principal

    with env["app"].db.transaction() as db:
        _, _, token = create_principal(db, "other")
    async with api_client(env) as api:
        response = await api.get(
            f"/runs/{run_id}/graph", headers={"Authorization": "Bearer " + token}
        )
    assert response.status_code in {403, 404}


@pytest.mark.parametrize("completed_checkpoint", [False, True])
async def test_corrupt_business_result_cannot_be_reconciled(env, completed_checkpoint):
    from careerforge.models import StageArtifact

    app = env["app"]
    run_id, _, _ = start(env)
    epoch = app.runtime.claim(run_id, "worker")
    with app.db.read() as db:
        run = db.get(AgentRun, run_id)
    adapter = GraphRuntimeAdapter(app)
    if completed_checkpoint:
        await adapter.execute(run, epoch, "worker")
    else:
        await app.jd.execute(run, epoch, "worker")
    with app.db.transaction() as db:
        result = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == run_id, StageArtifact.stage_key == "result"
            )
        )
        result.payload = {}
    with pytest.raises(DomainError):
        await adapter.execute(run, epoch, "worker")
    assert len(env["model"].calls) == 1


async def test_existing_api_starts_graph_and_threads_are_isolated(env):
    app = env["app"]
    first, body, _ = start(env)
    # A distinct JD creates a distinct graph thread even for the same owner.
    _, second_body, _ = start(env, key="second")
    async with api_client(env) as api:
        response = await api.post("/jds/extractions", json=second_body.model_dump(mode="json"))
        assert response.status_code == 202
        second = response.json()["run_id"]
    await app.drain()
    assert first != second
    async with app.graph_runtime.checkpointers.open() as saver:
        for run_id in (first, second):
            checkpoint = await saver.aget_tuple({"configurable": {"thread_id": run_id}})
            assert checkpoint.checkpoint["channel_values"]["workflow_run_id"] == run_id
    assert len(env["model"].calls) == 2


async def test_cancelled_graph_never_calls_provider(env):
    app = env["app"]
    run_id, _, _ = start(env)
    with app.db.read() as db:
        revision = db.get(AgentRun, run_id).revision
    app.runtime.cancel(env["actor"], run_id, revision)
    await app.execute(run_id)
    assert not env["model"].calls


async def test_pause_boundary_after_domain_commit_resumes_without_duplicate(env):
    from careerforge.errors import PauseBoundary

    app = env["app"]
    run_id, _, _ = start(env)
    epoch = app.runtime.claim(run_id, "worker")
    with app.db.read() as db:
        run = db.get(AgentRun, run_id)
    original = app.jd.execute

    async def pause_after_commit(*args):
        await original(*args)
        with app.db.transaction() as db:
            db.get(AgentRun, run_id).pause_requested = True

    app.jd.execute = pause_after_commit
    adapter = GraphRuntimeAdapter(app)
    with pytest.raises(PauseBoundary):
        await adapter.execute(run, epoch, "worker")
    with app.db.transaction() as db:
        db.get(AgentRun, run_id).pause_requested = False
    app.jd.execute = original
    await adapter.execute(run, epoch, "worker")
    assert len(env["model"].calls) == 1
    with app.db.read() as db:
        events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id)))
        assert sum(e.event_type == "workflow.stage.started" for e in events) == 1


async def test_checkpoint_transport_loss_keeps_run_recoverable_without_rebilling(env):
    from datetime import timedelta

    from careerforge.db import clock
    from careerforge.errors import GraphCheckpointUnavailable

    app = env["app"]
    run_id, _, _ = start(env)
    adapter = app.graph_runtime = GraphRuntimeAdapter(app)
    original = adapter.execute

    async def lose_checkpoint(run, epoch, worker):
        await app.jd.execute(run, epoch, worker)
        raise GraphCheckpointUnavailable("synthetic transport fault after business commit")

    adapter.execute = lose_checkpoint
    await app.execute(run_id)
    with app.db.transaction() as db:
        run = db.get(AgentRun, run_id)
        assert run.status == "running" and not run.budget_settled
        run.lease_until = clock(db) - timedelta(seconds=1)
    assert app.runtime.reap() == 1
    adapter.execute = original
    await app.execute(run_id)
    with app.db.read() as db:
        assert db.get(AgentRun, run_id).status == "succeeded"
    assert len(env["model"].calls) == 1


async def test_summary_rejects_incompatible_checkpoint(env):
    app = env["app"]
    run_id, _, _ = start(env)
    epoch = app.runtime.claim(run_id, "worker")
    with app.db.read() as db:
        run = db.get(AgentRun, run_id)
    adapter = GraphRuntimeAdapter(app)
    async with adapter.checkpointers.open() as saver:
        graph = adapter.graph(saver, run, epoch, "worker")
        await graph.aupdate_state(
            {"configurable": {"thread_id": run_id}},
            {
                "workflow_run_id": "foreign-thread",
                "graph_version": "jd-extract-v1",
                "state_schema_version": 1,
            },
        )
    with pytest.raises(DomainError):
        await adapter.summary(run)


async def test_graph_requires_synchronous_checkpoint_durability(env, monkeypatch):
    original = GraphRuntimeAdapter.graph
    observed = []

    def instrument(self, *args, **kwargs):
        graph = original(self, *args, **kwargs)
        invoke = graph.ainvoke

        async def invoke_checked(*call_args, **call_kwargs):
            observed.append(call_kwargs.get("durability"))
            return await invoke(*call_args, **call_kwargs)

        graph.ainvoke = invoke_checked
        return graph

    monkeypatch.setattr(GraphRuntimeAdapter, "graph", instrument)
    run_id, _, _ = start(env)
    await env["app"].drain()
    assert observed == ["sync"]
    with env["app"].db.read() as db:
        assert db.get(AgentRun, run_id).status == "succeeded"
