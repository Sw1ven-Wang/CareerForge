"""Restore the delivery snapshot into an isolated local development instance."""

import json
import secrets
import sqlite3
from pathlib import Path

from sqlalchemy import select

from careerforge.config import Settings
from careerforge.db import Database, schema_is_current
from careerforge.models import Namespace, Principal
from careerforge.util import digest


def main():
    settings = Settings.from_env()
    state = settings.state_dir.resolve()
    source = Path(__file__).resolve().parents[2] / "evidence" / "session-final.sqlite"
    target = state / "development.db"
    client = state / "client.json"
    state.mkdir(parents=True, exist_ok=True)
    if client.exists():
        if not target.exists():
            raise RuntimeError("Client exists but runtime database is missing; restore manually.")
        print("Existing local instance retained.")
        return
    if target.exists():
        raise RuntimeError("Runtime database already exists without a client; refusing overwrite.")
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as original:
        with sqlite3.connect(target) as copy:
            original.backup(copy)
            if copy.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("Snapshot copy failed SQLite quick_check.")
    database = Database(settings)
    if not schema_is_current(database.engine):
        raise RuntimeError("Snapshot requires an explicit schema migration.")
    token = secrets.token_urlsafe(48)
    with database.transaction() as session:
        owners = list(session.scalars(select(Principal).where(Principal.active.is_(True))))
        if len(owners) != 1:
            raise RuntimeError("Expected exactly one delivery owner; choose the owner manually.")
        owner = owners[0]
        owner.token_hash = digest(token)
        owner.auth_revision += 1
        private = session.scalar(select(Namespace).where(
            Namespace.owner_id == owner.id, Namespace.kind == "private"
        ))
        public = session.scalar(select(Namespace).where(Namespace.kind == "shared_public"))
        config = {
            "principal_id": owner.id,
            "namespace_id": private.id,
            "public_namespace_id": public.id,
            "token": token,
            "api_url": settings.api_url,
            "development": True,
        }
    client.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Delivery copied; local login saved privately. Original snapshot unchanged.")


if __name__ == "__main__":
    main()
