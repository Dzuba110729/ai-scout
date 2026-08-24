"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-08-10

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    session_status = sa.Enum("active", "needs_session", "paused", name="session_status")
    change_type = sa.Enum("new", "changed", "removed", name="change_type")
    notification_status = sa.Enum("sent", "failed", name="notification_status")

    op.create_table(
        "competitors",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("base_url", sa.Text, nullable=False),
        sa.Column("status", session_status, nullable=False, server_default="needs_session"),
        sa.Column("storage_state_path", sa.Text, nullable=True),
        sa.Column("session_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_paused", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "pages",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "competitor_id", sa.Integer, sa.ForeignKey("competitors.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("is_removed", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("competitor_id", "url", name="uq_pages_competitor_url"),
    )

    op.create_table(
        "page_snapshots",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("page_id", sa.Integer, sa.ForeignKey("pages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.Column("text_content", sa.Text, nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_page_snapshots_page_id", "page_snapshots", ["page_id"])

    op.create_table(
        "page_changes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("page_id", sa.Integer, sa.ForeignKey("pages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("change_type", change_type, nullable=False),
        sa.Column(
            "old_snapshot_id", sa.Integer, sa.ForeignKey("page_snapshots.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column(
            "new_snapshot_id", sa.Integer, sa.ForeignKey("page_snapshots.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("diff_text", sa.Text, nullable=True),
        sa.Column("detected_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_page_changes_page_id", "page_changes", ["page_id"])

    op.create_table(
        "ai_analysis",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "page_change_id",
            sa.Integer,
            sa.ForeignKey("page_changes.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("category", sa.Text, nullable=True),
        sa.Column("usp", sa.Text, nullable=True),
        sa.Column("cta", sa.Text, nullable=True),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column("raw_response", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "notifications_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "page_change_id", sa.Integer, sa.ForeignKey("page_changes.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column(
            "competitor_id", sa.Integer, sa.ForeignKey("competitors.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("channel", sa.Text, nullable=False, server_default="telegram"),
        sa.Column("status", notification_status, nullable=False),
        sa.Column("message", sa.Text, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("notifications_log")
    op.drop_table("ai_analysis")
    op.drop_table("page_changes")
    op.drop_table("page_snapshots")
    op.drop_table("pages")
    op.drop_table("competitors")
    sa.Enum(name="notification_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="change_type").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="session_status").drop(op.get_bind(), checkfirst=True)
