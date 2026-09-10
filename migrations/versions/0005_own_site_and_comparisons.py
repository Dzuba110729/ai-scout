"""own site flag + comparison of competitor findings with our own site

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-10

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_VERDICT_VALUES = ("exact", "similar", "none")


def upgrade() -> None:
    # В таблице лежат реальные конкуренты: колонку добавляем со значением по
    # умолчанию, чтобы существующие строки не стали "нашим сайтом" и не упали на NOT NULL.
    op.add_column(
        "competitors",
        sa.Column("is_own", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    # Наш сайт может быть только один — частичный уникальный индекс не даёт
    # завести второй, даже если что-то пойдёт не так в коде.
    op.create_index(
        "uq_competitors_single_own_site",
        "competitors",
        ["is_own"],
        unique=True,
        postgresql_where=sa.text("is_own"),
    )

    # Тип создаётся самим create_table (в отличие от add_column в 0004, где его
    # приходилось создавать вручную) — второй раз вызывать create() нельзя.
    verdict = sa.Enum(*_VERDICT_VALUES, name="comparison_verdict")

    op.create_table(
        "own_site_comparisons",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "page_change_id",
            sa.Integer(),
            sa.ForeignKey("page_changes.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("verdict", verdict, nullable=False),
        sa.Column("our_page_url", sa.Text(), nullable=True),
        sa.Column("differences", sa.Text(), nullable=True),
        sa.Column("missing", sa.Text(), nullable=True),
        sa.Column("raw_response", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("own_site_comparisons")
    sa.Enum(*_VERDICT_VALUES, name="comparison_verdict").drop(op.get_bind(), checkfirst=True)
    op.drop_index("uq_competitors_single_own_site", table_name="competitors")
    op.drop_column("competitors", "is_own")
