"""create mcp_function_calls table

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-22
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_function_calls",
        sa.Column("partition_key", sa.String(128), nullable=False),
        sa.Column("mcp_function_call_uuid", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("mcp_type", sa.String(), nullable=False),
        sa.Column("arguments", postgresql.JSONB(), nullable=True),
        sa.Column("content_in_s3", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default=sa.text("'initial'")),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("time_spent", sa.Integer(), nullable=True),
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
        sa.PrimaryKeyConstraint("partition_key", "mcp_function_call_uuid"),
    )
    op.create_index(
        "idx_mcp_function_calls_partition_mcp_type",
        "mcp_function_calls",
        ["partition_key", "mcp_type"],
    )
    op.create_index(
        "idx_mcp_function_calls_partition_name",
        "mcp_function_calls",
        ["partition_key", "name"],
    )
    op.create_index(
        "idx_mcp_function_calls_partition_updated_at",
        "mcp_function_calls",
        ["partition_key", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_mcp_function_calls_partition_updated_at", table_name="mcp_function_calls")
    op.drop_index("idx_mcp_function_calls_partition_name", table_name="mcp_function_calls")
    op.drop_index("idx_mcp_function_calls_partition_mcp_type", table_name="mcp_function_calls")
    op.drop_table("mcp_function_calls")