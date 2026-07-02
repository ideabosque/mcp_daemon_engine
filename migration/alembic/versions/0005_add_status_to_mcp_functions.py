"""replace enabled boolean with status smallint on mcp_functions

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-25
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add status column (SmallInteger, default 1=enabled)
    op.add_column(
        "mcp_functions",
        sa.Column("status", sa.SmallInteger(), nullable=False, server_default=sa.text("1")),
    )
    # Drop the old enabled column
    op.drop_column("mcp_functions", "enabled")


def downgrade() -> None:
    # Re-add enabled column
    op.add_column(
        "mcp_functions",
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    # Drop status column
    op.drop_column("mcp_functions", "status")