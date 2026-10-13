"""Versioned code knowledge indexes and learning links."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "f24d8a91c502"
down_revision = "e13c9b27d401"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "code_knowledge_indexes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id", "owner_id"],
            ["repository_snapshots.id", "repository_snapshots.owner_id"],
        ),
    )
    op.create_table(
        "code_knowledge_chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(1000), nullable=False),
        sa.Column("symbol", sa.String(1000), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["snapshot_id", "owner_id"],
            ["repository_snapshots.id", "repository_snapshots.owner_id"],
        ),
        sa.UniqueConstraint("snapshot_id", "ordinal"),
    )
    op.create_index(
        "ix_code_knowledge_chunks_snapshot_id", "code_knowledge_chunks", ["snapshot_id"]
    )
    op.execute(
        "CREATE INDEX ix_code_knowledge_search ON code_knowledge_chunks "
        "USING gin (to_tsvector('simple'::regconfig, search_text))"
    )
    op.create_table(
        "code_learning_links",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "objective_version_id",
            sa.Uuid(),
            sa.ForeignKey("learning_objective_versions.id"),
            nullable=False,
        ),
        sa.Column("citation_id", sa.Uuid(), sa.ForeignKey("code_citations.id"), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("owner_id", "objective_version_id", "citation_id"),
    )
    op.create_index("ix_code_learning_links_owner_id", "code_learning_links", ["owner_id"])


def downgrade():
    op.drop_table("code_learning_links")
    op.drop_table("code_knowledge_chunks")
    op.drop_table("code_knowledge_indexes")
