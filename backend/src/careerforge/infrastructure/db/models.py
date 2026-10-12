from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Timestamped, Base):
    __tablename__ = "users"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)


class UserSession(Timestamped, Base):
    __tablename__ = "user_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoginAttempt(Timestamped, Base):
    __tablename__ = "login_attempts"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    identity_hash: Mapped[str] = mapped_column(String(64), index=True)


class CareerGoal(Timestamped, Base):
    __tablename__ = "career_goals"
    __table_args__ = (UniqueConstraint("id", "owner_id"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    revision: Mapped[int] = mapped_column(default=1)


class CareerGoalVersion(Timestamped, Base):
    __tablename__ = "career_goal_versions"
    __table_args__ = (
        ForeignKeyConstraint(["goal_id", "owner_id"], ["career_goals.id", "career_goals.owner_id"]),
        UniqueConstraint("goal_id", "version_no"),
        UniqueConstraint("id", "owner_id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    goal_id: Mapped[UUID]
    owner_id: Mapped[UUID]
    version_no: Mapped[int]
    content_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    change_reason: Mapped[str | None] = mapped_column(Text)


class Source(Timestamped, Base):
    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("id", "owner_id"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    identity_hash: Mapped[str] = mapped_column(String(64), index=True)
    source_url: Mapped[str | None] = mapped_column(Text)
    import_mode: Mapped[str] = mapped_column(String(20))
    source_type: Mapped[str] = mapped_column(String(40))
    title: Mapped[str | None] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(default=1)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourceSnapshot(Timestamped, Base):
    __tablename__ = "source_snapshots"
    __table_args__ = (
        ForeignKeyConstraint(["source_id", "owner_id"], ["sources.id", "sources.owner_id"]),
        UniqueConstraint("source_id", "version_no"),
        UniqueConstraint("id", "owner_id"),
        UniqueConstraint("id", "source_id", "owner_id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    source_id: Mapped[UUID]
    owner_id: Mapped[UUID]
    version_no: Mapped[int]
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    metadata_hash: Mapped[str] = mapped_column(String(64))
    metadata_values: Mapped[dict[str, Any]] = mapped_column(JSONB)
    change_reason: Mapped[str | None] = mapped_column(Text)


class Citation(Timestamped, Base):
    __tablename__ = "citations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["snapshot_id", "source_id", "owner_id"],
            ["source_snapshots.id", "source_snapshots.source_id", "source_snapshots.owner_id"],
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    source_id: Mapped[UUID]
    snapshot_id: Mapped[UUID]
    owner_id: Mapped[UUID]
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)
    text_hash: Mapped[str] = mapped_column(String(64))


class IdempotencyRecord(Timestamped, Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("owner_id", "operation", "key"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    operation: Mapped[str] = mapped_column(String(200))
    key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    response_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class Deletion(Timestamped, Base):
    __tablename__ = "deletions"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    source_id: Mapped[UUID] = mapped_column(ForeignKey("sources.id"))
    status: Mapped[str] = mapped_column(String(20), default="revoked")
