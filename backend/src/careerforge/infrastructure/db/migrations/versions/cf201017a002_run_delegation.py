"""Bounded run delegation, held call slots and terminal settlement."""

import sqlalchemy as sa
from alembic import op

revision = "cf201017a002"
down_revision = "cf201013a001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("parent_run_id", sa.Uuid(), nullable=True))
    op.add_column("agent_runs", sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "agent_runs", sa.Column("held_calls", sa.Integer(), server_default="0", nullable=False)
    )
    op.create_index("ix_agent_runs_parent_run_id", "agent_runs", ["parent_run_id"])
    op.create_foreign_key(
        "fk_run_parent_owner",
        "agent_runs",
        "agent_runs",
        ["parent_run_id", "owner_id"],
        ["id", "owner_id"],
    )
    op.create_check_constraint(
        "ck_run_parent_distinct", "agent_runs", "parent_run_id IS NULL OR parent_run_id <> id"
    )
    op.drop_constraint("ck_run_budget", "agent_runs", type_="check")
    op.create_check_constraint(
        "ck_run_budget",
        "agent_runs",
        "call_budget >= 0 AND calls_reserved >= 0 AND held_calls >= 0 "
        "AND calls_reserved + held_calls <= call_budget",
    )
    op.drop_constraint("ck_run_status", "agent_runs", type_="check")
    op.create_check_constraint(
        "ck_run_status",
        "agent_runs",
        "status IN ('queued','running','waiting_user','waiting_children','succeeded',"
        "'partially_succeeded','failed','budget_stopped','cancelled')",
    )
    op.create_table(
        "run_allocations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("parent_run_id", sa.Uuid(), nullable=False),
        sa.Column("child_run_id", sa.Uuid(), nullable=False),
        sa.Column("task_key", sa.String(100), nullable=False),
        sa.Column("allocated_calls", sa.Integer(), nullable=False),
        sa.Column("output_token_limit", sa.Integer(), nullable=False),
        sa.Column("released_calls", sa.Integer(), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["parent_run_id", "owner_id"], ["agent_runs.id", "agent_runs.owner_id"]
        ),
        sa.ForeignKeyConstraint(
            ["child_run_id", "owner_id"], ["agent_runs.id", "agent_runs.owner_id"]
        ),
        sa.UniqueConstraint("parent_run_id", "task_key"),
        sa.UniqueConstraint("child_run_id"),
        sa.CheckConstraint(
            "allocated_calls > 0 AND released_calls >= 0 AND released_calls <= allocated_calls",
            name="ck_allocation_calls",
        ),
        sa.CheckConstraint(
            "output_token_limit >= 512 AND output_token_limit <= 8192", name="ck_allocation_tokens"
        ),
    )
    op.create_index("ix_run_allocations_parent_run_id", "run_allocations", ["parent_run_id"])


def downgrade() -> None:
    active: int = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM agent_runs WHERE held_calls > 0 "
                "OR status = 'waiting_children' OR (parent_run_id IS NOT NULL "
                "AND status IN ('queued','running','waiting_user'))"
            )
        )
        .scalar_one()
    )
    if active:
        raise RuntimeError("Finish or cancel active delegated tasks before downgrading.")
    op.drop_table("run_allocations")
    op.drop_constraint("ck_run_budget", "agent_runs", type_="check")
    op.create_check_constraint(
        "ck_run_budget",
        "agent_runs",
        "call_budget >= 0 AND calls_reserved >= 0 AND calls_reserved <= call_budget",
    )
    op.drop_constraint("ck_run_status", "agent_runs", type_="check")
    op.create_check_constraint(
        "ck_run_status",
        "agent_runs",
        "status IN ('queued','running','waiting_user','succeeded',"
        "'partially_succeeded','failed','budget_stopped','cancelled')",
    )
    op.drop_constraint("ck_run_parent_distinct", "agent_runs", type_="check")
    op.drop_constraint("fk_run_parent_owner", "agent_runs", type_="foreignkey")
    op.drop_index("ix_agent_runs_parent_run_id", table_name="agent_runs")
    op.drop_column("agent_runs", "parent_run_id")
    op.drop_column("agent_runs", "deadline_at")
    op.drop_column("agent_runs", "held_calls")
