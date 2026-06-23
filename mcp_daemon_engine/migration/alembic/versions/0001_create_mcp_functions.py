"""create mcp_functions table

Revision ID: 0001
Revises:
Create Date: 2026-06-22
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_functions",
        sa.Column("partition_key", sa.String(128), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("mcp_type", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("data", postgresql.JSONB(), nullable=True),
        sa.Column("annotations", sa.Text(), nullable=True),
        sa.Column("module_name", sa.String(), nullable=True),
        sa.Column("class_name", sa.String(), nullable=True),
        sa.Column("function_name", sa.String(), nullable=True),
        sa.Column("return_type", sa.String(), nullable=True),
        sa.Column("is_async", sa.Boolean(), nullable=True),
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
        sa.PrimaryKeyConstraint("partition_key", "name"),
    )
    op.create_index(
        "idx_mcp_functions_partition_mcp_type",
        "mcp_functions",
        ["partition_key", "mcp_type"],
    )


def downgrade() -> None:
    op.drop_index("idx_mcp_functions_partition_mcp_type", table_name="mcp_functions")
    op.drop_table("mcp_functions")