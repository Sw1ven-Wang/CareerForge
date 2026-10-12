"""S02 database budget and status invariants."""

from alembic import op

revision = "f4e95ad00102"
down_revision = "d6b285e52f1c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_run_budget",
        "agent_runs",
        "call_budget >= 0 AND calls_reserved >= 0 AND calls_reserved <= call_budget",
    )
    op.create_check_constraint(
        "ck_run_status",
        "agent_runs",
        "status IN ('queued','running','waiting_user','succeeded',"
        "'partially_succeeded','failed','budget_stopped','cancelled')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_run_status", "agent_runs", type_="check")
    op.drop_constraint("ck_run_budget", "agent_runs", type_="check")
