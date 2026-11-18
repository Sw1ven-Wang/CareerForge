"""Goal-origin migration preserves historical content and refuses history loss."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from test_goal_amendments_v1 import amend_in, base_goal
from test_v16_migrations import migrate

from careerforge.models import GoalVersion


def downgrade(url, revision="cf166_session_graph"):
    root = Path(__file__).parents[1]
    return subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", revision],
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


def test_goal_migration_roundtrip_empty_database(tmp_path):
    url = "sqlite+pysqlite:///" + str(tmp_path / "goal-upgrade.db")
    migrate(url, "cf166_session_graph")
    engine = create_engine(url)
    assert "origin_contract" not in {
        c["name"] for c in inspect(engine).get_columns("career_goal_versions")
    }
    migrate(url, "cf166_goal_amendment")
    inspector = inspect(engine)
    assert {"origin_contract", "parent_goal_version_id", "goal_addendum_id", "version_hash"} <= {
        c["name"] for c in inspector.get_columns("career_goal_versions")
    }
    assert "ck_goal_version_origin" in {
        c["name"] for c in inspector.get_check_constraints("career_goal_versions")
    }
    assert "uq_goal_addendum_command" in {
        c["name"] for c in inspector.get_unique_constraints("workflow_goal_addenda")
    }
    assert next(c for c in inspector.get_columns("career_goal_versions") if c["name"] == "run_id")[
        "nullable"
    ]
    engine.dispose()
    result = downgrade(url)
    assert result.returncode == 0, result.stderr
    migrate(url, "cf166_goal_amendment")


def test_populated_legacy_upgrade_preserves_exact_goal_and_objects(env):
    _, _, old, before = base_goal(env)
    app = env["app"]
    with app.db.read() as db:
        row = db.get(GoalVersion, old)
        values = (row.source_text, row.content_hash, row.requirements, row.run_id)
    url = app.settings.database_url
    app.db.engine.dispose()
    result = downgrade(url)
    assert result.returncode == 0, result.stderr
    migrate(url, "cf166_goal_amendment")
    with app.db.read() as db:
        row = db.get(GoalVersion, old)
        assert (row.source_text, row.content_hash, row.requirements, row.run_id) == values
        assert row.origin_contract == "run-goal-v1"
        from careerforge.services.research_coverage import read_goal_version

        assert read_goal_version(db, env["actor"], old) == before


def test_command_history_cannot_be_downgraded(env):
    cid, wid, _, _ = base_goal(env)
    app = env["app"]
    with app.db.transaction() as db:
        amend_in(env, db, cid, wid)
    app.db.engine.dispose()
    result = downgrade(app.settings.database_url)
    assert result.returncode != 0
    assert "Retain command-origin goal history" in result.stderr


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE career_goal_versions SET run_id=NULL WHERE version=1",
        "UPDATE career_goal_versions SET origin_contract='unknown' WHERE version=1",
        "UPDATE career_goal_versions SET run_id=(SELECT run_id FROM career_goal_versions WHERE version=1) WHERE version=2",
        "UPDATE workflow_goal_addenda SET command_action_index=NULL WHERE command_receipt_id IS NOT NULL",
    ],
)
def test_origin_sql_constraints_reject_invalid_combinations(env, sql):
    cid, wid, _, _ = base_goal(env)
    app = env["app"]
    with app.db.transaction() as db:
        amend_in(env, db, cid, wid)
    with pytest.raises(IntegrityError):
        with app.db.transaction() as db:
            db.execute(text(sql))


def test_multiple_null_creation_runs_preserve_real_run_uniqueness(env):
    cid, wid, old, _ = base_goal(env)
    app = env["app"]
    with app.db.transaction() as db:
        child, _, _, _ = amend_in(env, db, cid, wid)
        amend_in(env, db, cid, child.id, technology="Rust")
        assert len(list(db.scalars(select(GoalVersion).where(GoalVersion.run_id.is_(None))))) == 2
    with pytest.raises(IntegrityError):
        with app.db.transaction() as db:
            row = db.get(GoalVersion, old)
            db.add(
                GoalVersion(
                    goal_id=row.goal_id,
                    version=4,
                    source_text=row.source_text,
                    content_hash=row.content_hash,
                    requirements=row.requirements,
                    run_id=row.run_id,
                )
            )
