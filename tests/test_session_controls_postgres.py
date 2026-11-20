"""Persistent M3 control interrupt survives a fresh PostgreSQL saver adapter.

The domain fixture is SQLite. Full PG-domain/Redis/Celery validation is separate.
"""

import pytest
from test_dialogue_actions import api_client, send
from test_langgraph_postgres_m1 import checkpoint_postgres  # noqa: F401
from test_session_controls_m3 import m3
from test_session_graph_m2 import prepare

from careerforge.models_dialogue import WorkflowExecution, WorkflowSession
from careerforge.workflows.checkpointer import Checkpointers
from careerforge.workflows.session_control_graph import ControlSessionGraphAdapter

pytestmark = pytest.mark.production


async def test_control_interrupt_survives_new_postgres_saver_then_resume_waits_for_approval(
    env,
    checkpoint_postgres,  # noqa: F811
):
    _, settings = checkpoint_postgres
    app = m3(env)
    async with api_client(env) as api:
        path, wid, eid = await prepare(env, api, drain=False)
        app.session_control_graph = ControlSessionGraphAdapter(app)
        app.session_control_graph.checkpointers = Checkpointers(settings)
        await app.drain()
        calls = len(env["model"].calls)
        await send(api, path, "/pause")
        await app.drain()
        with app.db.read() as db:
            workflow = db.get(WorkflowSession, wid)
            assert workflow.control_state == "paused"
            checkpoint = app.workflow_controls.current(db, workflow).checkpoint_id
            assert checkpoint
        app.session_control_graph = ControlSessionGraphAdapter(app)
        app.session_control_graph.checkpointers = Checkpointers(settings)
        await send(api, path, "/resume")
        await app.drain()
        with app.db.read() as db:
            assert db.get(WorkflowSession, wid).control_state == "active"
            assert db.get(WorkflowExecution, eid).wait_kind == "approval"
            assert not db.get(WorkflowSession, wid).target.get("accepted_plan")
        assert len(env["model"].calls) == calls
