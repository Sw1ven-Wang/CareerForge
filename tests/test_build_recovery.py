"""A saved artifact alone is not permission to publish a cancelled child."""

import pytest
from sqlalchemy import select
from support import FILES

from careerforge.contracts import ImportSourceInput, KnowledgeBuildInput
from careerforge.models import AgentRun, Publication, SourceSet


@pytest.mark.parametrize("stage", ["analyze", "review"])
@pytest.mark.asyncio
async def test_cancelled_child_saved_result_is_not_published(env, monkeypatch, stage):
    app = env["app"]
    source = app.sources.import_source(
        env["actor"],
        ImportSourceInput(
            namespace_id=env["ns"],
            repository_key="fixtures/cancelled",
            commit_sha="a" * 40,
            license_id="MIT",
            files=FILES,
            source_metadata={"synthetic": True},
        ),
    )
    original = getattr(app.build, stage)

    async def finish_then_cancel(run, epoch, worker):
        await original(run, epoch, worker)
        assert app.runtime.stage(run.id, "result")
        with app.db.read() as db:
            revision = db.get(AgentRun, run.id).revision
        app.runtime.cancel(env["actor"], run.id, revision)

    monkeypatch.setattr(app.build, stage, finish_then_cancel)
    identifier = app.build.start(
        env["actor"],
        KnowledgeBuildInput(
            namespace_id=env["ns"],
            funding_id=env["funding"],
            question="Tool Calling 返回值示例",
            source_id=source,
            topic_keys=["agent.tool_calling"],
            call_cap=10,
            max_units_per_project=1,
            idempotency_key="cancelled-child",
        ),
    )
    await app.drain()
    result = app.runtime.stage(identifier, "result")
    assert result["publication_ids"] == []
    assert result["gaps"]
    with app.db.read() as db:
        assert not db.scalars(select(Publication)).all()
        assert db.get(AgentRun, identifier).status == "partially_succeeded"


@pytest.mark.asyncio
async def test_snapshot_return_after_cancel_does_not_import_source(env, monkeypatch):
    app = env["app"]
    original = env["repos"].snapshot

    async def cancelled_snapshot(url, run_id, request_cap):
        result = await original(url, run_id, request_cap)
        with app.db.read() as db:
            revision = db.get(AgentRun, run_id).revision
        app.runtime.cancel(env["actor"], run_id, revision)
        return result

    monkeypatch.setattr(env["repos"], "snapshot", cancelled_snapshot)
    identifier = app.build.start(
        env["actor"],
        KnowledgeBuildInput(
            namespace_id=env["ns"],
            funding_id=env["funding"],
            question="Tool Calling 返回值示例",
            repository_url="https://github.com/fixtures/cancelled",
            topic_keys=["agent.tool_calling"],
            call_cap=10,
            max_units_per_project=1,
            idempotency_key="cancelled-snapshot",
        ),
    )
    await app.drain()
    with app.db.read() as db:
        assert not db.scalars(select(SourceSet)).all()
        assert db.get(AgentRun, identifier).status == "cancelled"
