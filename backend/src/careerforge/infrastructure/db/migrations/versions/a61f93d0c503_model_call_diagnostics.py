"""Keep safe wire/usage diagnostics separate from accounting totals."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a61f93d0c503"
down_revision = "f24d8a91c502"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("model_call_receipts", sa.Column("diagnostics", JSONB(), nullable=True))


def downgrade():
    op.drop_column("model_call_receipts", "diagnostics")
