"""Actual Alembic upgrade/rollback guard and unchanged legacy caller bytes."""

from sqlalchemy import create_engine, inspect, text
from test_goal_amendment_schema import downgrade
from test_practice_graph_m5 import session, start
from test_v16_migrations import migrate

from careerforge.config import Settings
from careerforge.db import SCHEMA_REVISION


def test_migration_empty_roundtrip(tmp_path):
    url = "sqlite+pysqlite:///" + str(tmp_path / "m5.db")
    migrate(url, "cf166_learning_graph")
    engine = create_engine(url)
    assert "workflow_practice_requests" not in inspect(engine).get_table_names()
    migrate(url, "head")
    assert {"workflow_practice_requests", "workflow_assessment_requests"} <= set(
        inspect(engine).get_table_names()
    )
    with engine.connect() as db:
        assert (
            db.scalar(text("SELECT version_num FROM alembic_version"))
            == SCHEMA_REVISION
        )
    assert downgrade(url, "cf166_learning_graph").returncode == 0
    assert "workflow_practice_requests" not in inspect(engine).get_table_names()
    migrate(url, "head")


async def test_history_blocks_destructive_downgrade(env):
    conv, wid = await session(env)
    await start(env, conv, wid)
    result = downgrade(env["app"].settings.database_url, "cf166_learning_graph")
    assert result.returncode != 0 and "Practice history exists" in result.stderr
    assert "workflow_practice_requests" in inspect(env["app"].db.engine).get_table_names()


def test_defaults_do_not_enable_practice_graph():
    assert Settings().workflow_engine == "legacy"
    assert Settings().learning_graph_version is None
