"""Explicit, private quarantine for historical data; never legacy publication authority."""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, Row


class LegacyImportBatch(Row, Base):
    __tablename__ = "legacy_import_batches"
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"), index=True)
    namespace_id: Mapped[str] = mapped_column(ForeignKey("knowledge_namespaces.id"), index=True)
    baseline_sha: Mapped[str] = mapped_column(String(40))
    legacy_owner_id: Mapped[str] = mapped_column(String(36))
    bundle_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="unreviewed_legacy")
    __table_args__ = (UniqueConstraint("principal_id", "bundle_hash"),)


class LegacyImportRecord(Row, Base):
    __tablename__ = "legacy_import_records"
    batch_id: Mapped[str] = mapped_column(ForeignKey("legacy_import_batches.id"), index=True)
    table_name: Mapped[str] = mapped_column(String(60))
    legacy_id: Mapped[str] = mapped_column(String(36))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    mapped_source_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_sets.id"), nullable=True
    )
    __table_args__ = (UniqueConstraint("batch_id", "table_name", "legacy_id"),)
