"""Real PostgreSQL domain + PostgreSQL saver; no native model/API claims.

Requires disposable *_validation_* domain and explicit graph DSN. The parent
persistence worker owns PostgreSQL/Redis/Celery lifetime and exact source freeze.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_langgraph_postgres_m1 import checkpoint_postgres  # noqa: F401
from test_learning_graph_admission_m4 import direct
from test_m3_migration_postgresql import migration_env  # noqa: F401
from test_runtime_postgresql import pg_runtime  # noqa: F401

from careerforge.contracts_conversation_actions_v3 import LearningDialogueMessageV1
from careerforge.models_dialogue import WorkflowExecution, WorkflowTutorRequest
from careerforge.workflows.checkpointer import Checkpointers
from careerforge.workflows.learning_session import LearningSessionGraphAdapter
from careerforge.workflows.runtime_adapter import GraphRuntimeAdapter

pytestmark = pytest.mark.production


def domain_env(runtime):
    app, _, _, _ = runtime
    app.settings = replace(app.settings, allow_fixture_publication=True)
    app.db.settings = app.settings
    owner = app.bootstrap("m4-pg-" + uuid4().hex)
    funding = app.runtime.create_funding(owner["principal_id"], owner["namespace_id"], 1000)
    return {
        "app": app,
        "owner": owner,
        "actor": owner["principal_id"],
        "ns": owner["namespace_id"],
        "funding": funding,
        "model": app.provider,
        "repos": app.repositories,
    }


async def test_pg_domain_saver_restart_and_exact_concurrent_followup(
    pg_runtime,  # noqa: F811
    checkpoint_postgres,  # noqa: F811
):
    env = domain_env(pg_runtime)
    app = env["app"]
    conv, _, _, response = await direct(env)
    _, saver_settings = checkpoint_postgres
    app.learning_session_graph = LearningSessionGraphAdapter(app)
    app.learning_session_graph.checkpointers = Checkpointers(saver_settings)
    app.graph_runtime = GraphRuntimeAdapter(app)
    app.graph_runtime.checkpointers = Checkpointers(saver_settings)
    await app.drain()
    with app.db.read() as db:
        initial = db.get(WorkflowTutorRequest, response["action_receipts"][0]["request_id"])
        assert initial.status == "completed"
        identity = initial.execution_id
    app.learning_session_graph = LearningSessionGraphAdapter(app)
    app.learning_session_graph.checkpointers = Checkpointers(saver_settings)
    app.graph_runtime = GraphRuntimeAdapter(app)
    app.graph_runtime.checkpointers = Checkpointers(saver_settings)
    revision = app.conversations.detail(env["actor"], conv)["revision"]
    message = LearningDialogueMessageV1(
        text="该函数返回什么 value？",
        expected_focus_revision=revision,
        idempotency_key="pg-concurrent-followup",
        authorization_ref=env["funding"],
    )
    barrier = Barrier(2)

    def send(_):
        barrier.wait(timeout=10)
        return app.dialogue.dispatch(env["actor"], conv, message)

    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(send, range(2)))
    assert replies[0] == replies[1]
    await app.drain()
    with app.db.read() as db:
        requests = list(
            db.scalars(
                select(WorkflowTutorRequest).where(WorkflowTutorRequest.execution_id == identity)
            )
        )
        assert len(requests) == 2 and all(r.status == "completed" for r in requests)
        assert len({r.run_id for r in requests}) == 2
        assert db.get(WorkflowExecution, identity).wait_kind == "followup"


def test_pg_learning_legacy_migration_is_not_an_implicit_admission(migration_env):  # noqa: F811
    from test_learning_graph_schema_m4 import (
        test_learning_upgrade_does_not_enroll_or_rewrite_existing_legacy_session as assert_roundtrip,
    )

    assert_roundtrip(migration_env)


async def test_pg_learning_request_constraints_and_retained_history(migration_env):  # noqa: F811
    from test_learning_graph_schema_m4 import (
        test_learning_retained_history_blocks_downgrade_before_any_ddl_and_paired_refs as assert_retained,
    )

    app = migration_env["app"]
    app.settings = replace(app.settings, allow_fixture_publication=True)
    app.db.settings = app.settings
    migration_env.update(model=app.provider, repos=app.repositories)
    await assert_retained(migration_env)
