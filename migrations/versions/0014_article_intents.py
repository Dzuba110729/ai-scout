"""intent cards of info articles

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-19

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "article_intents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "page_id", sa.Integer(), sa.ForeignKey("pages.id", ondelete="CASCADE"), nullable=False, unique=True
        ),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("main_intent", sa.Text(), nullable=False),
        sa.Column("subintents", sa.JSON(), nullable=True),
        sa.Column("entities", sa.JSON(), nullable=True),
        sa.Column("relations", sa.JSON(), nullable=True),
        sa.Column("material_type", sa.Text(), nullable=True),
        sa.Column("raw_response", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("article_intents")
