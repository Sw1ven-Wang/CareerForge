"""Bind new command approvals to the entire execution identity.

Existing approvals deliberately remain NULL: no migration may invent user consent.
They fail closed in the service and must be proposed and approved again.
"""

import sqlalchemy as sa

from alembic import op

revision = "cf151_command_binding"
down_revision = "cf150_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "command_authorizations", sa.Column("authorization_digest", sa.String(64), nullable=True)
    )


def downgrade():
    raise RuntimeError(
        "Removing execution approval bindings is unsafe. Restore an isolated verified backup instead."
    )
