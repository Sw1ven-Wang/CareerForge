"""Real constraints survive PG DDL compilation and safe historical rollback."""

from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from support import FixtureModel, FixtureRepositories
from test_adapters import test_postgresql_schema_compiles_without_claiming_live_db as compile_pg
from test_goal_amendment_schema import downgrade
from test_goal_amendments_v1 import amend_in, base_goal
from test_v16_migrations import migrate

from careerforge.application import Application
from careerforge.config import Settings
from careerforge.db import SCHEMA_REVISION, schema_is_current
from careerforge.models import GoalVersion
from careerforge.models_dialogue import ConversationActionReceipt, GoalAddendum, WorkflowSession
from careerforge.services.research_coverage import read_goal_version


def bootstrap(url, path):
    app = Application(
        Settings(mode="test", database_url=url, state_dir=path),
        FixtureModel(),
        FixtureRepositories(),
    )
    owner = app.bootstrap("migration-fixture-" + uuid4().hex)
    return {
        "app": app,
        "actor": owner["principal_id"],
        "ns": owner["namespace_id"],
        "funding": app.runtime.create_funding(owner["principal_id"], owner["namespace_id"], 1000),
    }


def row_state(app, table):
    with app.db.read() as db:
        return sorted(
            (dict(row) for row in db.execute(text('SELECT * FROM "' + table + '"')).mappings()),
            key=lambda row: str(row.get("id", row)),
        )


def complete_state(app):
    """Observe every actual table and row, not merely the Alembic version stamp."""
    inspector = inspect(app.db.engine)
    result = {}
    for table in inspector.get_table_names():
        result[table] = {
            "columns": [(c["name"], str(c["type"]), c["nullable"]) for c in inspector.get_columns(table)],
            "foreign_keys": sorted(inspector.get_foreign_keys(table), key=repr),
            "checks": sorted(inspector.get_check_constraints(table), key=repr),
            "uniques": sorted(inspector.get_unique_constraints(table), key=repr),
            "indexes": sorted(inspector.get_indexes(table), key=repr),
            "rows": row_state(app, table),
        }
    return deepcopy(result)


def assert_real_constraints(app, version_id, workflow_id):
    inspector = inspect(app.db.engine)
    fks = {f["name"]: f for f in inspector.get_foreign_keys("career_goal_versions")}
    assert fks["fk_goal_version_addendum"]["constrained_columns"] == ["goal_addendum_id"]
    assert fks["fk_goal_version_addendum"]["referred_table"] == "workflow_goal_addenda"
    assert fks["fk_goal_version_parent"]["referred_table"] == "career_goal_versions"
    assert "fk_workflow_current_execution" in {
        f["name"] for f in inspector.get_foreign_keys("workflow_sessions")
    }
    with pytest.raises(IntegrityError, match="(?i)foreign key"):
        with app.db.transaction() as db:
            row = db.get(GoalVersion, version_id)
            # All origin/unique/parent conditions are valid. Only the addendum
            # reference is dangling, so the database must reject its real FK.
            db.add(
                GoalVersion(
                    goal_id=row.goal_id,
                    version=2,
                    source_text=row.source_text,
                    content_hash=row.content_hash,
                    requirements=row.requirements,
                    origin_contract="command-goal-v1",
                    parent_goal_version_id=row.id,
                    goal_addendum_id="missing-addendum",
                    version_hash="a" * 64,
                )
            )
    with pytest.raises(IntegrityError, match="(?i)foreign key"):
        with app.db.transaction() as db:
            db.get(WorkflowSession, workflow_id).current_execution_id = "missing-execution"


def assert_legacy_roundtrip(env):
    app = env["app"]
    _, workflow, version, before_goal = base_goal(env)
    goals = row_state(app, "career_goal_versions")
    sessions = row_state(app, "workflow_sessions")
    assert_real_constraints(app, version, workflow)
    app.db.engine.dispose()
    result = downgrade(app.settings.database_url, "cf166_graph")
    assert result.returncode == 0, result.stderr
    inspector = inspect(app.db.engine)
    assert "origin_contract" not in {c["name"] for c in inspector.get_columns("career_goal_versions")}
    assert "orchestration_engine" not in {c["name"] for c in inspector.get_columns("workflow_sessions")}
    assert not next(c for c in inspector.get_columns("career_goal_versions") if c["name"] == "run_id")["nullable"]
    app.db.engine.dispose()
    migrate(app.settings.database_url, "head")
    assert schema_is_current(app.db.engine)
    assert row_state(app, "career_goal_versions") == goals
    assert row_state(app, "workflow_sessions") == sessions
    with app.db.read() as db:
        assert read_goal_version(db, env["actor"], version) == before_goal
        assert db.get(WorkflowSession, workflow).orchestration_engine == "legacy"
    assert_real_constraints(app, version, workflow)


def assert_history_refusal(env, kind):
    app = env["app"]
    cid, wid, version, _ = base_goal(env)
    with app.db.transaction() as db:
        if kind == "command_goal":
            amend_in(env, db, cid, wid)
        else:
            # Structural partially admitted history: receipt/addendum authority
            # must not be discarded even if no command-goal row exists yet.
            receipt = ConversationActionReceipt(
                conversation_id=cid, workflow_id=wid, idempotency_key=uuid4().hex,
                request_hash="a" * 64, action_plan_hash="b" * 64, plan={}, result={},
            )
            db.add(receipt)
            db.flush()
            db.add(GoalAddendum(
                workflow_id=wid, author_id=env["actor"], source_goal_version_id=version,
                command_receipt_id=receipt.id, command_action_index=0,
                manifest_hash="c" * 64, manifest={"fixture": "interrupted admission"},
            ))
    before = complete_state(app)
    app.db.engine.dispose()
    result = downgrade(app.settings.database_url, "cf166_graph")
    assert result.returncode != 0
    assert "Retain command-origin goal history" in result.stderr
    assert schema_is_current(app.db.engine)
    assert complete_state(app) == before
    with app.db.read() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == SCHEMA_REVISION


@pytest.mark.parametrize("initial", ["bootstrap", "migrated"])
def test_pg_compilation_then_sqlite_preserves_real_fks_and_legacy_roundtrip(tmp_path, initial):
    compile_pg()
    url = "sqlite+pysqlite:///" + str(tmp_path / "migration.db")
    if initial == "migrated":
        migrate(url, "cf166_session_graph")
        migrate(url, "head")
    env = bootstrap(url, tmp_path)
    try:
        assert_legacy_roundtrip(env)
    finally:
        env["app"].db.engine.dispose()


@pytest.mark.parametrize("kind", ["command_goal", "command_addendum_only"])
def test_sqlite_history_refusal_precedes_every_downgrade_ddl(tmp_path, kind):
    env = bootstrap("sqlite+pysqlite:///" + str(tmp_path / "history.db"), tmp_path)
    try:
        assert_history_refusal(env, kind)
    finally:
        env["app"].db.engine.dispose()
