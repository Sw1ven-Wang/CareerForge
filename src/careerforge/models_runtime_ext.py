"""v1.6 shared execution contracts and durable dependency traversal state."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, Row


class SharedBuildSlot(Row, Base):
    __tablename__ = "shared_build_slots"
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"))
    scope_hash: Mapped[str] = mapped_column(String(64))
    current_run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"))
    __table_args__ = (UniqueConstraint("namespace_id", "scope_hash"),)


class SharedBuild(Base):
    __tablename__ = "shared_builds"
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), primary_key=True)
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"))
    system_principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    funding_id: Mapped[str] = mapped_column(ForeignKey("funding_agreements.id"))
    source_id: Mapped[str] = mapped_column(ForeignKey("source_sets.id"))
    scope_hash: Mapped[str] = mapped_column(String(64), index=True)
    contract_hash: Mapped[str] = mapped_column(String(64))
    no_subscribers_policy: Mapped[str] = mapped_column(String(40), default="continue")
    ever_subscribed: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        CheckConstraint("no_subscribers_policy IN ('continue','cancel_after_last_detach')"),
    )


class TaskSubscription(Row, Base):
    __tablename__ = "task_subscriptions"
    run_id: Mapped[str] = mapped_column(ForeignKey("shared_builds.run_id"))
    subscriber_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    rights: Mapped[list[str]] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    detached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payment_consent_id: Mapped[str | None] = mapped_column(String(36))
    __table_args__ = (
        UniqueConstraint("run_id", "subscriber_id"),
        CheckConstraint("payment_consent_id IS NULL"),
        Index("ix_task_subscriptions_owner", "subscriber_id", "active", "id"),
    )


class DependencyInvalidationJob(Row, Base):
    __tablename__ = "dependency_invalidation_jobs"
    root_id: Mapped[str] = mapped_column(ForeignKey("dependency_objects.id"))
    generation: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    edges_scanned: Mapped[int] = mapped_column(Integer, default=0)
    impacts_created: Mapped[int] = mapped_column(Integer, default=0)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("root_id", "generation"),
        CheckConstraint("generation >= 1 AND edges_scanned >= 0 AND impacts_created >= 0"),
        CheckConstraint("status IN ('pending','running','completed')"),
    )


class DependencyFrontier(Row, Base):
    __tablename__ = "dependency_invalidation_frontier"
    job_id: Mapped[str] = mapped_column(ForeignKey("dependency_invalidation_jobs.id"))
    node_id: Mapped[str] = mapped_column(ForeignKey("dependency_objects.id"))
    edge_cursor: Mapped[str | None] = mapped_column(String(36))
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        UniqueConstraint("job_id", "node_id"),
        Index("ix_dependency_frontier_pending", "job_id", "done", "id"),
    )
