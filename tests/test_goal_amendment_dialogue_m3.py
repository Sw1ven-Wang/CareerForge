"""V2 conversation admission preserves old goals and does not grant spending."""

from copy import deepcopy
from dataclasses import replace

import pytest
from sqlalchemy import func, select
from test_dialogue_actions import api_client, send
from test_dialogue_workflow_e2e import fixed_source
from test_goal_amendments_v1 import base_goal
from test_learning_loop_v16 import configure_planner

from careerforge.contracts_conversation_actions_v1 import DialogueMessage
from careerforge.contracts_learning_ext import PlanTextReference
from careerforge.errors import DomainError
from careerforge.models import (
    AgentRun,
    CareerGoal,
    Conversation,
    GoalVersion,
    ModelReceipt,
    Publication,
)
from careerforge.models_dialogue import (
    GoalAddendum,
    WorkflowExecution,
    WorkflowSession,
)
from careerforge.services.learning_projection import (
    EVIDENCE_KINDS,
    GOAL_KINDS,
    resolve_reference,
    text_catalog,
)
from careerforge.services.research_coverage import read_goal_version


def setup_goal(env):
    app = env["app"]
    app.settings = replace(
        app.settings, workflow_engine="langgraph", session_graph_version="knowledge-session-v2"
    )
    app.db.settings = app.settings
    configure_planner(env)
    cid, wid, gid, snapshot = base_goal(env)
    return app, "/conversations/" + cid, wid, gid, snapshot


async def test_exact_request_creates_v2_no_fake_runs_preserves_raw_source_and_replays(env):
    app, path, wid, gid, snapshot = setup_goal(env)
    async with api_client(env) as api:
        revision = (await api.get(path)).json()["revision"]
        raw = " \r\n增加 LangGraph 技术栈，要求掌握。\r\n"
        first = (await send(api, path, raw, key="exact-example", revision=revision)).json()
        step = first["action_receipts"][0]
        assert step["status"] == "applied", first
        v2, child = step["new_goal_version_id"], first["workflow_id"]
        assert v2 != gid and child != wid and step["execution_id"] is None
        assert (await send(api, path, raw, key="exact-example", revision=revision)).json() == first
        with app.db.read() as db:
            assert read_goal_version(db, env["actor"], gid) == snapshot
            goal = read_goal_version(db, env["actor"], v2)
            objective = goal["user_learning_objectives"][0]
            assert objective["technology"] == "LangGraph"
            assert (objective["target_depth"], objective["learning_priority"]) == (
                "掌握",
                "required",
            )
            assert objective["source"]["text"] == raw
            assert db.get(GoalVersion, v2).run_id is None
            assert db.scalar(select(func.count()).select_from(AgentRun)) == 1
            assert not db.scalar(select(ModelReceipt.id))
            assert not db.scalar(select(WorkflowExecution.id))
            assert not db.scalar(select(Publication.id))
        assert not env["model"].calls
        conflict = await send(
            api, path, "增加 LangGraph 技术栈，要求精通。", key="exact-example", revision=revision
        )
        assert conflict.status_code == 409 and "IDEMPOTENCY_CONFLICT" in conflict.text


async def test_structured_source_actual_fields_and_cas(env):
    app, path, wid, gid, _ = setup_goal(env)
    with app.db.read() as db:
        revision = db.get(WorkflowSession, wid).revision
        goal_revision = db.get(CareerGoal, db.get(GoalVersion, gid).goal_id).revision
    body = dict(
        source_goal_version_id=gid,
        expected_goal_revision=goal_revision,
        expected_revision=revision,
        idempotency_key="typed-add",
        topic="LangGraph",
        target_depth=None,
        learning_priority="unspecified",
    )
    async with api_client(env) as api:
        result = (await api.post(f"/workflows/{wid}/goal-amendments", json=body)).json()
        step = result["action_receipts"][0]
        assert step["status"] == "applied", result
        with app.db.read() as db:
            source = read_goal_version(db, env["actor"], step["new_goal_version_id"])[
                "user_learning_objectives"
            ][0]["source"]
            assert source["kind"] == "goal_revision_fields"
            assert source["fields"]["topic"] == "LangGraph"
            assert "text" not in source
        assert (await api.post(f"/workflows/{wid}/goal-amendments", json=body)).json() == result
        missing = dict(body)
        missing.pop("expected_goal_revision")
        assert (
            await api.post(f"/workflows/{wid}/goal-amendments", json=missing)
        ).status_code == 422


async def test_compound_add_then_build_reads_materialized_receipt_and_v2_scope(env):
    app, path, wid, gid, old_snapshot = setup_goal(env)
    source = fixed_source(env)
    with app.db.read() as db:
        revision = db.get(Conversation, path.split("/")[-1]).revision
    result = app.dialogue.dispatch(
        env["actor"],
        path.split("/")[-1],
        DialogueMessage(
            text="补充 agent.tool_calling",
            expected_focus_revision=revision,
            idempotency_key="compound-add-build",
            authorization_ref=env["funding"],
            source_id=source,
        ),
        forced_actions=[
            ("add_learning_topic", {"target": "agent.tool_calling"}),
            ("start_build", {}),
        ],
    )
    assert [item["status"] for item in result["action_receipts"]] == ["applied", "applied"], result
    v2 = result["action_receipts"][0]["new_goal_version_id"]
    execution_id = result["action_receipts"][1]["execution_id"]
    with app.db.read() as db:
        execution = db.get(WorkflowExecution, execution_id)
        scope = execution.binding_payload["admitted_scope"]["goal_build_scope"]
        assert execution.goal_version_id == v2 and scope["employer_requirement_ids"] == []
        assert execution.graph_version == "knowledge-session-v2"
        assert read_goal_version(db, env["actor"], gid) == old_snapshot
        build = db.get(AgentRun, result["run_refs"][0])
        assert build.input_manifest["goal_version_id"] == v2
        assert build.input_manifest["goal_build_scope"] == scope
        build_id = build.id
    await app.drain()
    context = app.runtime.stage(build_id, "research_context")
    assert context["research_task"]["requirements"] == []
    assert context["research_task"]["unresolved_requirement_ids"] == []
    with app.db.read() as db:
        workflow = db.get(WorkflowSession, result["workflow_id"])
        execution = db.get(WorkflowExecution, execution_id)
        assert execution.wait_kind == "approval", (
            execution.status,
            execution.last_error_code,
            workflow.target,
        )
        proposal = workflow.target["proposal"]
        plan = db.get(AgentRun, proposal["run_id"])
        frozen = deepcopy(plan.input_manifest)
        assert frozen["goal"]["goal_version_id"] == v2
        assert frozen["goal_amendment_plan"]["scope_hash"] == scope["scope_hash"]
        assert frozen["text_catalog_version"] == "learning-text-catalog-goal-amendment-v1"
        assert workflow.target["coverage"] != "complete"
    catalog = text_catalog(frozen)
    objective = next(
        value for value in catalog.values() if value["kind"] == "user_learning_objective"
    )
    ref = PlanTextReference(text_id=objective["text_id"], quote=objective["text"])
    selected = resolve_reference(ref, catalog, kinds=GOAL_KINDS)
    assert selected["origin"] == "user_learning_objective" and not selected["reviewed_source"]
    with pytest.raises(DomainError):
        resolve_reference(ref, catalog, kinds=EVIDENCE_KINDS)


async def test_historical_goal_revisions_route_retains_addendum_only_contract(env):
    app, path, wid, gid, _ = setup_goal(env)
    with app.db.read() as db:
        revision = db.get(WorkflowSession, wid).revision
    body = dict(
        expected_revision=revision,
        idempotency_key="legacy-route",
        source_goal_version_id=gid,
        topic="LangGraph",
    )
    async with api_client(env) as api:
        result = (await api.post(f"/workflows/{wid}/goal-revisions", json=body)).json()
        step = result["action_receipts"][0]
        assert step["status"] == "applied", result
        assert "new_goal_version_id" not in step
        with app.db.read() as db:
            child = db.get(WorkflowSession, result["workflow_id"])
            assert child.goal_version_id == gid
            addendum = db.get(GoalAddendum, child.target["goal_addendum_id"])
            assert addendum.manifest["version"] == "goal-addendum-v1"
            assert addendum.command_receipt_id is None
            assert db.scalar(select(func.count()).select_from(GoalVersion)) == 1
        assert (await api.post(f"/workflows/{wid}/goal-revisions", json=body)).json() == result


async def test_zero_root_reuse_replay_and_v2_v3_approval_isolation(env):
    app, path, _, v1, before = setup_goal(env)
    source = fixed_source(env)
    async with api_client(env) as api:
        added = (await send(api, path, "补充 agent.tool_calling")).json()
        v2 = added["action_receipts"][0]["new_goal_version_id"]
        old_workflow = added["workflow_id"]
        build = (
            await send(api, path, "/build", source_id=source, authorization_ref=env["funding"])
        ).json()
        assert build["action_receipts"][0]["status"] == "applied", build
        await app.drain()
        with app.db.read() as db:
            prior = db.get(WorkflowSession, old_workflow)
            old_proposal = deepcopy(prior.target["proposal"])
            original_publications = list(db.scalars(select(Publication.id)))
        added_again = (await send(api, path, "补充 agent.tool_calling")).json()
        assert added_again["action_receipts"][0]["status"] == "applied", added_again
        v3 = added_again["action_receipts"][0]["new_goal_version_id"]
        assert v3 != v2
        newest = added_again["workflow_id"]
        reuse = (
            await send(api, path, "/build", source_id=source, authorization_ref=env["funding"])
        ).json()
        assert reuse["action_receipts"][0]["status"] == "applied", reuse
        assert reuse["run_refs"] == []
        eid = reuse["action_receipts"][0]["execution_id"]
        calls = len(env["model"].calls)
        with app.db.read() as db:
            rows = list(db.scalars(select(AgentRun.id)))
            binding = deepcopy(db.get(WorkflowExecution, eid).binding_payload)
        replay = (await send(api, path, "/build")).json()
        assert replay["action_receipts"][0]["execution_id"] == eid, replay
        assert len(env["model"].calls) == calls
        with app.db.read() as db:
            assert list(db.scalars(select(AgentRun.id))) == rows
            assert db.get(WorkflowExecution, eid).binding_payload == binding
            assert list(db.scalars(select(Publication.id))) == original_publications
            assert read_goal_version(db, env["actor"], v1) == before
        other = fixed_source(env, "wrong-reuse-scope", "b" * 40)
        changed = (
            await send(api, path, "/build", source_id=other, authorization_ref=env["funding"])
        ).json()
        assert changed["action_receipts"][0]["failure_code"] == "WORKFLOW_GOAL_SCOPE", changed
        await app.drain()
        with app.db.read() as db:
            current = db.get(WorkflowSession, newest)
            revision = current.revision
            assert current.target["proposal"]["version_id"] != old_proposal["version_id"]
            assert db.get(WorkflowSession, old_workflow).control_state == "paused"
        stale = await api.post(
            f"/workflows/{newest}/confirmations/{old_proposal['version_id']}",
            json={"expected_revision": revision, "idempotency_key": "wrong-plan"},
        )
        assert stale.status_code == 409 and "CONFIRMATION_STALE" in stale.text
        with app.db.read() as db:
            assert not db.get(WorkflowSession, newest).target.get("accepted_plan")


async def test_legacy_structured_revision_cannot_create_unusable_child_of_v2(env):
    app, path, _, _, _ = setup_goal(env)
    async with api_client(env) as api:
        result = (await send(api, path, "补充 LangGraph")).json()
        wid = result["workflow_id"]
        with app.db.read() as db:
            workflow = db.get(WorkflowSession, wid)
            body = dict(
                expected_revision=workflow.revision,
                source_goal_version_id=workflow.goal_version_id,
                idempotency_key="legacy-on-v2",
                topic="Rust",
            )
            count = db.scalar(select(func.count()).select_from(WorkflowSession))
        rejected = (await api.post(f"/workflows/{wid}/goal-revisions", json=body)).json()
        assert rejected["action_receipts"][0]["failure_code"] == "GOAL_REVISION_CONTRACT", rejected
        with app.db.read() as db:
            assert db.scalar(select(func.count()).select_from(WorkflowSession)) == count
