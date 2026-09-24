"""importance of a finding (high/medium/low) with a short reason

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-24

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ai_analysis", sa.Column("importance", sa.Text(), nullable=True))
    op.add_column("ai_analysis", sa.Column("importance_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("ai_analysis", "importance_reason")
    op.drop_column("ai_analysis", "importance")
