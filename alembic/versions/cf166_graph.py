"""Pin graph engine/version on each run; checkpoint tables are provisioned separately."""

import sqlalchemy as sa

from alembic import op

revision = "cf166_graph"
down_revision = "cf166_pause"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "agent_runs",
        sa.Column("workflow_engine", sa.String(16), nullable=False, server_default="legacy"),
    )
    op.add_column("agent_runs", sa.Column("graph_version", sa.String(40), nullable=True))
    op.add_column("agent_runs", sa.Column("state_schema_version", sa.Integer(), nullable=True))


def downgrade():
    connection = op.get_bind()
    if connection.scalar(
        sa.text("SELECT count(*) FROM agent_runs WHERE workflow_engine != 'legacy'")
    ):
        raise RuntimeError("Retain graph run history: rollback new-run flag, not graph columns.")
    with op.batch_alter_table("agent_runs") as table:
        table.drop_column("state_schema_version")
        table.drop_column("graph_version")
        table.drop_column("workflow_engine")
