"""Additive session graph schema, frozen identity and history-preserving rollback."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from test_v16_migrations import migrate

from careerforge.db import SCHEMA_REVISION, schema_is_current
from careerforge.models import Conversation
from careerforge.models_dialogue import WorkflowExecution, WorkflowPlanRequest, WorkflowSession


def session(env, engine="legacy"):
    app = env["app"]
    with app.db.transaction() as db:
        conv = Conversation(principal_id=env["actor"], namespace_id=env["ns"])
        db.add(conv)
        db.flush()
        row = WorkflowSession(
            conversation_id=conv.id,
            principal_id=env["actor"],
            namespace_id=env["ns"],
            orchestration_engine=engine,
        )
        db.add(row)
        db.flush()
        return row.id, conv.id


def execution(env, workflow_id, conversation_id, **kwargs):
    return WorkflowExecution(
        workflow_id=workflow_id,
        conversation_id=conversation_id,
        principal_id=env["actor"],
        namespace_id=env["ns"],
        goal_binding_hash="a" * 64,
        binding_payload={"schema": "knowledge-session-binding-v1"},
        **kwargs,
    )


def downgrade(url):
    root = Path(__file__).parents[1]
    return subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "cf166_graph"],
        cwd=root,
        env={
            **os.environ,
            "CAREERFORGE_MODE": "test",
            "CAREERFORGE_DATABASE_URL": url,
            "PYTHONPATH": str(root / "src"),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_migration_adds_complete_session_schema(tmp_path):
    url = "sqlite+pysqlite:///" + str(tmp_path / "migration.db")
    migrate(url, "cf166_graph")
    engine = create_engine(url)
    assert "workflow_executions" not in inspect(engine).get_table_names()
    migrate(url, "head")
    assert schema_is_current(engine)
    inspector = inspect(engine)
    assert {"workflow_executions", "workflow_plan_requests"} <= set(inspector.get_table_names())
    assert {"orchestration_engine", "current_execution_id"} <= {
        c["name"] for c in inspector.get_columns("workflow_sessions")
    }
    assert "fk_workflow_current_execution" in {
        f["name"] for f in inspector.get_foreign_keys("workflow_sessions")
    }
    with engine.connect() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == SCHEMA_REVISION
    engine.dispose()


def test_legacy_session_survives_roundtrip_and_stays_legacy(env):
    app = env["app"]
    identifier, _ = session(env)
    url = app.settings.database_url
    app.db.engine.dispose()
    result = downgrade(url)
    assert result.returncode == 0, result.stderr
    engine = create_engine(url)
    assert "orchestration_engine" not in {
        c["name"] for c in inspect(engine).get_columns("workflow_sessions")
    }
    migrate(url, "head")
    with app.db.read() as db:
        row = db.get(WorkflowSession, identifier)
        assert row.orchestration_engine == "legacy"
        assert row.current_execution_id is None
    assert schema_is_current(engine)
    engine.dispose()


@pytest.mark.parametrize("has_execution", [False, True])
def test_downgrade_refuses_graph_session_or_execution_history(env, has_execution):
    app = env["app"]
    identifier, conversation = session(env, "legacy" if has_execution else "langgraph")
    if has_execution:
        with app.db.transaction() as db:
            item = execution(env, identifier, conversation)
            db.add(item)
            db.flush()
            db.get(WorkflowSession, identifier).current_execution_id = item.id
    app.db.engine.dispose()
    result = downgrade(app.settings.database_url)
    assert result.returncode != 0
    assert "Retain session graph history" in result.stderr
    assert schema_is_current(app.db.engine)


def test_execution_defaults_and_generation_constraints(env):
    app = env["app"]
    identifier, conversation = session(env, "langgraph")
    with app.db.transaction() as db:
        item = execution(env, identifier, conversation)
        db.add(item)
        db.flush()
        db.get(WorkflowSession, identifier).current_execution_id = item.id
        assert (item.execution_generation, item.status, item.epoch, item.revision) == (
            1,
            "queued",
            0,
            1,
        )
        assert (item.workflow_type, item.graph_version, item.state_schema_version) == (
            "knowledge_session",
            "knowledge-session-v1",
            1,
        )
        assert item.wake_generation == item.handled_generation == 0
        assert item.lease_until is None and item.acceptance_receipt_id is None
    with pytest.raises(IntegrityError):
        with app.db.transaction() as db:
            db.add(execution(env, identifier, conversation))
    with pytest.raises(IntegrityError):
        with app.db.transaction() as db:
            db.add(
                execution(
                    env, identifier, conversation, execution_generation=2, handled_generation=1
                )
            )
    with app.db.read() as db:
        assert len(list(db.scalars(select(WorkflowExecution)))) == 1
    assert {
        "execution_id",
        "action_receipt_id",
        "action_index",
        "funding_id",
        "request_hash",
    } <= set(WorkflowPlanRequest.__table__.c.keys())
