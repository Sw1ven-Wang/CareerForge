"""Explicit Evolution requests; no historical manifest/hash backfills."""

import sqlalchemy as sa

from alembic import op

revision = "cf166_evolution_graph"
down_revision = "cf166_practice_graph"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workflow_evolution_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("principal_id", sa.String(36), sa.ForeignKey("principals.id"), nullable=False),
        sa.Column(
            "namespace_id", sa.String(36), sa.ForeignKey("knowledge_namespaces.id"), nullable=False
        ),
        sa.Column(
            "conversation_id", sa.String(36), sa.ForeignKey("conversations.id"), nullable=False
        ),
        sa.Column(
            "workflow_id", sa.String(36), sa.ForeignKey("workflow_sessions.id"), nullable=False
        ),
        sa.Column(
            "execution_id", sa.String(36), sa.ForeignKey("workflow_executions.id"), nullable=False
        ),
        sa.Column(
            "command_receipt_id",
            sa.String(36),
            sa.ForeignKey("idempotency_records.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("admission_manifest_hash", sa.String(64), nullable=True),
        sa.Column(
            "funding_id", sa.String(36), sa.ForeignKey("funding_agreements.id"), nullable=True
        ),
        sa.Column(
            "authorization_receipt_id",
            sa.String(36),
            sa.ForeignKey("idempotency_records.id"),
            nullable=True,
        ),
        sa.Column("authorization_witness", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "run_id", sa.String(36), sa.ForeignKey("agent_runs.id"), nullable=True, unique=True
        ),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("failure_code", sa.String(80), nullable=True),
        sa.UniqueConstraint("execution_id", "sequence", name="uq_evolution_request_sequence"),
        sa.CheckConstraint("sequence >= 1", name="ck_evolution_request_sequence"),
        sa.CheckConstraint(
            "kind IN ('candidate','replay','promote','rollback','reject')",
            name="ck_evolution_request_kind",
        ),
        sa.CheckConstraint(
            "status IN ('authorization','queued','running','completed','failed','rejected','cancelled')",
            name="ck_evolution_request_status",
        ),
        sa.CheckConstraint(
            "(funding_id IS NULL AND authorization_receipt_id IS NULL) OR (funding_id IS NOT NULL AND authorization_receipt_id IS NOT NULL)",
            name="ck_evolution_request_authority",
        ),
    )
    for name in ("principal_id", "conversation_id", "workflow_id", "execution_id"):
        op.create_index(
            "ix_workflow_evolution_requests_" + name, "workflow_evolution_requests", [name]
        )


def downgrade():
    connection = op.get_bind()
    if (
        connection.scalar(sa.text("SELECT COUNT(*) FROM workflow_evolution_requests"))
        or connection.scalar(
            sa.text(
                "SELECT COUNT(*) FROM workflow_executions WHERE workflow_type = 'evolution_session'"
            )
        )
        or connection.scalar(
            sa.text(
                "SELECT COUNT(*) FROM agent_runs WHERE graph_version IN ('evolution-candidate-v1','evolution-replay-v1')"
            )
        )
    ):
        raise RuntimeError("Retained evolution history requires its original schema and readers")
    # Exact downstream preflight before irreversible SQLite DDL. Legacy rows
    # alone are not a reason to block an otherwise valid roundtrip.
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
    op.drop_table("workflow_evolution_requests")
