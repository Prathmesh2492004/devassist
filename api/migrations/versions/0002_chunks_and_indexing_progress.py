"""chunks table and indexing progress

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIMENSIONS = 1024

NEW_STATUSES = ("queued", "cloning", "parsing", "embedding", "ready", "failed")
OLD_STATUSES = ("pending", "indexing", "ready", "failed")


def _swap_status_enum(values: Sequence[str], using: str, default: str) -> None:
    """Postgres can't drop enum values, so build a new type, convert the column, then rename."""
    op.execute(f"CREATE TYPE repo_status_new AS ENUM ({', '.join(repr(v) for v in values)})")
    op.execute("ALTER TABLE repos ALTER COLUMN status DROP DEFAULT")
    op.execute(f"ALTER TABLE repos ALTER COLUMN status TYPE repo_status_new USING ({using})")
    op.execute("DROP TYPE repo_status")
    op.execute("ALTER TYPE repo_status_new RENAME TO repo_status")
    op.execute(f"ALTER TABLE repos ALTER COLUMN status SET DEFAULT '{default}'")


def upgrade() -> None:
    _swap_status_enum(
        NEW_STATUSES,
        using="CASE status::text WHEN 'pending' THEN 'queued' WHEN 'indexing' THEN 'parsing' "
        "ELSE status::text END::repo_status_new",
        default="queued",
    )

    op.alter_column("repos", "default_branch", existing_type=sa.String(100), nullable=True)
    op.add_column("repos", sa.Column("progress", sa.Integer(), server_default="0", nullable=False))
    op.add_column("repos", sa.Column("error", sa.Text()))
    op.add_column(
        "repos", sa.Column("file_count", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column(
        "repos", sa.Column("chunk_count", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column(
        "repos",
        sa.Column("embedding_tokens", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.add_column("repos", sa.Column("indexing_started_at", sa.DateTime(timezone=True)))
    op.add_column("repos", sa.Column("indexed_at", sa.DateTime(timezone=True)))

    op.create_table(
        "chunks",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "repo_id",
            sa.Integer(),
            sa.ForeignKey("repos.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("commit_sha", sa.String(40), nullable=False),
        sa.Column("path", sa.String(1000), nullable=False),
        sa.Column("language", sa.String(30), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("symbol", sa.String(300)),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=False),
        sa.Column(
            "search",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('simple', coalesce(symbol, '') || ' ' || path || ' ' || content)",
                persisted=True,
            ),
        ),
    )
    op.create_index("ix_chunks_repo_path", "chunks", ["repo_id", "path"])
    op.create_index("ix_chunks_search", "chunks", ["search"], postgresql_using="gin")
    op.create_index(
        "ix_chunks_embedding_hnsw",
        "chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_table("chunks")
    for column in (
        "indexed_at",
        "indexing_started_at",
        "embedding_tokens",
        "chunk_count",
        "file_count",
        "error",
        "progress",
    ):
        op.drop_column("repos", column)
    op.execute("UPDATE repos SET default_branch = 'main' WHERE default_branch IS NULL")
    op.alter_column("repos", "default_branch", existing_type=sa.String(100), nullable=False)

    _swap_status_enum(
        OLD_STATUSES,
        using="CASE status::text WHEN 'queued' THEN 'pending' WHEN 'ready' THEN 'ready' "
        "WHEN 'failed' THEN 'failed' ELSE 'indexing' END::repo_status_new",
        default="pending",
    )
