"""pull request reviews

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

review_status = postgresql.ENUM(
    "queued", "running", "done", "failed", name="review_status", create_type=False
)


def upgrade() -> None:
    review_status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "reviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "repo_id", sa.Integer(), sa.ForeignKey("repos.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(500)),
        sa.Column("author", sa.String(100)),
        sa.Column("html_url", sa.String(500)),
        sa.Column("head_sha", sa.String(40)),
        sa.Column("status", review_status, nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("summary", sa.Text()),
        sa.Column("findings", postgresql.JSONB(), nullable=False),
        sa.Column("files", postgresql.JSONB(), nullable=False),
        sa.Column("model", sa.String(100)),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_reviews_repo_id", "reviews", ["repo_id"])


def downgrade() -> None:
    op.drop_table("reviews")
    review_status.drop(op.get_bind(), checkfirst=True)
