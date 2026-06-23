# mcp_daemon_engine

MCP business-logic package for SilvaEngine. HTTP delivery is handled by
`silvaengine_gateway`; this package no longer hosts FastAPI or Uvicorn.

## Runtime Shape

- Gateway HTTP routes call dispatch functions in `mcp_daemon_engine.main`.
- This follows the gateway-facing pattern used by `knowledge_graph_engine`:
  `MCPDaemonEngine` owns the GraphQL/runtime methods, `deploy()` exposes
  SilvaEngine metadata, and gateway dispatch functions remain module-level.
- This package does not expose a CLI entry point; start `silvaengine_gateway` for
  HTTP/SSE/GraphQL delivery.
- MCP GraphQL schema lives in `mcp_daemon_engine/schema.py`.
- `handlers/sse_manager.py` provides the in-process SSE queue manager used by
  the gateway SSE handler.

## Gateway Dispatch Functions

- `dispatch_graphql(**params)`
- `dispatch_mcp(**params)`
- `dispatch_sse_message(**params)`
- `dispatch_mcp_async(**params)`
- `dispatch_cache_refresh(**params)`
- `dispatch_cache_clear(**params)`
- `dispatch_mcp_info(**params)`

The gateway injects `endpoint_id`, `part_id`, `partition_key`, and
`context.user`. `partition_key` is composed as `endpoint_id#part_id`.

## Runtime

```powershell
python -m silvaengine_gateway
```

`mcp_daemon_engine` is loaded by the gateway route manifest. It should be
initialized through `mcp_daemon_engine.handlers.config:Config` and called through
the dispatch functions above.

## Dual-Backend Persistence

`mcp_daemon_engine` supports two selectable persistence backends for its 4 metadata
entities (`MCPFunction`, `MCPModule`, `MCPSetting`, `MCPFunctionCall`):

- **DynamoDB** (`db_backend="dynamodb"`, default) — PynamoDB models under `models/dynamodb/`.
- **PostgreSQL** (`db_backend="postgresql"`) — SQLAlchemy models under `models/postgresql/`,
  Alembic migrations under `migration/alembic/`.

A repository dispatch boundary at `models/repositories/` isolates GraphQL queries,
mutations, and the configuration-loading handler from backend-specific persistence
details. All metadata persistence routes through `get_repo(entity_type)`.

**S3 (package upload + content offload), the MCP runtime (SSE/stdio/JSON-RPC), the
external-MCP proxy, and dynamic tool-module loading are NOT backend-selectable.**
They work identically under both `DB_BACKEND` values. S3 stays initialized even in
PostgreSQL mode when `funct_bucket_name` is set.

See:
- `docs/DUAL_BACKEND_DEVELOPMENT_PLAN.md` — full development plan and phase status.
- `docs/DUAL_BACKEND_CONFIG.md` — backend selection and configuration guide.
- `docs/POSTGRESQL_SETUP.md` — PostgreSQL setup and migration guide.
- `docs/PHASE0_ENTITY_INVENTORY.md` — per-field DynamoDB→PostgreSQL type mapping.
