"""Read-only, owner-bounded exports from the inspected v1.2.1 database contract.

No historical Python code is imported. Credentials, sessions and payment tables
are not read. The export is an unreviewed archive, not a semantic migration.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import (
    LargeBinary,
    MetaData,
    Table,
    Text,
    cast,
    create_engine,
    func,
    inspect,
    or_,
    select,
    text,
)
from sqlalchemy.engine import make_url

from .contracts_compat import LEGACY_BASELINE, LEGACY_TABLES, LegacyBundle
from .errors import DomainError
from .util import canonical, fingerprint


def _json_value(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise DomainError("LEGACY_VALUE_TYPE", "历史资料包含不支持的非JSON字段；没有截断导出。", 422)


def export_legacy(database_url: str, owner_id: str) -> LegacyBundle:
    """Return a complete bounded selection, or fail without an output file."""
    owner = UUID(owner_id)
    if str(owner) != owner_id:
        raise DomainError("LEGACY_OWNER", "历史owner必须为规范UUID。", 422)
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite":
        if not url.database or url.database == ":memory:" or url.database.startswith("file:"):
            raise DomainError("LEGACY_DATABASE", "只读导出需要已存在的SQLite文件。", 422)
        path = Path(url.database).resolve(strict=True)
        # URI mode=ro refuses missing files and forbids writes at the driver level.
        url = url.set(
            drivername="sqlite+pysqlite",
            database="file:" + str(path),
            query={"mode": "ro", "uri": "true"},
        )
    elif url.get_backend_name() == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    else:
        raise DomainError("LEGACY_DATABASE", "仅支持只读PostgreSQL或SQLite历史库。", 422)
    engine = create_engine(url, pool_pre_ping=True)
    records: list[dict[str, Any]] = []
    total_bytes = 0
    try:
        with engine.connect() as connection, connection.begin():
            if engine.dialect.name == "postgresql":
                connection.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
                connection.execute(text("SET LOCAL statement_timeout = '30s'"))
            else:
                connection.execute(text("PRAGMA query_only = ON"))
                # sqlite3 legacy transaction mode otherwise does not BEGIN for
                # SELECT; pin one snapshot through byte preflight and export.
                connection.exec_driver_sql("BEGIN")
            names = set(inspect(connection).get_table_names())
            if not {"users", "study_tracks"} <= names:
                raise DomainError(
                    "LEGACY_DATABASE", "数据库不符合已核对的历史结构；拒绝猜测。", 422
                )
            for name in sorted(LEGACY_TABLES & names):
                table = Table(name, MetaData(), autoload_with=connection, resolve_fks=False)
                identifier = "id" if "id" in table.c else "unit_id"
                if "owner_id" not in table.c or identifier not in table.c:
                    raise DomainError("LEGACY_SCHEMA", "历史表缺少明确owner或记录身份。", 422)
                if {"password_hash", "token_hash", "csrf_token", "api_key"} & set(table.c.keys()):
                    raise DomainError("LEGACY_SCHEMA", "内容表出现凭据字段，拒绝读取。", 422)
                # SQLite's historical UUID storage may be either CHAR(32) or
                # canonical text. PostgreSQL native UUID uses the canonical bind.
                owner_clause = table.c.owner_id == owner_id
                if engine.dialect.name == "sqlite":
                    owner_clause = or_(owner_clause, table.c.owner_id == owner.hex)
                cell_lengths = [
                    func.coalesce(
                        func.length(cast(column, LargeBinary))
                        if engine.dialect.name == "sqlite"
                        else func.octet_length(cast(column, Text)),
                        0,
                    )
                    for column in table.columns
                ]
                raw_size = sum(cell_lengths)
                row_count, byte_count = connection.execute(
                    select(func.count(), func.coalesce(func.sum(raw_size), 0))
                    .select_from(table)
                    .where(owner_clause)
                ).one()
                if row_count + len(records) > 10000 or byte_count + total_bytes > 7_990_000:
                    raise DomainError(
                        "LEGACY_EXPORT_LIMIT",
                        "导出超过10000条或8MB；数据库端预检已拒绝读取正文。",
                        413,
                    )
                rows = connection.execute(
                    select(table)
                    .where(owner_clause)
                    .order_by(table.c[identifier])
                    .limit(10001 - len(records))
                    .execution_options(stream_results=True, yield_per=128, max_row_buffer=128)
                )
                for row in rows.mappings():
                    data = {key: _json_value(value) for key, value in row.items()}
                    for key, value in list(data.items()):
                        if isinstance(value, str) and (
                            key in {"id", "unit_id"} or key.endswith("_id")
                        ):
                            try:
                                data[key] = str(UUID(value))
                            except ValueError:
                                pass
                    record = {
                        "table_name": name,
                        "legacy_id": data[identifier],
                        "data": data,
                        "content_hash": fingerprint(data),
                    }
                    # Reject NaN/Infinity and unsupported values, not nonstandard JSON.
                    json.dumps(record, allow_nan=False)
                    total_bytes += len(canonical(record).encode("utf-8"))
                    records.append(record)
                    if len(records) > 10000 or total_bytes > 7_990_000:
                        raise DomainError(
                            "LEGACY_EXPORT_LIMIT", "导出超过10000条或8MB；没有截断导出。", 413
                        )
    finally:
        engine.dispose()
    manifest = {
        "format": "careerforge_legacy_export_v1",
        "baseline_sha": LEGACY_BASELINE,
        "owner_id": owner_id,
        "records": records,
    }
    return LegacyBundle.model_validate({**manifest, "bundle_hash": fingerprint(manifest)})


def write_export(bundle: LegacyBundle, output: Path) -> None:
    """Create a private file atomically with O_EXCL; never follow/replace an old file."""
    payload = bundle.model_dump_json(indent=2).encode("utf-8")
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        output.unlink(missing_ok=True)
        raise
