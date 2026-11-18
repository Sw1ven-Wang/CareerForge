"""G01: actual read-only legacy export + private quarantine, not old-client parity."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import event, func, select
from typer.testing import CliRunner

from careerforge.api import create_app
from careerforge.cli.main import app as cli
from careerforge.compat_export import export_legacy, write_export
from careerforge.contracts_compat import LegacyBundle, LegacyImportInput, LegacySourceInput
from careerforge.errors import DomainError
from careerforge.models import FundingAgreement, ModelReceipt, Publication
from careerforge.models_compat import LegacyImportRecord
from careerforge.services.security import create_principal
from careerforge.util import digest, fingerprint


def historical_db(path: Path):
    owner, foreign, repository, snapshot, file_id = [str(uuid4()) for _ in range(5)]
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE users(id TEXT, password_hash TEXT);
            CREATE TABLE study_tracks(id TEXT PRIMARY KEY, owner_id TEXT, name TEXT, payload JSON);
            CREATE TABLE repositories(id TEXT PRIMARY KEY, owner_id TEXT, github_url TEXT, revoked_at TEXT);
            CREATE TABLE repository_snapshots(id TEXT PRIMARY KEY, owner_id TEXT, repository_id TEXT, commit_sha TEXT, manifest_hash TEXT, payload JSON);
            CREATE TABLE repository_files(id TEXT PRIMARY KEY, owner_id TEXT, snapshot_id TEXT, path TEXT, content TEXT, content_hash TEXT);
        """)
        db.execute("INSERT INTO users VALUES(?,?)", (owner, "DO_NOT_EXPORT_SECRET_HASH"))
        db.executemany(
            "INSERT INTO study_tracks VALUES(?,?,?,?)",
            [
                (str(uuid4()), owner, "Owned historical course", json.dumps({"legacy": True})),
                (str(uuid4()), foreign, "Foreign private course", "{}"),
            ],
        )
        db.execute(
            "INSERT INTO repositories VALUES(?,?,?,NULL)",
            (repository, owner, "https://github.com/example/old-project"),
        )
        db.execute(
            "INSERT INTO repository_snapshots VALUES(?,?,?,?,?,?)",
            (snapshot, owner, repository, "a" * 40, "declared-not-trusted", "{}"),
        )
        body = "def add(a, b):\n    return a + b\n"
        db.execute(
            "INSERT INTO repository_files VALUES(?,?,?,?,?,?)",
            (file_id, owner, snapshot, "main.py", body, digest(body)),
        )
    return owner, foreign


def exported(tmp_path):
    path = tmp_path / "old.db"
    owner, foreign = historical_db(path)
    return export_legacy("sqlite+pysqlite:///" + str(path), owner), foreign


def test_read_only_export_owner_hashes_no_credentials(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    owner, foreign = historical_db(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    import careerforge.compat_export as module

    original = module.create_engine
    writes_refused = []

    def checked_engine(*args, **kwargs):
        engine = original(*args, **kwargs)

        @event.listens_for(engine, "connect")
        def readonly(connection, _record):
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                connection.execute("CREATE TABLE MUST_NOT_EXIST(value TEXT)")
            writes_refused.append(True)

        return engine

    monkeypatch.setattr(module, "create_engine", checked_engine)
    bundle = export_legacy("sqlite+pysqlite:///" + str(path), owner)
    assert writes_refused and len(bundle.records) == 4
    assert all(r.data["owner_id"] == owner for r in bundle.records)
    assert foreign not in bundle.model_dump_json()
    assert "DO_NOT_EXPORT_SECRET_HASH" not in bundle.model_dump_json()
    assert all(r.content_hash == fingerprint(r.data) for r in bundle.records)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    output = tmp_path / "private-export.json"
    write_export(bundle, output)
    assert output.stat().st_mode & 0o777 == 0o600
    assert LegacyBundle.model_validate_json(output.read_text()) == bundle
    with pytest.raises(FileExistsError):
        write_export(bundle, output)


def test_legacy_bundle_rejects_tamper_mixed_owner_credentials(tmp_path):
    bundle, foreign = exported(tmp_path)
    for change in ("body", "owner", "credentials", "duplicate"):
        value = bundle.model_dump(mode="json")
        if change == "body":
            value["records"][0]["data"]["injected"] = "not in original hash"
        elif change == "owner":
            value["records"][0]["data"]["owner_id"] = foreign
            value["records"][0]["content_hash"] = fingerprint(value["records"][0]["data"])
        elif change == "credentials":
            value["records"][0]["data"]["api_key"] = "not allowed"
            value["records"][0]["content_hash"] = fingerprint(value["records"][0]["data"])
        else:
            value["records"].append(value["records"][0])
        value["bundle_hash"] = fingerprint({k: v for k, v in value.items() if k != "bundle_hash"})
        with pytest.raises(ValidationError):
            LegacyBundle.model_validate(value)


def test_private_archive_idempotent_then_explicit_unreviewed_source(env, tmp_path):
    bundle, _ = exported(tmp_path)
    app, actor = env["app"], env["actor"]
    body = LegacyImportInput(namespace_id=env["ns"], bundle=bundle, confirm="IMPORT_UNREVIEWED")
    result = app.legacy.import_bundle(actor, body)
    assert app.legacy.import_bundle(actor, body) == result
    assert result["published_knowledge_created"] == 0 and result["old_sessions_imported"] is False
    page = app.legacy.records(actor, result["batch_id"], limit=2)
    assert len(page["records"]) == 2 and page["next_cursor"]
    second = app.legacy.records(actor, result["batch_id"], page["next_cursor"], 2)
    assert len(second["records"]) == 2 and second["next_cursor"] is None
    records = page["records"] + second["records"]
    snapshot = next(r for r in records if r["legacy_table"] == "repository_snapshots")
    request = LegacySourceInput(
        snapshot_record_id=snapshot["record_id"],
        license_id="MIT",
        rights_confirmed=False,
        confirm="IMPORT_SOURCE_NOT_KNOWLEDGE",
    )
    source = app.legacy.import_source(actor, result["batch_id"], request)
    assert source["knowledge_status"] == "requires_new_analysis_and_review"
    assert app.legacy.import_source(actor, result["batch_id"], request) == source
    with pytest.raises(DomainError, match="改变"):
        app.legacy.import_source(
            actor, result["batch_id"], request.model_copy(update={"rights_confirmed": True})
        )
    with app.db.read() as db:
        actual = app.sources.get(db, actor, source["source_id"])
        assert actual.commit_sha == "a" * 40 and "main.py" in actual.files
        assert db.scalar(select(func.count()).select_from(Publication)) == 0
        assert db.scalar(select(func.count()).select_from(ModelReceipt)) == 0
        assert db.scalar(select(func.count()).select_from(FundingAgreement)) == 1


def test_archive_acl_scope_and_private_only(env, tmp_path):
    bundle, _ = exported(tmp_path)
    app, actor = env["app"], env["actor"]
    body = LegacyImportInput(namespace_id=env["ns"], bundle=bundle, confirm="IMPORT_UNREVIEWED")
    result = app.legacy.import_bundle(actor, body)
    with app.db.transaction() as db:
        other, _, _ = create_principal(db, "archive-other")
        stranger = other.id
    with pytest.raises(DomainError):
        app.legacy.records(stranger, result["batch_id"])
    with pytest.raises(DomainError):
        app.legacy.import_bundle(stranger, body)
    with pytest.raises(DomainError, match="私有"):
        app.legacy.import_bundle(
            actor, body.model_copy(update={"namespace_id": env["owner"]["public_namespace_id"]})
        )
    with app.db.transaction() as db:
        record = db.scalar(
            select(LegacyImportRecord).where(LegacyImportRecord.batch_id == result["batch_id"])
        )
        record.payload = {"changed": True}
    with pytest.raises(DomainError, match="校验"):
        app.legacy.records(actor, result["batch_id"])


async def test_archive_http_and_cli_explicit_export(env, tmp_path, monkeypatch):
    path = tmp_path / "legacy-cli.db"
    owner, _ = historical_db(path)
    output = tmp_path / "export.json"
    monkeypatch.setenv("CAREERFORGE_LEGACY_DATABASE_URL", "sqlite+pysqlite:///" + str(path))
    runner = CliRunner()
    args = ["legacy-export", owner, str(output)]
    denied = runner.invoke(cli, args)
    assert denied.exit_code != 0 and not output.exists()
    exported_result = runner.invoke(cli, [*args, "--confirm-read-only-export"])
    assert exported_result.exit_code == 0, exported_result.output
    bundle = json.loads(output.read_text())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(env["app"])),
        base_url="http://test",
        headers={"Authorization": "Bearer " + env["owner"]["token"]},
    ) as client:
        response = await client.post(
            "/api/v1/compat/legacy/imports",
            json={"namespace_id": env["ns"], "bundle": bundle, "confirm": "IMPORT_UNREVIEWED"},
        )
        assert response.status_code == 201, response.text
        page = await client.get(
            "/api/v1/compat/legacy/imports/" + response.json()["batch_id"] + "/records"
        )
        assert page.status_code == 200 and len(page.json()["records"]) == 4


def test_export_rejects_unrelated_or_missing_database(tmp_path):
    path = tmp_path / "different.db"
    with sqlite3.connect(path):
        pass
    with pytest.raises(DomainError, match="结构"):
        export_legacy("sqlite+pysqlite:///" + str(path), str(uuid4()))
    with pytest.raises(FileNotFoundError):
        export_legacy("sqlite+pysqlite:///" + str(tmp_path / "missing.db"), str(uuid4()))


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_legacy_rejects_nested_nonfinite_json_before_hash_acceptance(tmp_path, bad):
    bundle, _ = exported(tmp_path)
    value = bundle.model_dump(mode="json")
    value["records"][0]["data"]["nested"] = {"bad": [bad]}
    value["records"][0]["content_hash"] = fingerprint(value["records"][0]["data"])
    value["bundle_hash"] = fingerprint({k: v for k, v in value.items() if k != "bundle_hash"})
    with pytest.raises(ValidationError, match="finite JSON"):
        LegacyBundle.model_validate(value)


def test_legacy_large_database_row_is_rejected_before_selecting_body(tmp_path, monkeypatch):
    import careerforge.compat_export as module

    path = tmp_path / "large.db"
    owner, _ = historical_db(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE repository_files SET content = ?", ("x" * 9_000_000,))
    statements = []
    original = module.create_engine

    def checked_engine(*args, **kwargs):
        engine = original(*args, **kwargs)

        @event.listens_for(engine, "before_cursor_execute")
        def collect(_connection, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        return engine

    monkeypatch.setattr(module, "create_engine", checked_engine)
    with pytest.raises(DomainError) as exc:
        export_legacy("sqlite+pysqlite:///" + str(path), owner)
    assert exc.value.code == "LEGACY_EXPORT_LIMIT"
    assert any("sum(" in sql and "repository_files" in sql for sql in statements)
    assert not any(sql.lstrip().startswith("SELECT repository_files.id") for sql in statements)
