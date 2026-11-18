"""Real PostgreSQL checkpoint checks; SQLite domain DB and synthetic model only.

Prerequisites: explicit CAREERFORGE_TEST_GRAPH_POSTGRES_DSN PostgreSQL URI,
psycopg + langgraph-checkpoint-postgres, and CREATE SCHEMA permission on a
DISPOSABLE test database. No Celery, production domain PostgreSQL or live model
acceptance is implied. Each test owns and removes one unique checkpoint schema.
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
from dataclasses import replace
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest
import pytest_asyncio
from test_langgraph_m1 import start

from careerforge.errors import DomainError, GraphCheckpointUnavailable
from careerforge.models import AgentRun
from careerforge.workflows.checkpointer import Checkpointers
from careerforge.workflows.runtime_adapter import GraphRuntimeAdapter

pytestmark = pytest.mark.production


def _uri_options(uri, **values):
    parts = urlsplit(uri)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(values)
    return urlunsplit(parts._replace(query=urlencode(query, quote_via=quote)))


@pytest_asyncio.fixture
async def checkpoint_postgres(env):
    uri = os.getenv("CAREERFORGE_TEST_GRAPH_POSTGRES_DSN")
    if not uri:
        pytest.skip("Set CAREERFORGE_TEST_GRAPH_POSTGRES_DSN to a disposable PostgreSQL URI")
    if not uri.startswith(("postgresql://", "postgres://")):
        pytest.fail("CAREERFORGE_TEST_GRAPH_POSTGRES_DSN must be a PostgreSQL URI")
    import psycopg
    from psycopg import sql

    schema = "cf_m1_" + uuid4().hex
    connection = await psycopg.AsyncConnection.connect(uri, autocommit=True)
    await connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    isolated_uri = _uri_options(uri, options=f"-c search_path={schema}", connect_timeout="3")
    # Change only checkpoint settings. The existing fixture domain remains SQLite.
    settings = replace(env["app"].settings, mode="development", graph_checkpoint_url=isolated_uri)
    checkpoints = Checkpointers(settings)
    try:
        async with checkpoints.open() as saver:
            await saver.setup()
        yield isolated_uri, settings
    finally:
        await connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        await connection.close()


def _adapter(env, settings):
    adapter = GraphRuntimeAdapter(env["app"])
    adapter.checkpointers = Checkpointers(settings)
    return adapter


def _claimed(env):
    run_id, _, _ = start(env)
    epoch = env["app"].runtime.claim(run_id, "postgres-test-worker")
    with env["app"].db.read() as db:
        run = db.get(AgentRun, run_id)
    return run, epoch


async def test_checkpoint_survives_new_python_process(env, checkpoint_postgres):
    uri, settings = checkpoint_postgres
    run, epoch = _claimed(env)
    await _adapter(env, settings).execute(run, epoch, "postgres-test-worker")
    script = """
import asyncio, json, os
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
async def main():
    async with AsyncPostgresSaver.from_conn_string(os.environ['CF_TEST_CHILD_DSN']) as saver:
        item = await saver.aget_tuple({'configurable': {'thread_id': os.environ['CF_TEST_CHILD_RUN']}})
        values = item.checkpoint['channel_values']
        print(json.dumps({key: values.get(key) for key in ('workflow_run_id', 'current_stage', 'result_stage_key')}))
asyncio.run(main())
"""
    child_env = {**os.environ, "CF_TEST_CHILD_DSN": uri, "CF_TEST_CHILD_RUN": run.id}
    completed = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", script],
        env=child_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    # Do not echo DSNs or arbitrary driver exception messages in assertion output.
    assert completed.returncode == 0, "Isolated checkpoint reader subprocess failed"
    assert json.loads(completed.stdout) == {
        "workflow_run_id": run.id,
        "current_stage": "completed",
        "result_stage_key": "result",
    }
    assert len(env["model"].calls) == 1


async def test_business_commit_before_checkpoint_fresh_adapter_reconciles(env, checkpoint_postgres):
    _, settings = checkpoint_postgres
    run, epoch = _claimed(env)
    await env["app"].jd.execute(run, epoch, "postgres-test-worker")
    async with Checkpointers(settings).open() as saver:
        assert await saver.aget_tuple({"configurable": {"thread_id": run.id}}) is None
    await _adapter(env, settings).execute(run, epoch, "postgres-test-worker")
    assert len(env["model"].calls) == 1
    with env["app"].db.read() as db:
        env["app"].jd.validate_result(
            db, run.principal_id, run, env["app"].runtime.stage(run.id, "result")
        )


async def test_completed_checkpoint_fresh_connection_does_not_duplicate(env, checkpoint_postgres):
    _, settings = checkpoint_postgres
    run, epoch = _claimed(env)
    await _adapter(env, settings).execute(run, epoch, "postgres-test-worker")
    before = env["app"].runtime.stage(run.id, "result")
    await _adapter(env, settings).execute(run, epoch, "postgres-test-worker")
    assert env["app"].runtime.stage(run.id, "result") == before
    assert len(env["model"].calls) == 1


async def test_persistent_incompatible_checkpoint_refuses_resume(env, checkpoint_postgres):
    _, settings = checkpoint_postgres
    run, epoch = _claimed(env)
    adapter = _adapter(env, settings)
    async with adapter.checkpointers.open() as saver:
        graph = adapter.graph(saver, run, epoch, "postgres-test-worker")
        await graph.aupdate_state(
            {"configurable": {"thread_id": run.id}},
            {"workflow_run_id": run.id, "graph_version": "incompatible", "state_schema_version": 0},
        )
    with pytest.raises(DomainError) as error:
        await _adapter(env, settings).execute(run, epoch, "postgres-test-worker")
    assert error.value.code == "GRAPH_STATE"
    assert not env["model"].calls


async def test_real_checkpoint_connection_outage_is_recoverable(env, checkpoint_postgres):
    _, settings = checkpoint_postgres
    run_id, _, _ = start(env)
    adapter = env["app"].graph_runtime = _adapter(env, settings)
    # An owned bound-but-not-listening socket gives a local refused port without
    # stopping the user's PostgreSQL server or changing its configuration.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as refused:
        refused.bind(("127.0.0.1", 0))
        port = refused.getsockname()[1]
        bad_uri = f"postgresql://unused@127.0.0.1:{port}/unused?connect_timeout=1"
        adapter.checkpointers = Checkpointers(replace(settings, graph_checkpoint_url=bad_uri))
        with pytest.raises(GraphCheckpointUnavailable):
            async with adapter.checkpointers.open():
                pytest.fail("A non-listening port must not connect")
        await env["app"].execute(run_id)
    with env["app"].db.read() as db:
        run = db.get(AgentRun, run_id)
        assert run.status == "running" and not run.budget_settled
    assert not env["model"].calls
