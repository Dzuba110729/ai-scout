"""resumable crawl: page fetch cache + crawl progress total

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-10

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "competitors",
        sa.Column("crawl_pages_total", sa.Integer(), nullable=True),
    )

    op.create_table(
        "page_fetch_cache",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "competitor_id",
            sa.Integer(),
            sa.ForeignKey("competitors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("text_content", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("competitor_id", "url", name="uq_page_fetch_cache_competitor_url"),
    )


def downgrade() -> None:
    op.drop_table("page_fetch_cache")
    op.drop_column("competitors", "crawl_pages_total")
