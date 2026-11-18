"""Rebuildable HTTP projections and authoritative distributed request guards."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class ExternalBudget(Base):
    __tablename__ = "external_request_budgets"
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), primary_key=True)
    request_cap: Mapped[int] = mapped_column(Integer)


class GitHubRateBucket(Base):
    __tablename__ = "github_rate_buckets"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    resource: Mapped[str] = mapped_column(String(30))
    tokens: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GitHubHttpCache(Base):
    __tablename__ = "github_http_cache"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    identity_hash: Mapped[str] = mapped_column(String(64), index=True)
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"))
    request_hash: Mapped[str] = mapped_column(String(64))
    etag: Mapped[str] = mapped_column(String(500))
    body: Mapped[bytes] = mapped_column(LargeBinary)
    body_hash: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
