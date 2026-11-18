import pytest
from sqlalchemy import func, select
from test_dialogue_actions import api_client, open_dialogue, send
from test_dialogue_workflow_e2e import fixed_source

from careerforge.contracts import KnowledgeBuildInput
from careerforge.models import AgentRun, Conversation
from careerforge.models_dialogue import WorkflowSession


async def prepared(env, api):
    app = env["app"]
    source = fixed_source(env)
    path = await open_dialogue(api)
    await send(api, path, "我想学习 Tool Calling")
    with app.db.transaction() as db:
        conv = db.get(Conversation, path.rsplit("/", 1)[1])
        workflow = app.workflows.owned_in(db, conv)
        run_id = app.build.start(
            env["actor"],
            KnowledgeBuildInput(
                namespace_id=env["ns"],
                funding_id=env["funding"],
                question="Tool Calling",
                source_id=source,
                topic_keys=["agent.tool_calling"],
                call_cap=20,
                max_projects=1,
                max_units_per_project=1,
                idempotency_key="retry-original",
            ),
            db=db,
        )
        run = db.get(AgentRun, run_id)
        app.workflows.link_in(db, workflow, run, "build")
        identity = (workflow.id, run.id, run.input_hash, run.revision)
    return path, source, identity


@pytest.mark.asyncio
async def test_retry_waits_for_original_terminal_and_requires_original_source(env):
    async with api_client(env) as api:
        path, source, (workflow, run, digest, _) = await prepared(env, api)
        response = (
            await send(api, path, "补证重试", source_id=source, authorization_ref=env["funding"])
        ).json()
        assert response["action_receipts"][0]["failure_code"] == "RESEARCH_RETRY_STATE"
        with env["app"].db.read() as db:
            assert db.get(AgentRun, run).input_hash == digest
            assert db.scalar(select(func.count()).select_from(WorkflowSession)) == 1


@pytest.mark.asyncio
async def test_retry_creates_child_without_reviving_or_mutating_original_and_replays_once(env):
    app = env["app"]
    async with api_client(env) as api:
        path, source, (parent, run, digest, revision) = await prepared(env, api)
        app.runtime.cancel(env["actor"], run, revision)
        before = (await api.get(path)).json()["revision"]
        payload = {
            "text": "补证重试",
            "source_id": source,
            "auto_evidence_retry": True,
            "build_call_cap": 48,
            "authorization_ref": env["funding"],
            "expected_focus_revision": before,
            "idempotency_key": "retry-once",
        }
        first = (await api.post(path + "/messages", json=payload)).json()
        assert first["action_receipts"][0]["status"] == "applied", first
        assert (await api.post(path + "/messages", json=payload)).json() == first
        with app.db.read() as db:
            child = db.get(WorkflowSession, first["workflow_id"])
            old = db.get(WorkflowSession, parent)
            assert child.parent_workflow_id == old.id
            assert child.goal_version_id == old.goal_version_id
            assert child.source_jd_revision_id == old.source_jd_revision_id
            assert db.get(AgentRun, run).status == "cancelled"
            assert db.get(AgentRun, run).input_hash == digest
            assert db.scalar(select(func.count()).select_from(AgentRun)) == 2
            fresh = db.get(AgentRun, first["run_refs"][0])
            assert fresh.input_manifest["source_id"] == source
            assert fresh.input_manifest["auto_evidence_retry"] is True


@pytest.mark.asyncio
async def test_retry_rejects_amended_scope_before_creating_child(env):
    app = env["app"]
    async with api_client(env) as api:
        path, source, (parent, run, _, revision) = await prepared(env, api)
        app.runtime.cancel(env["actor"], run, revision)
        with app.db.transaction() as db:
            workflow = db.get(WorkflowSession, parent)
            workflow.target = {**workflow.target, "goal_addendum_id": "prior-workflow-amendment"}
        response = (
            await send(api, path, "补证重试", source_id=source, authorization_ref=env["funding"])
        ).json()
        assert response["action_receipts"][0]["failure_code"] == "RESEARCH_RETRY_SCOPE"
        with app.db.read() as db:
            assert db.scalar(select(func.count()).select_from(WorkflowSession)) == 1
            assert db.scalar(select(func.count()).select_from(AgentRun)) == 1
