"""Personal erasure retains only inert session graph audit tombstones."""

import pytest
from sqlalchemy import select
from test_dialogue_actions import api_client, send
from test_privacy_v16 import confirm
from test_session_graph_m2 import prepare

from careerforge.models import Outbox
from careerforge.models_dialogue import WorkflowExecution, WorkflowPlanRequest


@pytest.mark.parametrize("phase", ["active", "completed"])
async def test_erasure_covers_execution_requests_and_wakes(env, phase):
    app = env["app"]
    async with api_client(env) as api:
        path, _, execution_id = await prepare(env, api, drain=phase == "completed")
        if phase == "active":
            reply = (await send(api, path, "/plan", authorization_ref=env["funding"])).json()
            assert reply["action_receipts"][0]["status"] == "applied"
        else:
            await send(api, path, "接受学习计划")
            await app.drain()
    export = app.privacy.export(env["actor"])
    assert export["records"]["workflow_executions"]
    if phase == "active":
        assert export["records"]["workflow_plan_requests"]
    with app.db.read() as db:
        wake_ids = list(db.scalars(select(Outbox.id).where(Outbox.aggregate_id == execution_id)))
    await confirm(app.privacy, env["actor"], app.privacy.propose(env["actor"]))
    with app.db.read() as db:
        execution = db.get(WorkflowExecution, execution_id)
        assert execution.status == "cancelled" and execution.binding_payload == {}
        assert execution.worker_id is None and execution.lease_until is None
        assert execution.workflow_engine == "langgraph"
        requests = list(
            db.scalars(
                select(WorkflowPlanRequest).where(WorkflowPlanRequest.execution_id == execution_id)
            )
        )
        assert all(r.status == "rejected" and r.failure_code == "PRIVACY_ERASURE" for r in requests)
        wakes = list(db.scalars(select(Outbox).where(Outbox.id.in_(wake_ids))))
        assert wakes and all(w.delivered and w.payload == {} for w in wakes)
    await app.execute_workflow(execution_id)


async def test_session_goal_addendum_body_is_in_erasure_scope(env):
    from careerforge.models_dialogue import GoalAddendum

    app = env["app"]
    async with api_client(env) as api:
        path, _, _ = await prepare(env, api, drain=False)
        reply = (await send(api, path, "补充 LangGraph")).json()
        identifier = reply["action_receipts"][0]["goal_addendum_id"]
    export = app.privacy.export(env["actor"])
    assert export["records"]["workflow_goal_addenda"]
    await confirm(app.privacy, env["actor"], app.privacy.propose(env["actor"]))
    with app.db.read() as db:
        assert db.get(GoalAddendum, identifier).manifest == {}
