"""users and repos

Revision ID: 0001
Revises:
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

repo_status = sa.Enum("pending", "indexing", "ready", "failed", name="repo_status")


def upgrade() -> None:
    # pgvector is needed from Week 2 (embeddings); enabling it now proves the image is right.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("github_id", sa.BigInteger(), nullable=False),
        sa.Column("login", sa.String(100), nullable=False),
        sa.Column("name", sa.String(200)),
        sa.Column("avatar_url", sa.String(500)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_users_github_id", "users", ["github_id"], unique=True)

    op.create_table(
        "repos",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "owner_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("full_name", sa.String(200), nullable=False),
        sa.Column("default_branch", sa.String(100), nullable=False),
        sa.Column("status", repo_status, nullable=False),
        sa.Column("last_indexed_sha", sa.String(40)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("owner_id", "full_name", name="uq_repos_owner_full_name"),
    )
    op.create_index("ix_repos_owner_id", "repos", ["owner_id"])


def downgrade() -> None:
    op.drop_index("ix_repos_owner_id", table_name="repos")
    op.drop_table("repos")
    op.drop_index("ix_users_github_id", table_name="users")
    op.drop_table("users")
    repo_status.drop(op.get_bind(), checkfirst=True)
