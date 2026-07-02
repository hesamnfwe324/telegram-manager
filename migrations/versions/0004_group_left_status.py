"""Add 'left' status to group_status enum

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE cannot run inside the transaction Alembic
    # normally wraps migrations in, so we commit first and run it in
    # autocommit mode.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE group_status ADD VALUE IF NOT EXISTS 'left'")


def downgrade() -> None:
    # Postgres does not support removing enum values directly. Groups with
    # status='left' would need to be reassigned before a real downgrade.
    pass
