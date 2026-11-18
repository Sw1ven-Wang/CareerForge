"""Real PostgreSQL saver restart at a knowledge child interrupt, synthetic domain only."""

import pytest
from test_langgraph_m2 import start
from test_langgraph_postgres_m1 import checkpoint_postgres  # noqa: F401

from careerforge.models import AgentRun
from careerforge.workflows.checkpointer import Checkpointers
from careerforge.workflows.runtime_adapter import GraphRuntimeAdapter

pytestmark = pytest.mark.production


async def test_knowledge_interrupt_survives_fresh_adapter(env, checkpoint_postgres):  # noqa: F811
    _, settings = checkpoint_postgres
    app = env["app"]
    run_id = start(env)
    adapter = app.graph_runtime = GraphRuntimeAdapter(app)
    adapter.checkpointers = Checkpointers(settings)
    await app.execute(run_id)
    with app.db.read() as db:
        run = db.get(AgentRun, run_id)
        assert run.status == "waiting_children"
    # A new saver/adapter has no in-memory graph state. Child wakeups must use
    # persisted interrupt state and the immutable knowledge-build-v1 binding.
    restarted = app.graph_runtime = GraphRuntimeAdapter(app)
    restarted.checkpointers = Checkpointers(settings)
    await app.drain()
    assert app.runtime.stage(run_id, "result")["publication_ids"]
    assert (await restarted.summary(run))["current_stage"] == "completed"
