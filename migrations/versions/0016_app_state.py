"""key-value app state (id of the SEO spreadsheet)

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-19

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "app_state",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("app_state")
