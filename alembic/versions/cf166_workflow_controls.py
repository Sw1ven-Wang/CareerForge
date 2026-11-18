"""Receipt-derived workflow controls and explicit graph admission version."""

import sqlalchemy as sa

from alembic import op

revision = "cf166_workflow_controls"
down_revision = "cf166_goal_amendment"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "workflow_sessions",
        sa.Column(
            "session_graph_version",
            sa.String(40),
            nullable=False,
            server_default="knowledge-session-v1",
        ),
    )
    op.add_column(
        "workflow_sessions",
        sa.Column("control_generation", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("workflow_executions", sa.Column("terminal_checkpoint_id", sa.String(120)))
    op.create_table(
        "workflow_control_commands",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "workflow_id", sa.String(36), sa.ForeignKey("workflow_sessions.id"), nullable=False
        ),
        sa.Column("execution_id", sa.String(36), sa.ForeignKey("workflow_executions.id")),
        sa.Column(
            "action_receipt_id",
            sa.String(36),
            sa.ForeignKey("conversation_action_receipts.id"),
            nullable=False,
        ),
        sa.Column("action_index", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("goal_binding_hash", sa.String(64)),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("checkpoint_id", sa.String(120)),
        sa.Column(
            "resume_command_id", sa.String(36), sa.ForeignKey("workflow_control_commands.id")
        ),
        sa.UniqueConstraint("workflow_id", "generation", name="uq_workflow_control_generation"),
        sa.UniqueConstraint("action_receipt_id", "action_index", name="uq_workflow_control_action"),
        sa.CheckConstraint(
            "generation >= 1 AND action_index >= 0", name="ck_workflow_control_counters"
        ),
        sa.CheckConstraint(
            "kind IN ('pause', 'resume', 'cancel')", name="ck_workflow_control_kind"
        ),
    )
    op.create_index(
        "ix_workflow_control_commands_workflow_id", "workflow_control_commands", ["workflow_id"]
    )


def downgrade():
    db = op.get_bind()
    # SQLite DDL is not transactionally rolled back across earlier revisions.
    # Refuse before any column/table drop rather than leave a half-downgraded DB.
    # The next revision also refuses command history. Preflight it here so a
    # multi-revision downgrade cannot first remove these tables/columns.
    if db.scalar(
        sa.text(
            "SELECT COUNT(*) FROM career_goal_versions WHERE origin_contract != 'run-goal-v1' OR run_id IS NULL"
        )
    ) or db.scalar(
        sa.text("SELECT COUNT(*) FROM workflow_goal_addenda WHERE command_receipt_id IS NOT NULL")
    ):
        raise RuntimeError(
            "Retain command-origin goal history; rollback only new admission, not readers"
        )
    if db.scalar(sa.text("SELECT COUNT(*) FROM workflow_executions")) or db.scalar(
        sa.text("SELECT COUNT(*) FROM workflow_sessions WHERE orchestration_engine = 'langgraph'")
    ):
        raise RuntimeError("Retain session graph history; rollback by disabling new admission")
    if db.scalar(sa.text("SELECT COUNT(*) FROM workflow_control_commands")) or db.scalar(
        sa.text(
            "SELECT COUNT(*) FROM workflow_sessions WHERE session_graph_version <> 'knowledge-session-v1'"
        )
    ):
        raise RuntimeError(
            "Retain workflow control and v2 graph history; disable new admission instead"
        )
    op.drop_table("workflow_control_commands")
    with op.batch_alter_table("workflow_executions") as batch:
        batch.drop_column("terminal_checkpoint_id")
    with op.batch_alter_table("workflow_sessions") as batch:
        batch.drop_column("control_generation")
        batch.drop_column("session_graph_version")
