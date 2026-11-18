"""Immutable command-origin goals; historical run origins remain unchanged."""

import sqlalchemy as sa

from alembic import op

revision = "cf166_goal_amendment"
down_revision = "cf166_session_graph"
branch_labels = None
depends_on = None
ORIGIN_CHECK = (
    "(origin_contract = 'run-goal-v1' AND run_id IS NOT NULL "
    "AND parent_goal_version_id IS NULL AND goal_addendum_id IS NULL AND version_hash IS NULL) OR "
    "(origin_contract = 'command-goal-v1' AND run_id IS NULL AND parent_goal_version_id IS NOT NULL "
    "AND goal_addendum_id IS NOT NULL AND version_hash IS NOT NULL AND version >= 2)"
)


def upgrade():
    with op.batch_alter_table("workflow_goal_addenda") as table:
        table.add_column(sa.Column("command_receipt_id", sa.String(36), nullable=True))
        table.add_column(sa.Column("command_action_index", sa.Integer(), nullable=True))
        table.create_foreign_key(
            "fk_addendum_command_receipt",
            "conversation_action_receipts",
            ["command_receipt_id"],
            ["id"],
        )
        table.create_unique_constraint(
            "uq_goal_addendum_command", ["command_receipt_id", "command_action_index"]
        )
        table.create_check_constraint(
            "ck_goal_addendum_command",
            "(command_receipt_id IS NULL AND command_action_index IS NULL) OR (command_receipt_id IS NOT NULL AND command_action_index IS NOT NULL AND command_action_index >= 0)",
        )
        table.create_index("ix_workflow_goal_addenda_command_receipt_id", ["command_receipt_id"])
    with op.batch_alter_table("career_goal_versions") as table:
        table.alter_column("run_id", existing_type=sa.String(36), nullable=True)
        table.add_column(
            sa.Column(
                "origin_contract", sa.String(40), nullable=False, server_default="run-goal-v1"
            )
        )
        table.add_column(sa.Column("parent_goal_version_id", sa.String(36), nullable=True))
        table.add_column(sa.Column("goal_addendum_id", sa.String(36), nullable=True))
        table.add_column(sa.Column("version_hash", sa.String(64), nullable=True))
        table.create_foreign_key(
            "fk_goal_version_parent", "career_goal_versions", ["parent_goal_version_id"], ["id"]
        )
        table.create_foreign_key(
            "fk_goal_version_addendum", "workflow_goal_addenda", ["goal_addendum_id"], ["id"]
        )
        table.create_unique_constraint("uq_goal_version_addendum", ["goal_addendum_id"])
        table.create_index(
            "ix_career_goal_versions_parent_goal_version_id", ["parent_goal_version_id"]
        )
        table.create_check_constraint("ck_goal_version_origin", ORIGIN_CHECK)


def downgrade():
    bind = op.get_bind()
    if (
        bind.execute(
            sa.text(
                "SELECT 1 FROM career_goal_versions WHERE origin_contract != 'run-goal-v1' OR run_id IS NULL LIMIT 1"
            )
        ).first()
        or bind.execute(
            sa.text(
                "SELECT 1 FROM workflow_goal_addenda WHERE command_receipt_id IS NOT NULL LIMIT 1"
            )
        ).first()
    ):
        raise RuntimeError(
            "Retain command-origin goal history; rollback only new admission, not readers"
        )
    with op.batch_alter_table("career_goal_versions") as table:
        table.drop_constraint("ck_goal_version_origin", type_="check")
        table.drop_constraint("fk_goal_version_parent", type_="foreignkey")
        table.drop_constraint("fk_goal_version_addendum", type_="foreignkey")
        table.drop_constraint("uq_goal_version_addendum", type_="unique")
        table.drop_index("ix_career_goal_versions_parent_goal_version_id")
        for name in (
            "origin_contract",
            "parent_goal_version_id",
            "goal_addendum_id",
            "version_hash",
        ):
            table.drop_column(name)
        table.alter_column("run_id", existing_type=sa.String(36), nullable=False)
    with op.batch_alter_table("workflow_goal_addenda") as table:
        table.drop_index("ix_workflow_goal_addenda_command_receipt_id")
        table.drop_constraint("fk_addendum_command_receipt", type_="foreignkey")
        table.drop_constraint("uq_goal_addendum_command", type_="unique")
        table.drop_constraint("ck_goal_addendum_command", type_="check")
        table.drop_column("command_receipt_id")
        table.drop_column("command_action_index")
