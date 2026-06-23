# PostgreSQL Setup Guide

> How to set up and run `mcp_daemon_engine` with the PostgreSQL backend.

## Prerequisites

1. PostgreSQL 13+ (or compatible RDS/Aurora instance).
2. A database schema created for the daemon.
3. Python 3.11+.

## Installation

```bash
pip install mcp-daemon-engine[postgresql]
```

This installs `SQLAlchemy>=1.4`, `psycopg2-binary>=2.9`, and `alembic>=1.10`
in addition to the core dependencies.

## Database Setup

### Option A: Alembic Migrations (recommended for production)

```bash
# Set the database URL
export DATABASE_URL="postgresql+psycopg2://user:password@localhost:5432/mcp_daemon_engine"

# Run migrations
cd /path/to/mcp_daemon_engine
alembic -c mcp_daemon_engine/migration/alembic.ini upgrade head
```

Migrations create 4 tables in order:
1. `0001_create_mcp_functions` — `mcp_functions`
2. `0002_create_mcp_modules` — `mcp_modules`
3. `0003_create_mcp_settings` — `mcp_settings`
4. `0004_create_mcp_function_calls` — `mcp_function_calls`

### Option B: Auto-create on startup

Set `initialize_tables: true` in the configuration setting. The daemon will run
`Base.metadata.create_all(checkfirst=True)` on startup, creating any missing tables.

This is suitable for development but not recommended for production (no migration history).

## Configuration

```python
setting = {
    "db_backend": "postgresql",
    "db_host": "localhost",
    "db_port": 5432,
    "db_user": "mcp_daemon",
    "db_password": "your_password",
    "db_schema": "mcp_daemon_engine",
    "initialize_tables": True,  # optional: auto-create tables
    # S3 is still needed for package uploads + content offload:
    "funct_bucket_name": "your-bucket-name",
    "region_name": "us-east-1",  # optional in PG mode for S3 (falls back to default chain)
    "aws_access_key_id": "...",  # optional in PG mode
    "aws_secret_access_key": "...",  # optional in PG mode
    # Other settings (transport, auth, etc.) are the same as DynamoDB mode.
    "transport": "sse",
    "port": 8000,
    "auth_provider": "local",
    "jwt_secret_key": "your_secret",
}
```

## Verification

After starting the daemon, verify the backend is active:

```python
from mcp_daemon_engine.handlers.config import Config
print(f"DB_BACKEND: {Config.DB_BACKEND}")  # should print "postgresql"
print(f"db_session: {Config.db_session}")  # should be a scoped_session
```

## Backup and Recovery

- Use `pg_dump` for full database backups.
- Each table is partitioned by `partition_key` (tenant key), enabling per-tenant
  data export if needed.
- No DynamoDB→PostgreSQL data migration is provided (no production DynamoDB data
  to migrate — both backends start empty).

## Troubleshooting

### `ImportError: SQLAlchemy is required for PostgreSQL backend`

Install the optional dependency: `pip install mcp-daemon-engine[postgresql]`

### `KeyError: No repository registered for entity '...'`

Ensure `DB_BACKEND` is set to `"postgresql"` in the setting dict and that
`Config.initialize()` has been called. The PG repos are lazily registered on
first `get_repo()` call.