"""last run report link on competitor

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-24

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("competitors", sa.Column("last_report_url", sa.Text(), nullable=True))
    op.add_column(
        "competitors",
        sa.Column("last_report_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("competitors", "last_report_at")
    op.drop_column("competitors", "last_report_url")
