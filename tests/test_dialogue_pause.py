import asyncio
import os
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from support import FixtureModel, FixtureRepositories
from test_dialogue_actions import CASES, api_client, open_dialogue, send

from careerforge.application import Application
from careerforge.config import Settings
from careerforge.contracts import ModelOutput
from careerforge.db import clock, schema_is_current
from careerforge.errors import DomainError, PauseBoundary
from careerforge.models import AgentRun, Conversation, ModelReceipt, Turn
from careerforge.models_dialogue import GoalAddendum, WorkflowSession
from careerforge.services.runtime import leased
from careerforge.services.security import create_principal


@pytest.fixture
def postgres_env(tmp_path):
    url = os.getenv("CAREERFORGE_DIALOGUE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires a disposable PostgreSQL database")
    assert "_validation_" in url.split("/")[-1]
    model = FixtureModel()
    app = Application(
        Settings(
            mode="test",
            database_url=url,
            state_dir=tmp_path,
            allow_fixture_publication=True,
            embedding_dimensions=8,
        ),
        model,
        FixtureRepositories(),
    )
    name = "dialogue-" + uuid4().hex[:10]
    if schema_is_current(app.db.engine):
        with app.db.transaction() as db:
            principal, namespace, token = create_principal(db, name)
            owner = {"principal_id": principal.id, "namespace_id": namespace.id, "token": token}
    else:
        owner = app.bootstrap(name)
    actor, ns = owner["principal_id"], owner["namespace_id"]
    funding = app.runtime.create_funding(actor, ns, 50)
    yield {"app": app, "model": model, "owner": owner, "actor": actor, "ns": ns, "funding": funding}
    app.db.engine.dispose()


async def bound_run(env, api, *, claim=True):
    app, actor = env["app"], env["actor"]
    path = await open_dialogue(api)
    draft = (await send(api, path, "我想学习 Python")).json()
    run = app.runtime.create(actor, env["ns"], env["funding"], "analysis", {}, 4, uuid4().hex)
    with app.db.transaction() as db:
        conv = db.get(Conversation, path.split("/")[-1])
        app.workflows.link_in(db, app.workflows.owned_in(db, conv), db.get(AgentRun, run), "build")
    epoch = app.runtime.claim(run, "paused-worker") if claim else None
    return path, draft["workflow_id"], run, epoch


@pytest.mark.production
@pytest.mark.asyncio
async def test_T10_T11_T13_T18_pause_split_worker_and_sse_replay(postgres_env):
    env, app = postgres_env, postgres_env["app"]
    async with api_client(env) as api:
        path, workflow, run, epoch = await bound_run(env, api)
        action = (await send(api, path, CASES["T10"]["user_message"])).json()
        assert [r["action_type"] for r in action["action_receipts"]] == [
            "request_pause",
            "switch_focus",
            "ask_clarification",
        ]
        assert action["action_receipts"][0]["control_state"] == "pause_requested"
        assert action["workflow_id"] != workflow
        status_url = path + "/workflow-status"
        assert (await api.get(status_url, params={"workflow_id": workflow})).json()[
            "control_state"
        ] == "pause_requested"
        with pytest.raises(PauseBoundary):
            with app.db.transaction() as db:
                leased(db, run, epoch, "paused-worker")
        with pytest.raises(PauseBoundary):
            app.runtime.create_child(run, epoch, "paused-worker", "prohibited", "review", {}, 1)
        with pytest.raises(PauseBoundary):
            await app.gateway.call(run, epoch, "paused-worker", "blocked", "analysis", "test", {})
        assert not env["model"].calls
        assert not app.runtime.acknowledge_pause(run, epoch, "wrong-worker")
        assert app.runtime.acknowledge_pause(run, epoch, "paused-worker")
        assert (await api.get(status_url, params={"workflow_id": workflow})).json()[
            "control_state"
        ] == "paused"
        app.process_projections()
        app.runtime.reap()
        assert app.runtime.claim(run, "cannot-claim-paused") is None
        with app.db.read() as db:
            assert db.get(AgentRun, run).status == "paused"
            assert db.get(WorkflowSession, workflow).control_state == "paused"
            assert not db.scalar(select(AgentRun).where(AgentRun.parent_id == run))
            assert not db.scalar(select(ModelReceipt).where(ModelReceipt.run_id == run))
        url = path + "/activity-events?once=true"
        original = (await api.get(url)).text
        assert "run.paused" in original
        ids = [line[4:] for line in original.splitlines() if line.startswith("id: ")]
        for _ in range(3):
            assert (await api.get(url)).text == original
            replay = await api.get(url, headers={"Last-Event-ID": ids[-2]})
            assert [
                line[4:] for line in replay.text.splitlines() if line.startswith("id: ")
            ] == ids[-1:]
        resumed = (await send(api, path, CASES["T14"]["user_message"])).json()
        assert resumed["workflow_id"] == workflow
        assert resumed["action_receipts"][0]["status"] == "applied", resumed
        assert app.runtime.claim(run, "resumed-worker") == epoch + 1


@pytest.mark.production
@pytest.mark.asyncio
@pytest.mark.parametrize("known", [True, False])
async def test_T12_pause_during_paid_request_keeps_receipt_and_does_not_retry(postgres_env, known):
    env, app = postgres_env, postgres_env["app"]
    entered, release = asyncio.Event(), asyncio.Event()

    async def provider(request):
        env["model"].calls.append(request)
        entered.set()
        await release.wait()
        if not known:
            raise DomainError("NETWORK_OUTCOME_UNKNOWN", "fixture outcome unknown")
        return ModelOutput(text="source-bound example", usage={"prompt_tokens": 1}, synthetic=True)

    env["model"].complete = provider
    async with api_client(env) as api:
        path, workflow, run, epoch = await bound_run(env, api)

        async def worker():
            try:
                return await app.gateway.call(
                    run, epoch, "paused-worker", "paid", "analysis", "test", {}
                )
            except PauseBoundary:
                assert app.runtime.acknowledge_pause(run, epoch, "paused-worker")
            except DomainError:
                assert not app.runtime.finish(
                    run, epoch, "paused-worker", "failed", "NETWORK_OUTCOME_UNKNOWN"
                )

        task = asyncio.create_task(worker())
        await asyncio.wait_for(entered.wait(), 15)
        try:
            data = (await send(api, path, CASES["T12"]["user_message"])).json()
            assert data["action_receipts"][0]["control_state"] == "pause_requested"
            assert (await api.get(path + "/workflow-status")).json()[
                "control_state"
            ] == "pause_requested"
        finally:
            release.set()
        await asyncio.wait_for(task, 15)
        with app.db.read() as db:
            row = db.get(AgentRun, run)
            receipt = db.scalar(select(ModelReceipt).where(ModelReceipt.run_id == run))
            assert row.calls_consumed == 1
            assert row.status == ("paused" if known else "pause_pending_inflight")
            assert receipt.status == ("completed" if known else "unknown")
        resume = (await send(api, path, CASES["T14"]["user_message"])).json()
        if known:
            assert resume["action_receipts"][0]["status"] == "applied"
            new_epoch = app.runtime.claim(run, "replay-worker")
            result = await app.gateway.call(
                run, new_epoch, "replay-worker", "paid", "analysis", "test", {}
            )
            assert result.text == "source-bound example"
        else:
            assert resume["action_receipts"][0]["failure_code"] == "RECEIPT_RECONCILIATION"
            app.runtime.reap()
            assert app.runtime.claim(run, "must-not-retry") is None
        assert len(env["model"].calls) == 1


@pytest.mark.production
@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["revoke", "deadline", "cancel"])
async def test_T14_T15_T16_resume_refuses_expiry_revocation_and_terminal_runs(postgres_env, stop):
    env, app = postgres_env, postgres_env["app"]
    async with api_client(env) as api:
        path, _, run, _ = await bound_run(env, api, claim=False)
        paused = await send(api, path, "暂停")
        assert paused.json()["action_receipts"][0]["status"] == "applied"
        if stop == "revoke":
            app.runtime.revoke_funding(env["actor"], env["funding"])
        elif stop == "deadline":
            with app.db.transaction() as db:
                item = db.get(AgentRun, run)
                item.deadline_at = clock(db) - timedelta(seconds=1)
                deadline = item.deadline_at
            app.runtime.reap()
        else:
            with app.db.read() as db:
                revision = db.get(AgentRun, run).revision
            app.runtime.cancel(env["actor"], run, revision)
        result = (
            await send(api, path, CASES["T16" if stop == "cancel" else "T14"]["user_message"])
        ).json()
        step = result["action_receipts"][0]
        assert (
            step["failure_code"]
            == {
                "revoke": "FUNDING_UNAVAILABLE",
                "deadline": "WAITING_REAUTHORIZATION",
                "cancel": "TERMINAL_RUN",
            }[stop]
        )
        with app.db.read() as db:
            assert db.get(AgentRun, run).status == ("paused" if stop == "deadline" else "cancelled")
            if stop == "deadline":
                assert db.get(AgentRun, run).deadline_at == deadline
        assert not env["model"].calls


@pytest.mark.production
@pytest.mark.asyncio
async def test_T07_T17_pg_focus_CAS_and_late_turn(postgres_env):
    env, app = postgres_env, postgres_env["app"]
    async with api_client(env) as api:
        path = await open_dialogue(api)
        first = (await send(api, path, "我想学习 Python")).json()
        turn = (
            await send(api, path, "解释刚才的项目代码。", authorization_ref=env["funding"])
        ).json()
        assert turn["lane"] == "answer"
        revision = (await api.get(path)).json()["revision"]
        before = env["model"].calls[:]
        replies = await asyncio.gather(
            *[send(api, path, CASES["T07"]["user_message"], revision=revision) for _ in range(2)]
        )
        assert sorted(reply.status_code for reply in replies) == [202, 409]
        assert env["model"].calls == before
        active = next(reply.json()["workflow_id"] for reply in replies if reply.status_code == 202)
        assert active != first["workflow_id"]
        await app.execute(turn["run_id"])
        status = (await api.get(path + "/workflow-status")).json()
        assert status["workflow_id"] == active
        with app.db.read() as db:
            assert db.get(Turn, turn["turn_id"]).focus["active_workflow_id"] == first["workflow_id"]
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(GoalAddendum)
                    .where(GoalAddendum.author_id == env["actor"])
                )
                == 1
            )
