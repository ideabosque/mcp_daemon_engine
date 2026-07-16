"""Enable Row-Level Security policies on all partition-keyed tables.

All four MCP tables carry a ``partition_key`` column, so each gets a
tenant-isolation policy. Table names are literal (mcp_daemon_engine does not
use a table prefix).

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-16
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

RLS_TABLES = [
    "mcp_functions",
    "mcp_modules",
    "mcp_settings",
    "mcp_function_calls",
]


def upgrade() -> None:
    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (partition_key = current_setting('app.tenant_id', true))"
        )


def downgrade() -> None:
    for table in RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
