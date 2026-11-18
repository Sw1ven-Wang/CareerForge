"""Opt-in real PostgreSQL migration roundtrip in disposable private schemas."""

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from test_m3_migration_compatibility import (
    assert_history_refusal,
    assert_legacy_roundtrip,
    bootstrap,
)
from test_v16_migrations import migrate

pytestmark = pytest.mark.production


@pytest.fixture
def migration_env(tmp_path):
    url = os.getenv("CAREERFORGE_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("requires explicit disposable PostgreSQL test database")
    parsed = make_url(url)
    assert parsed.get_backend_name() == "postgresql"
    assert "_validation_" in (parsed.database or ""), "refusing non-validation database"
    schema = "m3_validation_" + uuid4().hex
    admin = create_engine(parsed)
    with admin.begin() as db:
        db.execute(text('CREATE SCHEMA "' + schema + '"'))
    isolated = parsed.update_query_dict({"options": "-csearch_path=" + schema + ",public"})
    value = None
    try:
        # Actual Alembic DDL, not metadata compilation. Separate schema prevents
        # changing any other test's tables or migration stamp in this database.
        migrate(isolated.render_as_string(hide_password=False), "cf166_session_graph")
        migrate(isolated.render_as_string(hide_password=False), "head")
        value = bootstrap(isolated.render_as_string(hide_password=False), tmp_path)
        yield value
    finally:
        if value:
            value["app"].db.engine.dispose()
        with admin.begin() as db:
            db.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        admin.dispose()


def test_pg_populated_legacy_roundtrip_retains_data_and_foreign_keys(migration_env):
    assert_legacy_roundtrip(migration_env)


@pytest.mark.parametrize("kind", ["command_goal", "command_addendum_only"])
def test_pg_history_refusal_retains_all_schema_rows_and_stamp(migration_env, kind):
    assert_history_refusal(migration_env, kind)
