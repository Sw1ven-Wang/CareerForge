from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from threading import RLock
from typing import Iterator, TypedDict

from sqlalchemy import create_engine, event, func, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import Pool, StaticPool

from .config import Settings
from .errors import DomainError
from .lease_heartbeat import check_heartbeat_stop
from .models import Base, Principal

SCHEMA_REVISION = "cf166_evolution_graph"


def schema_is_current(engine) -> bool:
    inspector = inspect(engine)
    if not {"alembic_version", *Base.metadata.tables} <= set(inspector.get_table_names()):
        return False
    if not all(
        set(table.columns.keys()) <= {column["name"] for column in inspector.get_columns(name)}
        for name, table in Base.metadata.tables.items()
    ):
        return False
    if "version_num" not in {c["name"] for c in inspector.get_columns("alembic_version")}:
        return False
    with Session(bind=engine) as db:
        return list(db.scalars(text("SELECT version_num FROM alembic_version"))) == [
            SCHEMA_REVISION
        ]


class _SQLiteConnectArgs(TypedDict):
    check_same_thread: bool
    timeout: int


class _EngineOptions(TypedDict, total=False):
    pool_pre_ping: bool
    connect_args: _SQLiteConnectArgs
    poolclass: type[Pool]


class Database:
    def __init__(self, settings: Settings):
        settings.validate()
        sqlite = settings.database_url.startswith("sqlite")
        options: _EngineOptions = {"pool_pre_ping": True}
        if sqlite:
            options["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if ":memory:" in settings.database_url:
                options["poolclass"] = StaticPool
        self.engine = create_engine(settings.database_url, **options)
        if sqlite:

            @event.listens_for(self.engine, "connect")
            def on_connect(connection, record):
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=30000")
                connection.execute("PRAGMA journal_mode=WAL")

        self.factory = sessionmaker(self.engine, expire_on_commit=False)
        self.settings = settings
        # StaticPool shares ONE DBAPI connection. Separate Sessions alone are
        # insufficient when a lease keeper runs concurrently with its owner.
        self._connection_lock = RLock() if isinstance(self.engine.pool, StaticPool) else None

    def create_schema(self) -> None:
        # bootstrap must be as conservative as Alembic: never attach a new schema to legacy data.
        tables = set(inspect(self.engine).get_table_names())
        if tables & {"users", "study_tracks", "study_artifacts"}:
            raise DomainError(
                "LEGACY_DATABASE", "请使用全新数据库；历史资料只能显式只读导出后导入隔离归档。", 409
            )
        if tables and not schema_is_current(self.engine):
            raise DomainError(
                "MIGRATION_REQUIRED",
                "已有数据库必须先完成显式版本迁移；bootstrap不会补造安全状态。",
                409,
            )
        if self.engine.dialect.name == "postgresql":
            with self.engine.begin() as conn:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        Base.metadata.create_all(self.engine)
        if not tables:
            # Fresh bootstrap is stamped only after its complete schema exists.
            # Existing/unversioned databases are never silently adopted.
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
                    )
                )
                conn.execute(
                    text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                    {"revision": SCHEMA_REVISION},
                )
        if self.engine.dialect.name == "postgresql":
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_embeddings_fts ON knowledge_search_documents USING gin(to_tsvector('simple', search_text))"
                    )
                )

    @contextmanager
    def _session_connection(self) -> Iterator[None]:
        if self._connection_lock is None:
            yield
            return
        # A keeper waiting behind its owner's in-memory transaction must be
        # stoppable even if that owner exits its lease scope before the Session.
        while not self._connection_lock.acquire(timeout=0.1):
            check_heartbeat_stop()
        try:
            check_heartbeat_stop()
            yield
        finally:
            self._connection_lock.release()

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        with self._session_connection(), self.factory() as session:
            try:
                if self.engine.dialect.name == "sqlite":
                    session.execute(text("BEGIN IMMEDIATE"))
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise

    @contextmanager
    def read(self) -> Iterator[Session]:
        with self._session_connection(), self.factory() as session:
            yield session


def clock(session: Session) -> datetime:
    if session.get_bind().dialect.name == "postgresql":
        timestamp = session.scalar(select(func.clock_timestamp()))
        if not isinstance(timestamp, datetime):
            # A missing production clock must not fall back to host time and
            # accidentally authorize an expired lease or funding agreement.
            raise DomainError("DATABASE_CLOCK", "数据库时间不可用，停止执行。", 503)
        return timestamp
    # Offline backend only; production leases always use the DB clock.
    return datetime.now(UTC)


def aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def locked(session: Session, cls, identifier: str):
    # Principal is an account mutex, not an intent to change its referenced key.
    # NO KEY UPDATE still serializes all account writers (including repeated
    # nested calls), but permits FK KEY SHARE checks while an ACK holds Plan /
    # Profile. FOR UPDATE here creates Principal -> Plan -> FK(Principal) cycles.
    # Other authority rows retain their original lock strength and ordering.
    return session.scalar(
        select(cls)
        .where(cls.id == identifier)
        .with_for_update(key_share=cls is Principal)
        .execution_options(populate_existing=True)
    )
