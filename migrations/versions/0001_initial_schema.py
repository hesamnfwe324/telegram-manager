"""Initial schema

Revision ID: 0001
Revises:
Create Date: 2024-01-01 00:00:00.000000

"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "groups",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(512), nullable=True),
        sa.Column("username", sa.String(255), nullable=True),
        sa.Column("invite_link", sa.String(1024), nullable=True),
        sa.Column("members_count", sa.Integer(), nullable=True),
        sa.Column("join_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending", "approved", "rejected", "joined", "failed",
                name="group_status",
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id"),
    )
    op.create_index("ix_groups_group_id", "groups", ["group_id"])
    op.create_index("ix_groups_status", "groups", ["status"])
    op.create_index("ix_groups_username", "groups", ["username"])

    op.create_table(
        "discovered_links",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("link", sa.String(1024), nullable=False),
        sa.Column("source", sa.String(512), nullable=True),
        sa.Column("discovered_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending", "approved", "rejected", "joined", "failed",
                name="link_status",
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("link"),
    )
    op.create_index("ix_discovered_links_link", "discovered_links", ["link"])
    op.create_index("ix_discovered_links_status", "discovered_links", ["status"])

    op.create_table(
        "logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("action", sa.String(255), nullable=False),
        sa.Column("result", sa.String(50), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("actor", sa.String(255), nullable=True),
        sa.Column("target", sa.String(512), nullable=True),
        sa.Column("details", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_logs_action", "logs", ["action"])
    op.create_index("ix_logs_timestamp", "logs", ["timestamp"])


def downgrade() -> None:
    op.drop_table("logs")
    op.drop_table("discovered_links")
    op.drop_table("groups")
    op.execute("DROP TYPE IF EXISTS group_status")
    op.execute("DROP TYPE IF EXISTS link_status")
