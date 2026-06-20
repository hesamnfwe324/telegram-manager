"""Add join_attempts table and performance indexes

Revision ID: 0003
Revises: 0002
Create Date: 2024-01-03 00:00:00.000000

"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # join_attempts table
    op.create_table(
        "join_attempts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("invite_link", sa.String(1024), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("attempted_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_join_attempts_group_id", "join_attempts", ["group_id"])
    op.create_index("ix_join_attempts_attempted_at", "join_attempts", ["attempted_at"])

    # Additional performance indexes
    op.create_index("ix_logs_result", "logs", ["result"])
    op.create_index("ix_groups_updated_at", "groups", ["updated_at"])
    op.create_index("ix_groups_created_at", "groups", ["created_at"])
    op.create_index("ix_contacted_users_last_seen_at", "contacted_users", ["last_seen_at"])


def downgrade() -> None:
    op.drop_index("ix_contacted_users_last_seen_at", "contacted_users")
    op.drop_index("ix_groups_created_at", "groups")
    op.drop_index("ix_groups_updated_at", "groups")
    op.drop_index("ix_logs_result", "logs")
    op.drop_index("ix_join_attempts_attempted_at", "join_attempts")
    op.drop_index("ix_join_attempts_group_id", "join_attempts")
    op.drop_table("join_attempts")
