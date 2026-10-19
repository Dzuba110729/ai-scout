"""page structure for SEO analysis and the section filter of the last crawl

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-19

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "page_structures",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "page_id", sa.Integer(), sa.ForeignKey("pages.id", ondelete="CASCADE"), nullable=False, unique=True
        ),
        sa.Column("meta_title", sa.Text(), nullable=True),
        sa.Column("meta_description", sa.Text(), nullable=True),
        sa.Column("h1", sa.Text(), nullable=True),
        sa.Column("headings", sa.JSON(), nullable=True),
        sa.Column("main_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("main_text_length", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column("competitors", sa.Column("tracked_include_paths", sa.Text(), nullable=True))
    # Уже обойдённые сайты обходились с текущим фильтром — записываем его, иначе
    # первый обход после обновления не знал бы, какие разделы открыты заново.
    op.execute(
        "UPDATE competitors SET tracked_include_paths = COALESCE(include_paths, '') "
        "WHERE last_crawl_finished_at IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("competitors", "tracked_include_paths")
    op.drop_table("page_structures")
