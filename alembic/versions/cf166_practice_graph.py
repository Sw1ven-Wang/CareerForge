"""Receipt-bound practice and assessment commands, no historical rewrites."""

import sqlalchemy as sa

from alembic import op

revision = "cf166_practice_graph"
down_revision = "cf166_learning_graph"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workflow_practice_requests",
        sa.Column(
            "principal_id", sa.String(length=36), sa.ForeignKey("principals.id"), nullable=False
        ),
        sa.Column(
            "namespace_id",
            sa.String(length=36),
            sa.ForeignKey("knowledge_namespaces.id"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            sa.String(length=36),
            sa.ForeignKey("conversations.id"),
            nullable=False,
        ),
        sa.Column(
            "active_conversation_id",
            sa.String(length=36),
            sa.ForeignKey("conversations.id"),
            nullable=True,
        ),
        sa.Column(
            "workflow_id",
            sa.String(length=36),
            sa.ForeignKey("workflow_sessions.id"),
            nullable=False,
        ),
        sa.Column(
            "execution_id",
            sa.String(length=36),
            sa.ForeignKey("workflow_executions.id"),
            nullable=False,
        ),
        sa.Column(
            "action_receipt_id",
            sa.String(length=36),
            sa.ForeignKey("conversation_action_receipts.id"),
            nullable=False,
        ),
        sa.Column("action_index", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column(
            "activity_id",
            sa.String(length=36),
            sa.ForeignKey("dependency_objects.id"),
            nullable=True,
        ),
        sa.Column(
            "attempt_id", sa.String(length=36), sa.ForeignKey("learning_attempts.id"), nullable=True
        ),
        sa.Column(
            "workspace_id", sa.String(length=36), sa.ForeignKey("workspaces.id"), nullable=True
        ),
        sa.Column("specification_hash", sa.String(length=64), nullable=True),
        sa.Column("submission", sa.JSON(), nullable=False),
        sa.Column("failure_code", sa.String(length=80), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", name=None),
        sa.UniqueConstraint("active_conversation_id", name=None),
        sa.UniqueConstraint("activity_id", name=None),
        sa.UniqueConstraint("attempt_id", name=None),
        sa.CheckConstraint("sequence >= 1 AND action_index >= 0", name="ck_practice_counters"),
        sa.CheckConstraint(
            "(active_conversation_id IS NOT NULL AND active_conversation_id = conversation_id AND status IN ('queued','waiting_answer','submitted')) OR (active_conversation_id IS NULL AND status IN ('completed','abandoned','cancelled','rejected'))",
            name="ck_practice_active",
        ),
        sa.CheckConstraint(
            "(activity_id IS NULL AND attempt_id IS NULL AND workspace_id IS NULL AND specification_hash IS NULL) OR (activity_id IS NOT NULL AND attempt_id IS NOT NULL AND workspace_id IS NOT NULL AND specification_hash IS NOT NULL)",
            name="ck_practice_materialized",
        ),
        sa.CheckConstraint(
            "status IN ('queued','waiting_answer','submitted','completed','abandoned','cancelled','rejected')",
            name="ck_practice_status",
        ),
        sa.UniqueConstraint("action_receipt_id", "action_index", name="uq_practice_origin"),
        sa.UniqueConstraint("execution_id", "sequence", name="uq_practice_sequence"),
    )
    op.create_index(
        "ix_workflow_practice_requests_conversation_id",
        "workflow_practice_requests",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        "ix_workflow_practice_requests_execution_id",
        "workflow_practice_requests",
        ["execution_id"],
        unique=False,
    )
    op.create_index(
        "ix_workflow_practice_requests_principal_id",
        "workflow_practice_requests",
        ["principal_id"],
        unique=False,
    )
    op.create_index(
        "ix_workflow_practice_requests_workflow_id",
        "workflow_practice_requests",
        ["workflow_id"],
        unique=False,
    )
    op.create_table(
        "workflow_assessment_requests",
        sa.Column(
            "practice_id",
            sa.String(length=36),
            sa.ForeignKey("workflow_practice_requests.id"),
            nullable=False,
        ),
        sa.Column(
            "active_practice_id",
            sa.String(length=36),
            sa.ForeignKey("workflow_practice_requests.id"),
            nullable=True,
        ),
        sa.Column(
            "action_receipt_id",
            sa.String(length=36),
            sa.ForeignKey("conversation_action_receipts.id"),
            nullable=False,
        ),
        sa.Column("action_index", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("authorization_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "authorization_receipt_id",
            sa.String(length=36),
            sa.ForeignKey("conversation_action_receipts.id"),
            nullable=True,
        ),
        sa.Column("authorization_action_index", sa.Integer(), nullable=True),
        sa.Column(
            "funding_id",
            sa.String(length=36),
            sa.ForeignKey("funding_agreements.id"),
            nullable=True,
        ),
        sa.Column("authorization_hash", sa.String(length=64), nullable=True),
        sa.Column("run_id", sa.String(length=36), sa.ForeignKey("agent_runs.id"), nullable=True),
        sa.Column("result_hash", sa.String(length=64), nullable=True),
        sa.Column("failure_code", sa.String(length=80), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False, primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("active_practice_id", name=None),
        sa.UniqueConstraint("run_id", name=None),
        sa.CheckConstraint(
            "(authorization_receipt_id IS NULL AND authorization_action_index IS NULL AND funding_id IS NULL AND authorization_hash IS NULL) OR (authorization_receipt_id IS NOT NULL AND authorization_action_index IS NOT NULL AND authorization_action_index >= 0 AND funding_id IS NOT NULL AND authorization_hash IS NOT NULL)",
            name="ck_assessment_authorization",
        ),
        sa.CheckConstraint("sequence >= 1 AND action_index >= 0", name="ck_assessment_counters"),
        sa.CheckConstraint(
            "(active_practice_id IS NOT NULL AND active_practice_id = practice_id AND status IN ('pending_authorization','ready','dispatched','blocked_reconciliation')) OR (active_practice_id IS NULL AND status IN ('completed','rejected','expired','cancelled'))",
            name="ck_assessment_active",
        ),
        sa.CheckConstraint(
            "(run_id IS NULL) OR (authorization_hash IS NOT NULL AND funding_id IS NOT NULL)",
            name="ck_assessment_run_authority",
        ),
        sa.CheckConstraint(
            "status IN ('pending_authorization','ready','dispatched','completed','rejected','expired','cancelled','blocked_reconciliation')",
            name="ck_assessment_request_status",
        ),
        sa.UniqueConstraint("action_receipt_id", "action_index", name="uq_assessment_origin"),
        sa.UniqueConstraint("practice_id", "sequence", name="uq_assessment_sequence"),
    )
    op.create_index(
        "ix_workflow_assessment_requests_practice_id",
        "workflow_assessment_requests",
        ["practice_id"],
        unique=False,
    )


def downgrade():
    connection = op.get_bind()
    for table in ("workflow_practice_requests", "workflow_assessment_requests"):
        if connection.scalar(sa.text("SELECT COUNT(*) FROM " + table)):
            raise RuntimeError(
                "Practice history exists; disable new admission instead of destructive downgrade"
            )
    if connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM workflow_executions WHERE graph_version = 'learning-session-practice-v1'"
        )
    ) or connection.scalar(
        sa.text("SELECT COUNT(*) FROM agent_runs WHERE graph_version = 'learning-assessment-v1'")
    ):
        raise RuntimeError("Practice history exists; retained graph bindings forbid downgrade")
    if (
        connection.execute(sa.text("SELECT COUNT(*) FROM workflow_tutor_requests")).scalar()
        or connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM workflow_sessions WHERE learning_graph_version IS NOT NULL"
            )
        ).scalar()
    ):
        raise RuntimeError("Retained learning history prohibits downgrade")
    # Preflight downstream irreversible SQLite DDL before touching this revision.
    if connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM career_goal_versions WHERE origin_contract != 'run-goal-v1' OR run_id IS NULL"
        )
    ) or connection.scalar(
        sa.text("SELECT COUNT(*) FROM workflow_goal_addenda WHERE command_receipt_id IS NOT NULL")
    ):
        raise RuntimeError(
            "Retain command-origin goal history; rollback only new admission, not readers"
        )
    if connection.scalar(sa.text("SELECT COUNT(*) FROM workflow_executions")) or connection.scalar(
        sa.text("SELECT COUNT(*) FROM workflow_sessions WHERE orchestration_engine = 'langgraph'")
    ):
        raise RuntimeError("Retain session graph history; rollback by disabling new admission")
    if connection.scalar(
        sa.text("SELECT COUNT(*) FROM workflow_control_commands")
    ) or connection.scalar(
        sa.text(
            "SELECT COUNT(*) FROM workflow_sessions WHERE session_graph_version <> 'knowledge-session-v1'"
        )
    ):
        raise RuntimeError(
            "Retain workflow control and v2 graph history; disable new admission instead"
        )
    op.drop_table("workflow_assessment_requests")
    op.drop_table("workflow_practice_requests")
