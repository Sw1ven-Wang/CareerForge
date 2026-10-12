from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
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


class AgentRun(Timestamped, Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        UniqueConstraint("id", "owner_id"),
        ForeignKeyConstraint(
            ["goal_version_id", "owner_id"],
            ["career_goal_versions.id", "career_goal_versions.owner_id"],
        ),
        CheckConstraint(
            "call_budget >= 0 AND calls_reserved >= 0 AND calls_reserved <= call_budget",
            name="ck_run_budget",
        ),
        CheckConstraint(
            "status IN ('queued','running','waiting_user','succeeded',"
            "'partially_succeeded','failed','budget_stopped','cancelled')",
            name="ck_run_status",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    goal_version_id: Mapped[UUID | None]
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.clock_timestamp()
    )
    workflow_type: Mapped[str] = mapped_column(String(40))
    workflow_version: Mapped[str] = mapped_column(String(80))
    input_manifest: Mapped[dict[str, Any]] = mapped_column(JSONB)
    input_manifest_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    version: Mapped[int] = mapped_column(default=1)
    epoch: Mapped[int] = mapped_column(default=0)
    worker_id: Mapped[str | None] = mapped_column(String(100))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    event_seq: Mapped[int] = mapped_column(default=0)
    min_event_seq: Mapped[int] = mapped_column(default=1)
    call_budget: Mapped[int] = mapped_column(default=3)
    calls_reserved: Mapped[int] = mapped_column(default=0)


class RunAttempt(Timestamped, Base):
    __tablename__ = "run_attempts"
    __table_args__ = (UniqueConstraint("run_id", "epoch"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id"))
    epoch: Mapped[int]
    worker_id: Mapped[str] = mapped_column(String(100))


class RunEvent(Timestamped, Base):
    __tablename__ = "run_events"
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id"), primary_key=True)
    seq: Mapped[int] = mapped_column(primary_key=True)
    schema_version: Mapped[int] = mapped_column(default=1)
    type: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class StageArtifact(Timestamped, Base):
    __tablename__ = "stage_artifacts"
    __table_args__ = (
        UniqueConstraint("run_id", "stage_key", "input_fingerprint", "stage_version"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id"))
    stage_key: Mapped[str] = mapped_column(String(100))
    input_fingerprint: Mapped[str] = mapped_column(String(64))
    stage_version: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class WaitRequest(Timestamped, Base):
    __tablename__ = "wait_requests"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "stage_key", "input_fingerprint", "question_version", "wait_sequence"
        ),
        UniqueConstraint("id", "run_id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id"))
    stage_key: Mapped[str] = mapped_column(String(100))
    input_fingerprint: Mapped[str] = mapped_column(String(64))
    question_version: Mapped[str] = mapped_column(String(80))
    wait_sequence: Mapped[int] = mapped_column(default=1)
    question: Mapped[dict[str, Any]] = mapped_column(JSONB)
    response_schema: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ResumeDecision(Timestamped, Base):
    __tablename__ = "resume_decisions"
    wait_request_id: Mapped[UUID] = mapped_column(ForeignKey("wait_requests.id"), primary_key=True)
    response: Mapped[dict[str, Any]] = mapped_column(JSONB)
    response_hash: Mapped[str] = mapped_column(String(64))


class OutboxMessage(Timestamped, Base):
    __tablename__ = "outbox_messages"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ModelCallReceipt(Timestamped, Base):
    __tablename__ = "model_call_receipts"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("agent_runs.id"))
    epoch: Mapped[int]
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="reserved")
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
