"""persisted next run of the scheduled crawl cycle

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-24

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("schedule_config", sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True))
    # Раньше время следующего обхода было у каждого сайта своё — берём ближайшее,
    # чтобы общий цикл не сдвинулся дальше, чем уже был запланирован любой обход.
    op.execute(
        """
        UPDATE schedule_config SET next_run_at = (
            SELECT MIN(next_crawl_at) FROM competitors WHERE is_paused = false
        )
        """
    )


def downgrade() -> None:
    op.drop_column("schedule_config", "next_run_at")
