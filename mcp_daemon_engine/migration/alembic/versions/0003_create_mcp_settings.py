"""create mcp_settings table

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-22
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_settings",
        sa.Column("partition_key", sa.String(128), nullable=False),
        sa.Column("setting_id", sa.String(), nullable=False),
        sa.Column("setting", postgresql.JSONB(), nullable=True),
        sa.Column("updated_by", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("partition_key", "setting_id"),
    )


def downgrade() -> None:
    op.drop_table("mcp_settings")