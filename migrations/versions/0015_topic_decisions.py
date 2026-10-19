"""topic decisions for competitor articles and links to the articles

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-19

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "topic_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("main_intent", sa.Text(), nullable=False),
        sa.Column("material_type", sa.Text(), nullable=True),
        sa.Column("subintents", sa.JSON(), nullable=True),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("coverage", sa.Text(), nullable=True),
        sa.Column("our_url", sa.Text(), nullable=True),
        sa.Column("add_subintents", sa.JSON(), nullable=True),
        sa.Column("argument", sa.Text(), nullable=True),
        sa.Column("cannibal_urls", sa.JSON(), nullable=True),
        sa.Column("raw_response", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "seo_findings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "decision_id",
            sa.Integer(),
            sa.ForeignKey("topic_decisions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("page_id", sa.Integer(), sa.ForeignKey("pages.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "competitor_id", sa.Integer(), sa.ForeignKey("competitors.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "page_change_id", sa.Integer(), sa.ForeignKey("page_changes.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("added_subintents", sa.JSON(), nullable=True),
        sa.Column("detected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_seo_findings_detected_at", "seo_findings", ["detected_at"])
    op.create_index("ix_seo_findings_page_id", "seo_findings", ["page_id"])


def downgrade() -> None:
    op.drop_index("ix_seo_findings_page_id", table_name="seo_findings")
    op.drop_index("ix_seo_findings_detected_at", table_name="seo_findings")
    op.drop_table("seo_findings")
    op.drop_table("topic_decisions")
