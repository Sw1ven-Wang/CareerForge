"""Additive learning sessions and receipt-bound pending Tutor requests."""

import sqlalchemy as sa

from alembic import op

revision = "cf166_learning_graph"
down_revision = "cf166_workflow_controls"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("workflow_sessions") as t:
        t.add_column(sa.Column("learning_graph_version", sa.String(40), nullable=True))
    with op.batch_alter_table("workflow_executions") as t:
        t.add_column(sa.Column("predecessor_execution_id", sa.String(36), nullable=True))
        t.add_column(sa.Column("admission_receipt_id", sa.String(36), nullable=True))
        t.add_column(sa.Column("admission_action_index", sa.Integer(), nullable=True))
        t.create_foreign_key(
            "fk_learning_predecessor", "workflow_executions", ["predecessor_execution_id"], ["id"]
        )
        t.create_foreign_key(
            "fk_learning_admission",
            "conversation_action_receipts",
            ["admission_receipt_id"],
            ["id"],
        )
        t.create_unique_constraint("uq_learning_predecessor", ["predecessor_execution_id"])
        t.create_unique_constraint(
            "uq_learning_admission", ["admission_receipt_id", "admission_action_index"]
        )
        t.create_check_constraint(
            "ck_learning_admission",
            "(admission_receipt_id IS NULL AND admission_action_index IS NULL) OR (admission_receipt_id IS NOT NULL AND admission_action_index IS NOT NULL AND admission_action_index >= 0)",
        )
    # Frozen DDL, deliberately independent of future ORM model revisions.
    op.create_table(
        "workflow_tutor_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "workflow_id", sa.String(36), sa.ForeignKey("workflow_sessions.id"), nullable=False
        ),
        sa.Column(
            "conversation_id", sa.String(36), sa.ForeignKey("conversations.id"), nullable=False
        ),
        sa.Column("execution_id", sa.String(36), sa.ForeignKey("workflow_executions.id")),
        sa.Column(
            "predecessor_execution_id", sa.String(36), sa.ForeignKey("workflow_executions.id")
        ),
        sa.Column(
            "action_receipt_id",
            sa.String(36),
            sa.ForeignKey("conversation_action_receipts.id"),
            nullable=False,
        ),
        sa.Column("action_index", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column(
            "predecessor_request_id", sa.String(36), sa.ForeignKey("workflow_tutor_requests.id")
        ),
        sa.Column("predecessor_turn_id", sa.String(36), sa.ForeignKey("conversation_turns.id")),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column(
            "authorization_receipt_id",
            sa.String(36),
            sa.ForeignKey("conversation_action_receipts.id"),
        ),
        sa.Column("authorization_action_index", sa.Integer()),
        sa.Column("funding_id", sa.String(36), sa.ForeignKey("funding_agreements.id")),
        sa.Column("authorization_hash", sa.String(64)),
        sa.Column("materialized_input", sa.JSON()),
        sa.Column("materialized_hash", sa.String(64)),
        sa.Column("turn_id", sa.String(36), sa.ForeignKey("conversation_turns.id"), unique=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("agent_runs.id"), unique=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("failure_code", sa.String(100)),
        sa.UniqueConstraint("action_receipt_id", "action_index", name="uq_tutor_request_action"),
        sa.UniqueConstraint("workflow_id", "sequence", name="uq_tutor_request_sequence"),
        sa.CheckConstraint("action_index >= 0 AND sequence >= 1", name="ck_tutor_request_counters"),
        sa.CheckConstraint("kind IN ('initial','followup')", name="ck_tutor_request_kind"),
        sa.CheckConstraint(
            "status IN ('pending_handoff','pending_authorization','ready','dispatched','completed','rejected','cancelled')",
            name="ck_tutor_request_status",
        ),
        sa.CheckConstraint(
            "(turn_id IS NULL AND run_id IS NULL) OR (turn_id IS NOT NULL AND run_id IS NOT NULL)",
            name="ck_tutor_request_paid",
        ),
        sa.CheckConstraint(
            "(authorization_receipt_id IS NULL AND authorization_action_index IS NULL AND funding_id IS NULL AND authorization_hash IS NULL) OR (authorization_receipt_id IS NOT NULL AND authorization_action_index IS NOT NULL AND authorization_action_index >= 0 AND funding_id IS NOT NULL AND authorization_hash IS NOT NULL)",
            name="ck_tutor_request_authority",
        ),
    )
    op.create_index(
        "ix_workflow_tutor_requests_workflow_id", "workflow_tutor_requests", ["workflow_id"]
    )
    op.create_index(
        "ix_workflow_tutor_requests_conversation_id", "workflow_tutor_requests", ["conversation_id"]
    )


def downgrade():
    bind = op.get_bind()
    if (
        bind.execute(sa.text("SELECT COUNT(*) FROM workflow_tutor_requests")).scalar()
        or bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM workflow_sessions WHERE learning_graph_version IS NOT NULL"
            )
        ).scalar()
    ):
        raise RuntimeError("Retained learning history prohibits downgrade")
    # Preflight downstream irreversible SQLite DDL before touching this revision.
    if bind.scalar(
        sa.text(
            "SELECT COUNT(*) FROM career_goal_versions WHERE origin_contract != 'run-goal-v1' OR run_id IS NULL"
        )
    ) or bind.scalar(
        sa.text("SELECT COUNT(*) FROM workflow_goal_addenda WHERE command_receipt_id IS NOT NULL")
    ):
        raise RuntimeError(
            "Retain command-origin goal history; rollback only new admission, not readers"
        )
    if bind.scalar(sa.text("SELECT COUNT(*) FROM workflow_executions")) or bind.scalar(
        sa.text("SELECT COUNT(*) FROM workflow_sessions WHERE orchestration_engine = 'langgraph'")
    ):
        raise RuntimeError("Retain session graph history; rollback by disabling new admission")
    if bind.scalar(sa.text("SELECT COUNT(*) FROM workflow_control_commands")) or bind.scalar(
        sa.text(
            "SELECT COUNT(*) FROM workflow_sessions WHERE session_graph_version <> 'knowledge-session-v1'"
        )
    ):
        raise RuntimeError(
            "Retain workflow control and v2 graph history; disable new admission instead"
        )
    op.drop_table("workflow_tutor_requests")
    with op.batch_alter_table("workflow_executions") as t:
        t.drop_constraint("ck_learning_admission", type_="check")
        t.drop_constraint("uq_learning_admission", type_="unique")
        t.drop_constraint("uq_learning_predecessor", type_="unique")
        t.drop_constraint("fk_learning_admission", type_="foreignkey")
        t.drop_constraint("fk_learning_predecessor", type_="foreignkey")
        t.drop_column("admission_action_index")
        t.drop_column("admission_receipt_id")
        t.drop_column("predecessor_execution_id")
    with op.batch_alter_table("workflow_sessions") as t:
        t.drop_column("learning_graph_version")
