"""Bind session graphs without extending AgentRun budget or lease lifetime."""

import sqlalchemy as sa

from alembic import op

revision = "cf166_session_graph"
down_revision = "cf166_graph"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "workflow_sessions",
        sa.Column("orchestration_engine", sa.String(16), nullable=False, server_default="legacy"),
    )
    op.create_table(
        "workflow_executions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "workflow_id", sa.String(36), sa.ForeignKey("workflow_sessions.id"), nullable=False
        ),
        sa.Column("execution_generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("principal_id", sa.String(36), sa.ForeignKey("principals.id"), nullable=False),
        sa.Column(
            "namespace_id", sa.String(36), sa.ForeignKey("knowledge_namespaces.id"), nullable=False
        ),
        sa.Column(
            "conversation_id", sa.String(36), sa.ForeignKey("conversations.id"), nullable=False
        ),
        sa.Column(
            "workflow_type", sa.String(40), nullable=False, server_default="knowledge_session"
        ),
        sa.Column("workflow_engine", sa.String(16), nullable=False, server_default="langgraph"),
        sa.Column(
            "graph_version", sa.String(40), nullable=False, server_default="knowledge-session-v1"
        ),
        sa.Column("state_schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("goal_version_id", sa.String(36), sa.ForeignKey("career_goal_versions.id")),
        sa.Column("source_jd_revision_id", sa.String(36), sa.ForeignKey("dependency_objects.id")),
        sa.Column("goal_addendum_id", sa.String(36), sa.ForeignKey("workflow_goal_addenda.id")),
        sa.Column("goal_binding_hash", sa.String(64), nullable=False),
        sa.Column("binding_payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="queued"),
        sa.Column("wait_kind", sa.String(32)),
        sa.Column("last_error_code", sa.String(100)),
        sa.Column("epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("worker_id", sa.String(120)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("wake_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("handled_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "acceptance_receipt_id", sa.String(36), sa.ForeignKey("conversation_action_receipts.id")
        ),
        sa.Column("acceptance_action_index", sa.Integer()),
        sa.UniqueConstraint(
            "workflow_id", "execution_generation", name="uq_workflow_execution_generation"
        ),
        sa.CheckConstraint(
            "execution_generation >= 1 AND epoch >= 0 AND revision >= 1 AND wake_generation >= 0 AND handled_generation >= 0 AND handled_generation <= wake_generation",
            name="ck_workflow_execution_counters",
        ),
        sa.CheckConstraint(
            "workflow_engine = 'langgraph' AND state_schema_version >= 1",
            name="ck_workflow_execution_engine",
        ),
        sa.CheckConstraint(
            "acceptance_action_index IS NULL OR acceptance_action_index >= 0",
            name="ck_workflow_acceptance_index",
        ),
    )
    op.create_index("ix_workflow_executions_workflow_id", "workflow_executions", ["workflow_id"])
    op.create_index(
        "ix_workflow_executions_status_lease", "workflow_executions", ["status", "lease_until"]
    )
    # The named, deferred metadata FK breaks the session/execution create cycle.
    # Batch mode also supports adding this FK on an existing SQLite database.
    with op.batch_alter_table("workflow_sessions") as table:
        table.add_column(sa.Column("current_execution_id", sa.String(36), nullable=True))
        table.create_foreign_key(
            "fk_workflow_current_execution", "workflow_executions", ["current_execution_id"], ["id"]
        )
    op.create_table(
        "workflow_plan_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "execution_id", sa.String(36), sa.ForeignKey("workflow_executions.id"), nullable=False
        ),
        sa.Column(
            "action_receipt_id",
            sa.String(36),
            sa.ForeignKey("conversation_action_receipts.id"),
            nullable=False,
        ),
        sa.Column("action_index", sa.Integer(), nullable=False),
        sa.Column("funding_id", sa.String(36), sa.ForeignKey("funding_agreements.id")),
        sa.Column("goal_binding_hash", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("plan_run_id", sa.String(36), sa.ForeignKey("agent_runs.id")),
        sa.Column("failure_code", sa.String(100)),
        sa.UniqueConstraint(
            "action_receipt_id", "action_index", name="uq_workflow_plan_request_action"
        ),
        sa.CheckConstraint("action_index >= 0", name="ck_workflow_plan_request_action_index"),
    )
    op.create_index(
        "ix_workflow_plan_requests_execution_id", "workflow_plan_requests", ["execution_id"]
    )


def downgrade():
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT count(*) FROM workflow_executions")) or connection.scalar(
        sa.text("SELECT count(*) FROM workflow_sessions WHERE orchestration_engine != 'legacy'")
    ):
        raise RuntimeError(
            "Retain session graph history: rollback new-session flag, not execution tables."
        )
    op.drop_table("workflow_plan_requests")
    with op.batch_alter_table("workflow_sessions") as table:
        table.drop_constraint("fk_workflow_current_execution", type_="foreignkey")
        table.drop_column("current_execution_id")
        table.drop_column("orchestration_engine")
    op.drop_table("workflow_executions")
