"""Real incremental DDL and retained-history preflight for the M6 schema."""

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from test_evolution_graph_m6 import candidate_session
from test_goal_amendment_schema import downgrade
from test_m3_migration_compatibility import complete_state
from test_v16_migrations import migrate

from careerforge.db import SCHEMA_REVISION
from careerforge.models_dialogue import WorkflowEvolutionRequest


def test_m6_empty_schema_roundtrip_and_actual_constraints(tmp_path):
    url = "sqlite+pysqlite:///" + str(tmp_path / "evolution-ddl.db")
    migrate(url, "cf166_practice_graph")
    engine = create_engine(url)
    assert "workflow_evolution_requests" not in inspect(engine).get_table_names()
    migrate(url, "head")
    inspector = inspect(engine)
    assert {
        "ck_evolution_request_kind",
        "ck_evolution_request_authority",
        "ck_evolution_request_sequence",
        "ck_evolution_request_status",
    } <= {c["name"] for c in inspector.get_check_constraints("workflow_evolution_requests")}
    assert len(inspector.get_foreign_keys("workflow_evolution_requests")) == 9
    result = downgrade(url, "cf166_practice_graph")
    assert result.returncode == 0, result.stderr
    assert "workflow_evolution_requests" not in inspect(engine).get_table_names()
    migrate(url, "head")
    with engine.connect() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == SCHEMA_REVISION
    engine.dispose()


@pytest.mark.asyncio
async def test_m6_retained_history_blocks_downgrade_before_ddl_and_paired_authority(env):
    app = env["app"]
    _, _, _, result = await candidate_session(env, funding=False)
    with pytest.raises(IntegrityError):
        with app.db.transaction() as db:
            db.get(WorkflowEvolutionRequest, result["request_id"]).funding_id = env["funding"]
    before = complete_state(app)
    result = downgrade(app.settings.database_url, "cf166_graph")
    assert result.returncode != 0 and "Retained evolution history" in result.stderr
    assert complete_state(app) == before
