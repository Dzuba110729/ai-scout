"""page disappearance reason + redirect target

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-10

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_REASON_VALUES = ("deleted", "moved", "missing_from_crawl")


def upgrade() -> None:
    reason = sa.Enum(*_REASON_VALUES, name="disappearance_reason")
    # Тип создаём явно: add_column сам его не создаст, если тип ещё не существует.
    reason.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "pages",
        sa.Column("disappearance_reason", reason, nullable=True),
    )
    op.add_column("pages", sa.Column("redirect_to_url", sa.Text, nullable=True))
    op.add_column("pages", sa.Column("redirect_target_summary", sa.Text, nullable=True))
    op.add_column("pages", sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True))

    # В таблице лежат данные пилотных прогонов. Старая логика помечала удалённой
    # любую страницу, не попавшую в обход, — какая из них удалена на самом деле,
    # мы задним числом не знаем. Ставим "выпала из обхода": так в интерфейсе видно,
    # что вердикт не проверялся, а новые записи получат честную причину.
    op.execute(
        "UPDATE pages SET disappearance_reason = 'missing_from_crawl' "
        "WHERE is_removed = true AND disappearance_reason IS NULL"
    )


def downgrade() -> None:
    op.drop_column("pages", "last_checked_at")
    op.drop_column("pages", "redirect_target_summary")
    op.drop_column("pages", "redirect_to_url")
    op.drop_column("pages", "disappearance_reason")
    sa.Enum(*_REASON_VALUES, name="disappearance_reason").drop(op.get_bind(), checkfirst=True)
