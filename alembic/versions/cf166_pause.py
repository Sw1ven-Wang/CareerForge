"""Persist cooperative pause control alongside the runtime lease."""

import sqlalchemy as sa

from alembic import op

revision = "cf166_pause"
down_revision = "cf166_dialogue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("pause_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    with op.batch_alter_table("agent_runs") as table:
        table.drop_column("pause_requested")
