"""Editable source library folders.

Revision ID: b183a7e2c901
Revises: af4ebdc49e86
"""

import sqlalchemy as sa
from alembic import op

revision = "b183a7e2c901"
down_revision = "af4ebdc49e86"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "source_library_categories",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("id", "owner_id"),
        sa.UniqueConstraint("owner_id", "parent_id", "name"),
        sa.ForeignKeyConstraint(
            ["parent_id", "owner_id"],
            ["source_library_categories.id", "source_library_categories.owner_id"],
        ),
    )
    op.create_index(
        "ix_source_library_categories_owner_id", "source_library_categories", ["owner_id"]
    )
    op.create_index(
        "uq_source_library_root_name",
        "source_library_categories",
        ["owner_id", "name"],
        unique=True,
        postgresql_where=sa.text("parent_id IS NULL"),
    )
    op.add_column("sources", sa.Column("library_category_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_sources_library_category_owner",
        "sources",
        "source_library_categories",
        ["library_category_id", "owner_id"],
        ["id", "owner_id"],
    )


def downgrade():
    op.drop_constraint("fk_sources_library_category_owner", "sources", type_="foreignkey")
    op.drop_column("sources", "library_category_id")
    op.drop_table("source_library_categories")
