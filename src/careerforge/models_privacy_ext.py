"""Privacy control plane: no personal source bodies are stored in these audit rows."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, Row
from .util import now


class PrivacyState(Base):
    __tablename__ = "privacy_states"
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"), primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    operation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PrivacyDeletion(Row, Base):
    __tablename__ = "privacy_deletions"
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"), index=True)
    scope: Mapped[str] = mapped_column(String(40), default="all_personal_content")
    state: Mapped[str] = mapped_column(String(24), default="proposed")
    generation: Mapped[int] = mapped_column(Integer)
    nonce_hash: Mapped[str] = mapped_column(String(64))
    plan_hash: Mapped[str] = mapped_column(String(64))
    plan: Mapped[dict[str, Any]] = mapped_column(JSON)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    audit_retain_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PrivacyRetentionHold(Row, Base):
    __tablename__ = "privacy_retention_holds"
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"), index=True)
    placed_by: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    category: Mapped[str] = mapped_column(String(40), default="personal_content")
    reason_code: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
