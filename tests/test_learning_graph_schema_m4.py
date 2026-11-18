"""Actual additive DDL and rollback behavior, not model metadata alone."""

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from test_goal_amendment_schema import downgrade
from test_learning_graph_admission_m4 import direct
from test_m3_migration_compatibility import complete_state, row_state
from test_session_graph_schema import session
from test_v16_migrations import migrate

from careerforge.db import SCHEMA_REVISION
from careerforge.models_dialogue import WorkflowExecution, WorkflowTutorRequest


def test_learning_migration_empty_roundtrip_and_real_constraints(tmp_path):
    url = "sqlite+pysqlite:///" + str(tmp_path / "learning-ddl.db")
    migrate(url, "cf166_workflow_controls")
    engine = create_engine(url)
    assert "workflow_tutor_requests" not in inspect(engine).get_table_names()
    migrate(url, "head")
    inspector = inspect(engine)
    assert {"fk_learning_predecessor", "fk_learning_admission"} <= {
        f["name"] for f in inspector.get_foreign_keys("workflow_executions")
    }
    assert {"ck_tutor_request_paid", "ck_tutor_request_authority"} <= {
        c["name"] for c in inspector.get_check_constraints("workflow_tutor_requests")
    }
    result = downgrade(url, "cf166_workflow_controls")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "workflow_tutor_requests" not in inspect(engine).get_table_names()
    migrate(url, "head")
    with engine.connect() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == SCHEMA_REVISION


def test_learning_upgrade_does_not_enroll_or_rewrite_existing_legacy_session(env):
    app = env["app"]
    session(env, engine="legacy")
    before = row_state(app, "workflow_sessions")
    url = app.settings.database_url
    result = downgrade(url, "cf166_workflow_controls")
    assert result.returncode == 0, result.stdout + result.stderr
    migrate(url, "head")
    assert row_state(app, "workflow_sessions") == before
    assert before[0]["learning_graph_version"] is None
    assert not row_state(app, "workflow_tutor_requests")


@pytest.mark.asyncio
async def test_learning_retained_history_blocks_downgrade_before_any_ddl_and_paired_refs(env):
    app = env["app"]
    _, _, _, result = await direct(env, funding=False)
    request_id = result["action_receipts"][0]["request_id"]
    with app.db.read() as db:
        request = db.get(WorkflowTutorRequest, request_id)
        execution_id = request.execution_id
    with pytest.raises(IntegrityError):
        with app.db.transaction() as db:
            row = db.get(WorkflowExecution, execution_id)
            row.admission_action_index = None
    with pytest.raises(IntegrityError):
        with app.db.transaction() as db:
            row = db.get(WorkflowTutorRequest, request_id)
            row.authorization_action_index = 0
    before = complete_state(app)
    result = downgrade(app.settings.database_url, "cf166_workflow_controls")
    assert result.returncode != 0 and "Retained learning history" in result.stderr
    assert complete_state(app) == before
