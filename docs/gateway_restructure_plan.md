# MCP Daemon Engine Gateway Restructure Plan

> Reviewed with `../silvaengine_gateway` on 2026-06-15.

## Goal

`mcp_daemon_engine` should no longer host FastAPI, Uvicorn, auth routes, or HTTP
middleware. It should expose MCP business logic and dispatch functions. The
FastAPI delivery layer for SSE, MCP JSON-RPC, GraphQL, REST, auth, rate limiting,
and lifecycle hooks now belongs to `silvaengine_gateway`.

## Current State

The local module has already moved in this direction:

- `handlers/mcp_app.py` is deleted.
- `handlers/middleware.py` is deleted.
- `handlers/auth_router.py` is deleted.
- `handlers/jwt_local.py` and `handlers/jwt_cognito.py` are deleted from this
  package; gateway auth owns JWT verification.
- `pyproject.toml` no longer depends on `fastapi[all]` or `uvicorn[standard]`.
- `main.py` exposes gateway dispatch functions for GraphQL, MCP JSON-RPC, SSE
  POST, async execution, cache refresh/clear, and endpoint info.
- `main.py` does not expose a CLI `main()` function; gateway delivery is the
  runtime entry point.
- `main.py` exposes a SilvaEngine `deploy()` manifest for metadata registration,
  but the manifest is not the HTTP server runtime.
- `handlers/sse_manager.py` remains as an in-process queue manager and exposes a
  gateway shutdown hook.
- `schema.py` moved to the package root, and `MCPDaemonEngine` now lives in
  `main.py` beside the gateway dispatch functions.

The sibling gateway already includes MCP routes in `silvaengine_gateway/routes.yaml`.

## Gateway Delivery

`silvaengine_gateway` is responsible for the HTTP layer:

```text
Client
  -> silvaengine_gateway FastAPI app
     -> /{endpoint_id}/{part_id}/mcp_daemon_graphql
     -> /{endpoint_id}/{part_id}/mcp
     -> /{endpoint_id}/{part_id}/sse          (GET stream)
     -> /{endpoint_id}/{part_id}/sse          (POST JSON-RPC + SSE push)
     -> /{endpoint_id}/{part_id}/mcp_async_execute
     -> /{endpoint_id}/{part_id}/mcp_async/status/{task_id}
     -> /{endpoint_id}/{part_id}/admin/cache/refresh
     -> /{endpoint_id}/{part_id}/admin/cache
     -> /{endpoint_id}/{part_id}/mcp_info
```

The gateway initializes `mcp_daemon_engine.handlers.config:Config` with
`config_init_style: dict`, resolves dispatch functions with importlib, injects
`partition_key`, `endpoint_id`, `part_id`, and authenticated user context, and
streams SSE responses through the configured `sse_manager`.

## Dispatch Functions

The dispatch functions in `mcp_daemon_engine.main` are the stable gateway
contract:

- `dispatch_graphql(**params)`
- `dispatch_mcp(**params)`
- `dispatch_sse_message(**params)`
- `dispatch_mcp_async(**params)`
- `dispatch_cache_refresh(**params)`
- `dispatch_cache_clear(**params)`
- `dispatch_mcp_info(**params)`

This matches the gateway-facing part of `knowledge_graph_engine/main.py`: the
gateway imports module-level dispatch functions from the route manifest. MCP does
not keep the old `AIMCPDaemonEngine` class in `main.py`. It uses
`MCPDaemonEngine` plus a SilvaEngine `deploy()` metadata manifest, while HTTP
delivery remains gateway-owned and the MCP dispatch paths call the underlying
protocol helpers directly.

Important behavior:

- REST and SSE POST callers may send raw JSON-RPC bodies directly. Dispatch code
  strips gateway metadata before calling `process_mcp_message`.
- GraphQL configuration mutations clear the MCP configuration cache for the full
  `partition_key`.
- Async execution calls `execute_tool_function()` directly from the gateway dispatch path.
- Dispatch functions return JSON-serializable objects for the gateway to encode.

## Gateway Fixes Needed

The gateway route support must preserve these details:

- `handler_type: sse` must call a top-level `_make_sse_handler(sse_manager_ref=...)`.
- The route builder should derive `partition_key` from the path
  `{endpoint_id}/{part_id}` and only use `Part-Id` as a compatibility fallback.
- Dispatch exceptions should be allowed to bubble so manifest-registered domain
  exception handlers can map them to HTTP status codes.
- The gateway lifespan should call
  `mcp_daemon_engine.handlers.sse_manager:cleanup_sse`.

## Remaining Validation

Run these checks after installing both packages in the same environment:

```bash
python -m compileall mcp_daemon_engine
python -m silvaengine_gateway.tests.run_daemon
python -m silvaengine_gateway.tests.call_mcp_graphql --query tools
python -m silvaengine_gateway.tests.call_mcp_rest --method initialize
python -m silvaengine_gateway.tests.call_mcp_sse --send initialize --timeout 15
```

Also verify package upload mutations and external MCP proxy execution through
`/{endpoint_id}/{part_id}/mcp_daemon_graphql`, because those flows depend on AWS
configuration and cannot be proven by syntax checks alone.
