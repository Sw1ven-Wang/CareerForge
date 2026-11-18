"""Stamped v1.5 → v1.5.1 additive migration; no automatic adoption of unversioned DBs."""

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import MetaData, Table, create_engine, inspect, select
from support import FixtureModel, FixtureRepositories
from test_coding import FixtureSandbox

from careerforge.application import Application
from careerforge.config import Settings
from careerforge.errors import DomainError
from careerforge.util import digest, fingerprint, manifest


def test_existing_stamped_database_preserves_rows_but_cannot_invent_command_approval(tmp_path):
    root = Path(__file__).parents[1]
    url = "sqlite+pysqlite:///" + str(tmp_path / "previous.db")
    env = {
        **os.environ,
        "CAREERFORGE_MODE": "test",
        "CAREERFORGE_DATABASE_URL": url,
        "PYTHONPATH": str(root / "src"),
    }

    def migrate(revision):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", revision],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    migrate("cf150_initial")
    engine = create_engine(url)
    metadata = MetaData()
    tables = {
        name: Table(name, metadata, autoload_with=engine)
        for name in [
            "principals",
            "knowledge_namespaces",
            "workspaces",
            "execution_snapshots",
            "command_authorizations",
        ]
    }
    assert "authorization_digest" not in tables["command_authorizations"].c
    now = datetime.now(UTC)
    files = {"main.py": 'print("preserve me")\n'}
    code_hash = fingerprint(manifest(files))
    image = "sha256:" + "1" * 64
    with engine.begin() as db:
        db.execute(
            tables["principals"]
            .insert()
            .values(
                id="owner",
                created_at=now,
                name="migration-owner",
                token_hash=digest("old-test-token"),
                is_admin=True,
                active=True,
                auth_revision=1,
            )
        )
        db.execute(
            tables["knowledge_namespaces"]
            .insert()
            .values(
                id="private",
                created_at=now,
                kind="private",
                owner_id="owner",
                name="private",
                revision=1,
            )
        )
        db.execute(
            tables["workspaces"]
            .insert()
            .values(
                id="workspace",
                created_at=now,
                principal_id="owner",
                namespace_id="private",
                revision=1,
                files=files,
                manifest_hash=code_hash,
                assistance_mode="assisted",
                sealed=False,
            )
        )
        db.execute(
            tables["execution_snapshots"]
            .insert()
            .values(
                id="snapshot",
                created_at=now,
                workspace_id="workspace",
                workspace_revision=1,
                files=files,
                manifest_hash=code_hash,
                image_digest=image,
                environment_version="sandbox-network-none-1.5",
            )
        )
        for state in ["proposed", "queued"]:
            db.execute(
                tables["command_authorizations"]
                .insert()
                .values(
                    id=state,
                    created_at=now,
                    principal_id="owner",
                    execution_snapshot_id="snapshot",
                    argv=["python", "main.py"],
                    argv_hash=fingerprint(["python", "main.py"]),
                    nonce_hash=digest("original-nonce"),
                    expires_at=now + timedelta(minutes=10),
                    state=state,
                )
            )
    migrate("head")
    migrate("head")  # Re-running must not backfill new consent.
    assert "authorization_digest" in {
        c["name"] for c in inspect(engine).get_columns("command_authorizations")
    }
    current = Table("command_authorizations", MetaData(), autoload_with=engine)
    with engine.connect() as db:
        rows = db.execute(select(current.c.id, current.c.authorization_digest)).all()
        assert set(rows) == {("proposed", None), ("queued", None)}
        assert db.scalar(select(tables["workspaces"].c.files)) == files
    app = Application(
        Settings(mode="test", database_url=url, state_dir=tmp_path, sandbox_image=image),
        FixtureModel(),
        FixtureRepositories(),
    )
    try:
        with pytest.raises(DomainError) as error:
            app.coding.confirm_command("owner", "proposed", "original-nonce", "RUN")
        assert error.value.code == "APPROVAL_UNBOUND"
        box = FixtureSandbox()
        assert app.coding.process_commands(box) == 1 and box.calls == []
        assert app.coding.command_result("owner", "queued")["state"] == "rejected"
        # Existing content stays usable, but a new explicit authorization is required.
        fresh = app.coding.propose_command("owner", "workspace", 1, ["python", "main.py"])
        app.coding.confirm_command("owner", fresh["command_id"], fresh["approval_nonce"], "RUN")
        assert app.coding.process_commands(box) == 1 and len(box.calls) == 1
        assert app.coding.detail("owner", "workspace")["files"] == files
    finally:
        app.db.engine.dispose()
        engine.dispose()
