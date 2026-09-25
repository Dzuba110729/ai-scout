"""section filter and cookies-only crawling per competitor

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-25

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("competitors", sa.Column("include_paths", sa.Text(), nullable=True))
    op.add_column(
        "competitors",
        sa.Column("cookies_only", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("competitors", "cookies_only")
    op.drop_column("competitors", "include_paths")
