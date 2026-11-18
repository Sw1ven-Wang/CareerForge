"""Knowledge graph scheduling tests with synthetic domain providers only."""

from dataclasses import replace

import pytest
from sqlalchemy import select
from support import FILES

from careerforge.contracts import ImportSourceInput, KnowledgeBuildInput
from careerforge.models import AgentRun, ModelReceipt, Publication, RunEvent


def start(env, engine="langgraph", key="knowledge-graph", fixed_source=True):
    app = env["app"]
    app.settings = replace(app.settings, workflow_engine=engine)
    app.db.settings = app.settings
    source = app.sources.import_source(
        env["actor"],
        ImportSourceInput(
            namespace_id=env["ns"],
            repository_key="fixtures/knowledge-graph",
            commit_sha="a" * 40,
            license_id="MIT",
            files=FILES,
            source_metadata={"synthetic": True},
        ),
    )
    return app.build.start(
        env["actor"],
        KnowledgeBuildInput(
            namespace_id=env["ns"],
            funding_id=env["funding"],
            question="Tool Calling 返回值示例",
            source_id=source if fixed_source else None,
            topic_keys=["agent.tool_calling"],
            call_cap=10,
            max_units_per_project=1,
            idempotency_key=key,
        ),
    )


async def test_staged_build_waits_without_finishing_and_preserves_review(env):
    app = env["app"]
    run_id = start(env)
    await app.execute(run_id)
    with app.db.read() as db:
        root = db.get(AgentRun, run_id)
        assert root.status == "waiting_children"
        assert root.worker_id is None
        assert (root.graph_version, root.state_schema_version) == ("knowledge-build-v1", 1)
        children = list(db.scalars(select(AgentRun).where(AgentRun.parent_id == run_id)))
        assert len(children) == 1 and children[0].workflow == "analysis"
        assert children[0].workflow_engine == "legacy"
    async with app.graph_runtime.checkpointers.open() as saver:
        saved = await saver.aget_tuple({"configurable": {"thread_id": run_id}})
        values = saved.checkpoint["channel_values"]
        assert "question" not in values and "sources" not in values
    await app.drain()
    result = app.runtime.stage(run_id, "result")
    assert result["publication_ids"]
    with app.db.read() as db:
        assert db.get(AgentRun, run_id).status in {"succeeded", "partially_succeeded"}
        children = list(db.scalars(select(AgentRun).where(AgentRun.parent_id == run_id)))
        assert {c.workflow for c in children} == {"analysis", "review"}
        assert all(c.workflow_engine == "legacy" for c in children)
    assert (await app.graph_runtime.summary(root))["current_stage"] == "completed"


@pytest.mark.parametrize("role", ["analyze", "review"])
async def test_cancelled_child_saved_result_never_published(env, monkeypatch, role):
    app = env["app"]
    original = getattr(app.build, role)

    async def cancelled(run, epoch, worker):
        await original(run, epoch, worker)
        with app.db.read() as db:
            revision = db.get(AgentRun, run.id).revision
        app.runtime.cancel(env["actor"], run.id, revision)

    monkeypatch.setattr(app.build, role, cancelled)
    run_id = start(env)
    await app.drain()
    assert app.runtime.stage(run_id, "result")["publication_ids"] == []
    with app.db.read() as db:
        assert db.get(AgentRun, run_id).status == "partially_succeeded"
        assert not list(db.scalars(select(Publication)))


async def test_flag_rollback_does_not_rebind_knowledge_or_duplicate_calls(env):
    app = env["app"]
    run_id = start(env)
    await app.execute(run_id)
    app.settings = replace(app.settings, workflow_engine="legacy")
    app.db.settings = app.settings
    await app.drain()
    with app.db.read() as db:
        root = db.get(AgentRun, run_id)
        assert root.workflow_engine == "langgraph"
        assert root.status in {"succeeded", "partially_succeeded"}
        receipts = list(db.scalars(select(ModelReceipt)))
    await app.execute(run_id)
    with app.db.read() as db:
        assert len(list(db.scalars(select(ModelReceipt)))) == len(receipts)


@pytest.mark.parametrize("mode", ["success", "invalid_path", "cancel"])
async def test_graph_existing_bounded_retry_contract(env, monkeypatch, mode):
    from test_review2_evidence_retry import test_same_run_repairs_only_new_frozen_windows

    app = env["app"]
    app.settings = replace(app.settings, workflow_engine="langgraph")
    app.db.settings = app.settings
    await test_same_run_repairs_only_new_frozen_windows(env, monkeypatch, mode)


@pytest.mark.parametrize(
    "case",
    [
        "test_bounded_coverage_build_discloses_unfinished_goal",
        "test_reviewed_coverage_gap_uses_second_analysis_not_a_new_agent",
        "test_first_published_scope_suffices_without_an_extra_round",
    ],
)
async def test_graph_existing_coordinator_contract(env, monkeypatch, case):
    import test_coordinator_analysis_coverage_v1 as suite

    app = env["app"]
    app.settings = replace(app.settings, workflow_engine="langgraph")
    app.db.settings = app.settings
    await getattr(suite, case)(env, monkeypatch)


def expire(app, run_id):
    from datetime import timedelta

    from careerforge.util import now

    with app.db.transaction() as db:
        db.get(AgentRun, run_id).lease_until = now() - timedelta(seconds=1)
    app.runtime.reap()


@pytest.mark.parametrize("window", ["child_committed", "checkpoint_committed", "result_committed"])
async def test_domain_checkpoint_crash_windows_reconcile_without_duplicate_work(
    env, monkeypatch, window
):
    from careerforge.errors import GraphCheckpointUnavailable

    app = env["app"]
    run_id = start(env)
    crashed = False
    if window == "checkpoint_committed":
        original = app.runtime.wait_children

        def fail(*args):
            nonlocal crashed
            if not crashed:
                crashed = True
                raise GraphCheckpointUnavailable("synthetic crash before wait projection")
            return original(*args)

        monkeypatch.setattr(app.runtime, "wait_children", fail)
    else:
        name = "ensure_analysis_child" if window == "child_committed" else "publish_and_finalize"
        original = getattr(app.build, name)

        async def fail(*args):
            nonlocal crashed
            result = await original(*args)
            if not crashed:
                crashed = True
                raise GraphCheckpointUnavailable("synthetic crash after domain commit")
            return result

        monkeypatch.setattr(app.build, name, fail)
    await app.drain()
    assert crashed
    with app.db.read() as db:
        assert db.get(AgentRun, run_id).status == "running"
        before = len(list(db.scalars(select(ModelReceipt))))
    expire(app, run_id)
    await app.drain()
    with app.db.read() as db:
        assert db.get(AgentRun, run_id).status in {"succeeded", "partially_succeeded"}
        children = list(db.scalars(select(AgentRun).where(AgentRun.parent_id == run_id)))
        assert len(children) == 2
        if window == "result_committed":
            assert len(list(db.scalars(select(ModelReceipt)))) == before
    assert app.runtime.stage(run_id, "result")["publication_ids"]


@pytest.mark.parametrize(
    "branch", ["no_sources", "analysis_empty", "analysis_failed", "review_failed"]
)
async def test_graph_terminal_gap_branches(env, monkeypatch, branch):
    from careerforge.errors import DomainError

    app = env["app"]
    if branch == "no_sources":

        async def no_sources(*args, **kwargs):
            return {"candidates": [], "incomplete": False}

        monkeypatch.setattr(app.build.research, "run", no_sources)
    else:

        async def fail_or_empty(run, epoch, worker):
            if branch == "analysis_empty":
                app.runtime.save_stage(run.id, epoch, worker, "result", {"unit_version_ids": []})
            else:
                raise DomainError("SYNTHETIC_FAILURE", "Synthetic domain failure")

        monkeypatch.setattr(
            app.build, "review" if branch == "review_failed" else "analyze", fail_or_empty
        )
    run_id = start(env, fixed_source=branch != "no_sources")
    await app.drain()
    result = app.runtime.stage(run_id, "result")
    with app.db.read() as db:
        assert result is not None, [
            (e.event_type, e.payload)
            for e in db.scalars(select(RunEvent).where(RunEvent.run_id == run_id))
        ][-1:]
    assert result["publication_ids"] == [] and result["gaps"]
    with app.db.read() as db:
        assert db.get(AgentRun, run_id).status == "partially_succeeded"


async def test_replayed_checkpoint_cannot_allocate_a_second_retry_pair_or_bill_again(
    env, monkeypatch
):
    from test_review2_evidence_retry import test_same_run_repairs_only_new_frozen_windows

    from careerforge.errors import GraphCheckpointUnavailable

    app = env["app"]
    app.settings = replace(app.settings, workflow_engine="langgraph")
    app.db.settings = app.settings
    original_finalize = app.build.publish_and_finalize
    original_drain = app.drain
    crashed = False

    async def crash_after_result(*args):
        nonlocal crashed
        result = await original_finalize(*args)
        if not crashed:
            crashed = True
            raise GraphCheckpointUnavailable("synthetic crash after result")
        return result

    async def replay_and_recover(*args, **kwargs):
        await original_drain(*args, **kwargs)
        with app.db.read() as db:
            root = db.scalar(select(AgentRun).where(AgentRun.workflow == "knowledge_build"))
            assert root.status == "running"
            children = list(db.scalars(select(AgentRun).where(AgentRun.parent_id == root.id)))
            assert len(children) == 4
            calls = len(list(db.scalars(select(ModelReceipt))))
        adapter = app.graph_runtime
        async with adapter.checkpointers.open() as saver:
            graph = adapter.graph(saver, root, root.epoch, root.worker_id)
            config = {"configurable": {"thread_id": root.id}}
            snapshot = await graph.aget_state(config)
            # Model a delayed old checkpoint restoring pre-dispatch control flow.
            # No checkpoint field may authorize another pair or another model bill.
            await graph.aupdate_state(config, snapshot.values, as_node="prepare_retry")
        expire(app, root.id)
        result = await original_drain(*args, **kwargs)
        with app.db.read() as db:
            assert len(list(db.scalars(select(AgentRun).where(AgentRun.parent_id == root.id)))) == 4
            assert len(list(db.scalars(select(ModelReceipt)))) == calls
        return result

    monkeypatch.setattr(app.build, "publish_and_finalize", crash_after_result)
    monkeypatch.setattr(app, "drain", replay_and_recover)
    await test_same_run_repairs_only_new_frozen_windows(env, monkeypatch, "success")
    assert crashed


@pytest.mark.parametrize(
    "corruption",
    [
        "empty",
        "missing",
        "removed_publications",
        "removed_reviews",
        "foreign_publication",
        "foreign_review",
    ],
)
async def test_completed_checkpoint_rejects_corrupt_domain_result(env, monkeypatch, corruption):
    from careerforge.errors import DomainError
    from careerforge.models import StageArtifact

    app = env["app"]
    run_id = start(env)
    original_finish = app.runtime.finish

    def skip_root_finish(identifier, *args, **kwargs):
        if identifier == run_id:
            return False
        return original_finish(identifier, *args, **kwargs)

    monkeypatch.setattr(app.runtime, "finish", skip_root_finish)
    await app.drain()
    with app.db.transaction() as db:
        run = db.get(AgentRun, run_id)
        assert run.status == "running"
        artifact = db.scalar(
            select(StageArtifact).where(
                StageArtifact.run_id == run_id, StageArtifact.stage_key == "result"
            )
        )
        assert artifact.payload["publication_ids"]
        if corruption == "empty":
            artifact.payload = {}
        elif corruption == "missing":
            db.delete(artifact)
        elif corruption.startswith("foreign_"):
            key = "publication_ids" if corruption == "foreign_publication" else "review_ids"
            artifact.payload = {**artifact.payload, key: ["another-run-object"]}
        else:
            key = "publication_ids" if corruption == "removed_publications" else "review_ids"
            artifact.payload = {**artifact.payload, key: []}
    with pytest.raises(DomainError) as error:
        await app.graph_runtime.execute(run, run.epoch, run.worker_id)
    assert error.value.code == "GRAPH_RECONCILIATION"


async def test_child_finishes_after_interrupt_persisted_before_wait_projection(env, monkeypatch):
    from careerforge.models import Outbox
    from careerforge.workflows.runtime_adapter import GraphRuntimeAdapter

    app = env["app"]
    run_id = start(env)
    adapter = app.graph_runtime = GraphRuntimeAdapter(app)
    raced = False
    async with adapter.checkpointers.open() as saver:
        original = saver.aput_writes

        async def finish_child_after_interrupt(config, writes, task_id, task_path=""):
            nonlocal raced
            await original(config, writes, task_id, task_path)
            if not raced and any(channel == "__interrupt__" for channel, _ in writes):
                raced = True
                child = app.runtime.stage(run_id, "analysis_child")["run_id"]
                await app.execute(child)

        monkeypatch.setattr(saver, "aput_writes", finish_child_after_interrupt)
        await app.execute(run_id)
    assert raced
    with app.db.read() as db:
        assert db.get(AgentRun, run_id).status == "queued"
        assert list(
            db.scalars(select(Outbox).where(Outbox.aggregate_id == run_id, Outbox.kind == "run"))
        )
    await app.drain()
    assert app.runtime.stage(run_id, "result")["publication_ids"]
    with app.db.read() as db:
        assert len(list(db.scalars(select(AgentRun).where(AgentRun.parent_id == run_id)))) == 2


@pytest.mark.parametrize("target", ["source", "funding"])
async def test_revocation_while_interrupted_never_publishes(env, target):
    from careerforge.models import ObjectRow

    app = env["app"]
    run_id = start(env)
    await app.execute(run_id)
    with app.db.read() as db:
        run = db.get(AgentRun, run_id)
        assert run.status == "waiting_children"
    if target == "funding":
        app.runtime.revoke_funding(env["actor"], env["funding"])
    else:
        with app.db.transaction() as db:
            db.get(ObjectRow, run.input_manifest["source_id"]).state = "revoked"
    await app.drain()
    with app.db.read() as db:
        assert db.get(AgentRun, run_id).status in {"failed", "cancelled"}
        assert not list(db.scalars(select(Publication)))


async def test_persisted_interrupt_reclaimed_before_child_finishes_commits_wait(env, monkeypatch):
    from careerforge.errors import GraphCheckpointUnavailable

    app = env["app"]
    run_id = start(env)
    original = app.runtime.wait_children

    def crash(*args):
        raise GraphCheckpointUnavailable("synthetic crash before waiting projection")

    monkeypatch.setattr(app.runtime, "wait_children", crash)
    await app.execute(run_id)
    monkeypatch.setattr(app.runtime, "wait_children", original)
    expire(app, run_id)
    await app.execute(run_id)
    with app.db.read() as db:
        assert db.get(AgentRun, run_id).status == "waiting_children"
        child = db.get(AgentRun, app.runtime.stage(run_id, "analysis_child")["run_id"])
        assert child.status == "queued"
    await app.drain()
    assert app.runtime.stage(run_id, "result")["publication_ids"]
