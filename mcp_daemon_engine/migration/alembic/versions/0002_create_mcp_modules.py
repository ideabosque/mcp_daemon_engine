"""create mcp_modules table

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-22
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_modules",
        sa.Column("partition_key", sa.String(128), nullable=False),
        sa.Column("module_name", sa.String(), nullable=False),
        sa.Column("package_name", sa.String(), nullable=False),
        sa.Column("classes", postgresql.JSONB(), nullable=True),
        sa.Column("source", sa.String(), nullable=True),
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
        sa.PrimaryKeyConstraint("partition_key", "module_name"),
    )
    op.create_index(
        "idx_mcp_modules_partition_package_name",
        "mcp_modules",
        ["partition_key", "package_name"],
    )


def downgrade() -> None:
    op.drop_index("idx_mcp_modules_partition_package_name", table_name="mcp_modules")
    op.drop_table("mcp_modules")