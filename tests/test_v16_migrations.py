"""Additive checkpoint upgrade preserves authority; new bootstrap is versioned."""

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from support import FixtureModel, FixtureRepositories

from careerforge.api import create_app
from careerforge.application import Application
from careerforge.config import Settings
from careerforge.db import SCHEMA_REVISION, Database
from careerforge.errors import DomainError
from careerforge.models import Base
from careerforge.util import digest


def migrate(url, revision):
    root = Path(__file__).parents[1]
    env = {
        **os.environ,
        "CAREERFORGE_MODE": "test",
        "CAREERFORGE_DATABASE_URL": url,
        "PYTHONPATH": str(root / "src"),
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def test_cf151_upgrade_backfills_privacy_once_preserves_principal_and_schema(tmp_path):
    url = "sqlite+pysqlite:///" + str(tmp_path / "checkpoint.db")
    migrate(url, "cf151_command_binding")
    engine = create_engine(url)
    principals = Table("principals", MetaData(), autoload_with=engine)
    with engine.begin() as db:
        db.execute(
            principals.insert().values(
                id="existing-owner",
                created_at=datetime.now(UTC),
                name="preserved-owner",
                token_hash=digest("known-old-test-token"),
                is_admin=True,
                active=True,
                auth_revision=7,
            )
        )
    app = Application(
        Settings(mode="test", database_url=url, state_dir=tmp_path),
        FixtureModel(),
        FixtureRepositories(),
    )
    with pytest.raises(DomainError) as exc:
        app.db.create_schema()
    assert exc.value.code == "MIGRATION_REQUIRED"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url="http://test"
    ) as client:
        assert (await client.get("/health/ready")).status_code == 503
    migrate(url, "head")
    assert set(Base.metadata.tables) <= set(inspect(engine).get_table_names())
    state = Table("privacy_states", MetaData(), autoload_with=engine)
    with engine.begin() as db:
        row = db.execute(select(state)).mappings().one()
        assert row["generation"] == 0 and row["blocked"] is False
        assert db.scalar(select(principals.c.auth_revision)) == 7
        assert db.scalar(select(principals.c.token_hash)) == digest("known-old-test-token")
        db.execute(state.update().values(generation=9))
    migrate(url, "head")
    with engine.connect() as db:
        assert db.scalar(select(state.c.generation)) == 9
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == SCHEMA_REVISION
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url="http://test"
    ) as client:
        assert (await client.get("/health/ready")).status_code == 200
    app.db.engine.dispose()
    engine.dispose()


def test_fresh_bootstrap_has_explicit_schema_version(env):
    with env["app"].db.read() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == SCHEMA_REVISION
    env["app"].db.create_schema()  # Idempotent, never changes existing consent/generations.


def test_bootstrap_refuses_partial_existing_schema_without_mutation(tmp_path):
    db = Database(
        Settings(mode="test", database_url="sqlite+pysqlite:///" + str(tmp_path / "partial.db"))
    )
    with db.engine.begin() as conn:
        conn.execute(text("CREATE TABLE principals(id TEXT PRIMARY KEY)"))
    with pytest.raises(DomainError) as exc:
        db.create_schema()
    assert exc.value.code == "MIGRATION_REQUIRED"
    assert inspect(db.engine).get_table_names() == ["principals"]
    db.engine.dispose()


@pytest.mark.parametrize("revision", ["cf151_command_binding", "unversioned", "multiple"])
async def test_ready_and_bootstrap_refuse_untrusted_schema_stamp(env, revision):
    with env["app"].db.engine.begin() as db:
        if revision == "unversioned":
            db.execute(text("DROP TABLE alembic_version"))
        elif revision == "multiple":
            db.execute(text("INSERT INTO alembic_version(version_num) VALUES ('unknown-branch')"))
        else:
            db.execute(
                text("UPDATE alembic_version SET version_num=:revision"), {"revision": revision}
            )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(env["app"])), base_url="http://test"
    ) as client:
        assert (await client.get("/health/ready")).status_code == 503
    with pytest.raises(DomainError) as exc:
        env["app"].db.create_schema()
    assert exc.value.code == "MIGRATION_REQUIRED"
