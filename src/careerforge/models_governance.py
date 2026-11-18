"""Additive taxonomy and local maintenance governance; no historical row rewriting."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, Row


class TaxonomyHead(Base):
    __tablename__ = "taxonomy_heads"
    namespace_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_namespaces.id"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(Integer, default=0)
    release_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class TaxonomyProposal(Row, Base):
    __tablename__ = "taxonomy_proposals"
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"), index=True)
    proposer_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    idempotency_key: Mapped[str] = mapped_column(String(150))
    status: Mapped[str] = mapped_column(String(24), default="proposed")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    content_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    reviewer_id: Mapped[str | None] = mapped_column(ForeignKey("principals.id"), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    release_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    __table_args__ = (UniqueConstraint("namespace_id", "proposer_id", "idempotency_key"),)


class TaxonomyRelease(Row, Base):
    __tablename__ = "taxonomy_releases"
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("taxonomy_proposals.id"), unique=True)
    reviewer_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("namespace_id", "revision"),)


class TopicGovernance(Base):
    __tablename__ = "topic_governance"
    topic_id: Mapped[str] = mapped_column(ForeignKey("technology_topics.id"), primary_key=True)
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"), index=True)
    release_id: Mapped[str] = mapped_column(ForeignKey("taxonomy_releases.id"))
    status: Mapped[str] = mapped_column(String(24), default="active")
    replacements: Mapped[list[str]] = mapped_column(JSON, default=list)


class TopicRelation(Row, Base):
    __tablename__ = "topic_version_relations"
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"))
    topic_id: Mapped[str] = mapped_column(ForeignKey("technology_topics.id"))
    version: Mapped[int] = mapped_column(Integer)
    target_topic_id: Mapped[str] = mapped_column(ForeignKey("technology_topics.id"))
    relation: Mapped[str] = mapped_column(String(24))
    __table_args__ = (UniqueConstraint("topic_id", "version", "target_topic_id", "relation"),)


class SourceUpdatePlan(Row, Base):
    __tablename__ = "source_update_plans"
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"), index=True)
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    old_source_id: Mapped[str] = mapped_column(ForeignKey("source_sets.id"))
    new_source_id: Mapped[str] = mapped_column(ForeignKey("source_sets.id"))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(24), default="proposed")
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    build_run_id: Mapped[str | None] = mapped_column(ForeignKey("agent_runs.id"), nullable=True)


class MaintenanceSchedule(Row, Base):
    __tablename__ = "maintenance_schedules"
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"), index=True)
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    source_id: Mapped[str] = mapped_column(ForeignKey("source_sets.id"))
    interval_seconds: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_model_calls: Mapped[bool] = mapped_column(Boolean, default=False)
    funding_id: Mapped[str | None] = mapped_column(
        ForeignKey("funding_agreements.id"), nullable=True
    )
    call_cap: Mapped[int] = mapped_column(Integer, default=10)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    next_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MaintenanceOccurrence(Row, Base):
    __tablename__ = "maintenance_occurrences"
    schedule_id: Mapped[str] = mapped_column(ForeignKey("maintenance_schedules.id"))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    plan_id: Mapped[str | None] = mapped_column(ForeignKey("source_update_plans.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(String(100))
    __table_args__ = (UniqueConstraint("schedule_id", "due_at"),)
