"""reviews started by the GitHub App

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("reviews", sa.Column("installation_id", sa.BigInteger(), nullable=True))
    op.add_column("reviews", sa.Column("github_review_url", sa.String(500), nullable=True))
    op.add_column("reviews", sa.Column("post_error", sa.Text(), nullable=True))
    # Webhooks look up "was this commit of this PR reviewed already?"
    op.create_index("ix_reviews_repo_pr_sha", "reviews", ["repo_id", "pr_number", "head_sha"])


def downgrade() -> None:
    op.drop_index("ix_reviews_repo_pr_sha", table_name="reviews")
    op.drop_column("reviews", "post_error")
    op.drop_column("reviews", "github_review_url")
    op.drop_column("reviews", "installation_id")
