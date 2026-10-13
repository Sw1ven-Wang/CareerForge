"""Learning IDE workspaces, independent from reference snapshots."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "e13c9b27d401"
down_revision = "b183a7e2c901"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "learning_workspaces",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_learning_workspaces_owner_id", "learning_workspaces", ["owner_id"])


def downgrade():
    op.drop_table("learning_workspaces")
