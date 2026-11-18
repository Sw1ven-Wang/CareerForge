import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import publish_fixture
from sqlalchemy import create_engine, inspect, text

from careerforge.contracts import TurnInput
from careerforge.db import clock
from careerforge.models import CommandAuthorization


@pytest.mark.asyncio
async def test_dialogue_plan_proposal_requires_accept_and_observation_no_mastery(env):
    app = env["app"]
    actor = env["actor"]
    _, pub, _ = await publish_fixture(env)
    conv = app.conversations.create(actor)
    t = app.conversations.submit(
        actor,
        conv,
        TurnInput(
            question="给我Tool Calling的学习计划", expected_revision=1, idempotency_key="plan"
        ),
    )
    await app.conversations.process(t["turn_id"])
    blocks = [
        e["payload"]["content"]
        for e in app.answers.events(actor, t["turn_id"])["events"]
        if e["type"] == "answer.block.validated"
    ]
    proposal = next(b["plan_proposal"] for b in blocks if "plan_proposal" in b)
    before = app.learning.detail(actor, proposal["plan_id"])
    assert before["active_version_id"] is None
    app.learning.accept_plan(actor, proposal["plan_id"], proposal["version_id"], 1)
    module = proposal["module_ids"][0]
    observation = app.learning.observe(
        actor, module, "仍不理解返回值", {"explanation": False}, "assisted"
    )
    assert (
        observation["suggested_action"] == "reduce_step_or_prerequisite"
        and not observation["competence_verified"]
    )
    detail = app.learning.detail(actor, proposal["plan_id"])
    assert detail["active_version_id"] == proposal["version_id"]


def test_command_lost_supervisor_is_unknown_not_reexecuted(env):
    app = env["app"]
    actor = env["actor"]
    ws = app.coding.create_workspace(actor, {"main.py": "print(1)"}, [], "assisted")
    cmd = app.coding.propose_command(
        actor, ws["workspace_id"], ws["revision"], ["python", "-B", "main.py"]
    )
    app.coding.confirm_command(actor, cmd["command_id"], cmd["approval_nonce"], "RUN")
    with app.db.transaction() as db:
        row = db.get(CommandAuthorization, cmd["command_id"])
        row.state = "running"
        row.started_at = clock(db) - timedelta(minutes=3)
    assert app.coding.reap_commands() == 1
    result = app.coding.command_result(actor, cmd["command_id"])
    assert result["state"] == "unknown" and not result["receipt"]["automatic_retry"]


def test_initial_migration_new_sqlite_schema_and_legacy_refusal(tmp_path):
    root = Path(__file__).parents[1]
    url = "sqlite+pysqlite:///" + str(tmp_path / "new.db")
    env = {
        **os.environ,
        "CAREERFORGE_MODE": "test",
        "CAREERFORGE_DATABASE_URL": url,
        "PYTHONPATH": str(root / "src"),
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    engine = create_engine(url)
    assert {
        "knowledge_units",
        "policy_rollouts",
        "command_authorizations",
        "alembic_version",
    } <= set(inspect(engine).get_table_names())
    check = subprocess.run(
        [sys.executable, "-m", "alembic", "check"],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert check.returncode == 0, check.stdout + check.stderr
    engine.dispose()
    legacyurl = "sqlite+pysqlite:///" + str(tmp_path / "legacy.db")
    legacy = create_engine(legacyurl)
    with legacy.begin() as c:
        c.execute(text("CREATE TABLE study_tracks(id TEXT)"))
    env["CAREERFORGE_DATABASE_URL"] = legacyurl
    blocked = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert blocked.returncode != 0 and "Legacy CareerForge" in blocked.stderr
    assert "study_tracks" in inspect(legacy).get_table_names()
    legacy.dispose()


def test_bootstrap_also_refuses_legacy_database(tmp_path):
    import sqlite3

    from careerforge.application import Application
    from careerforge.config import Settings
    from careerforge.errors import DomainError

    path = tmp_path / "legacy-bootstrap.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE users (id TEXT PRIMARY KEY)")
    application = Application(
        Settings(mode="development", database_url="sqlite+pysqlite:///" + str(path))
    )
    with pytest.raises(DomainError, match="全新"):
        application.bootstrap()
    with sqlite3.connect(path) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {"users"}
    application.db.engine.dispose()
