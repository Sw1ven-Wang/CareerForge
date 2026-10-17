"""Versioned study artifacts; no automatic migration of historical plans."""

from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, ForeignKeyConstraint, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from careerforge.domain.json_types import JsonObject as Json
from careerforge.infrastructure.db.models import Base, Timestamped


class StudyTrack(Timestamped, Base):
    __tablename__ = "study_tracks"
    __table_args__ = (UniqueConstraint("id", "owner_id"),)
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    revision: Mapped[int] = mapped_column(default=1)
    payload: Mapped[Json] = mapped_column(JSONB)


class StudyArtifact(Timestamped, Base):
    __tablename__ = "study_artifacts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["track_id", "owner_id"], ["study_tracks.id", "study_tracks.owner_id"]
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    track_id: Mapped[UUID] = mapped_column(index=True)
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("agent_runs.id"))
    kind: Mapped[str] = mapped_column(String(30))
    payload: Mapped[Json] = mapped_column(JSONB)
