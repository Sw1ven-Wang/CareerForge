"""Agent study tracks and immutable typed artifacts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "cf201013a001"
down_revision = "a61f93d0c503"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "study_tracks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("id", "owner_id"),
    )
    op.create_index("ix_study_tracks_owner_id", "study_tracks", ["owner_id"])
    op.create_table(
        "study_artifacts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("track_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("agent_runs.id"), nullable=True),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["track_id", "owner_id"], ["study_tracks.id", "study_tracks.owner_id"]
        ),
    )
    op.create_index("ix_study_artifacts_owner_id", "study_artifacts", ["owner_id"])
    op.create_index("ix_study_artifacts_track_id", "study_artifacts", ["track_id"])


def downgrade() -> None:
    op.drop_table("study_artifacts")
    op.drop_table("study_tracks")
