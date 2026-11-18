"""PostgreSQL saver restart at a real session approval interrupt; synthetic domain."""

import pytest
from test_dialogue_actions import api_client, send
from test_langgraph_postgres_m1 import checkpoint_postgres  # noqa: F401
from test_session_graph_m2 import prepare

from careerforge.models_dialogue import WorkflowExecution
from careerforge.workflows.checkpointer import Checkpointers
from careerforge.workflows.session_graph import SessionGraphAdapter

pytestmark = pytest.mark.production


async def test_session_approval_survives_postgres_adapter_restart(env, checkpoint_postgres):  # noqa: F811
    _, settings = checkpoint_postgres
    app = env["app"]
    async with api_client(env) as api:
        path, _, execution_id = await prepare(env, api, drain=False)
        adapter = app.session_graph = SessionGraphAdapter(app)
        adapter.checkpointers = Checkpointers(settings)
        await app.drain()
        with app.db.read() as db:
            execution = db.get(WorkflowExecution, execution_id)
            assert (execution.status, execution.wait_kind) == ("waiting", "approval")
        restarted = app.session_graph = SessionGraphAdapter(app)
        restarted.checkpointers = Checkpointers(settings)
        assert (await restarted.summary(execution_id))["checkpoint_exists"]
        approved = (await send(api, path, "接受学习计划")).json()
        assert approved["action_receipts"][0]["status"] == "applied"
        await app.drain()
        with app.db.read() as db:
            assert db.get(WorkflowExecution, execution_id).status == "completed"
