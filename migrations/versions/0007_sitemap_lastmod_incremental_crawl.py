"""sitemap lastmod for incremental crawling

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-16

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "pages",
        sa.Column("sitemap_lastmod", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "competitors",
        sa.Column("last_full_crawl_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("competitors", "last_full_crawl_at")
    op.drop_column("pages", "sitemap_lastmod")
