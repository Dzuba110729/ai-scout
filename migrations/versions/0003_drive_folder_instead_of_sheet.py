"""google_sheet_* -> google_drive_folder_* on competitor

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-10

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("competitors", "google_sheet_id", new_column_name="google_drive_folder_id")
    op.alter_column("competitors", "google_sheet_url", new_column_name="google_drive_folder_url")


def downgrade() -> None:
    op.alter_column("competitors", "google_drive_folder_id", new_column_name="google_sheet_id")
    op.alter_column("competitors", "google_drive_folder_url", new_column_name="google_sheet_url")
