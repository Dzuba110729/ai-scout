"""schedule config + google sheets fields on competitor

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-10

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("competitors", sa.Column("google_sheet_id", sa.Text, nullable=True))
    op.add_column("competitors", sa.Column("google_sheet_url", sa.Text, nullable=True))
    op.add_column("competitors", sa.Column("last_crawl_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("competitors", sa.Column("last_crawl_finished_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("competitors", sa.Column("next_crawl_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "schedule_config",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("interval_days", sa.Integer, nullable=False, server_default="7"),
        sa.Column("interval_hours", sa.Integer, nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.execute("INSERT INTO schedule_config (id, interval_days, interval_hours) VALUES (1, 7, 0)")


def downgrade() -> None:
    op.drop_table("schedule_config")
    op.drop_column("competitors", "next_crawl_at")
    op.drop_column("competitors", "last_crawl_finished_at")
    op.drop_column("competitors", "last_crawl_started_at")
    op.drop_column("competitors", "google_sheet_url")
    op.drop_column("competitors", "google_sheet_id")
