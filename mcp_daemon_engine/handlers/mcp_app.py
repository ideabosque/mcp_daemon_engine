#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

__author__ = "bibow"

import asyncio
import json
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, Tuple

import pendulum
from fastapi import Depends, FastAPI, Header, HTTPException, Request, params
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from silvaengine_utility.serializer import Serializer

from .config import Config
from .mcp_server import list_prompts, list_resources, list_tools, process_mcp_message
from .sse_manager import sse_manager

# === Rate Limiting ===
request_counts = defaultdict(list)


def _get_partition_key(endpoint_id: str, request: Request) -> Tuple[str, str | None]:
    """Construct partition key from endpoint_id and optional part_id"""
    part_id = request.headers.get("Part-ID")
    if part_id:
        return f"{endpoint_id}#{part_id}", part_id
    return endpoint_id, None


# === Application Lifecycle Events ===
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan - startup and shutdown events"""
    # Startup
    if Config.logger:
        Config.logger.info("Starting up MCP SSE Server...")

    yield

    # Shutdown
    if Config.logger:
        Config.logger.info("Shutting down application, cleaning up resources...")

    # Cleanup SSE manager
    await sse_manager.cleanup_all()

    # Cleanup HTTP client if using Cognito auth
    if Config.auth_provider == "cognito":
        try:
            from .jwt_cognito import cleanup_http_client

            await cleanup_http_client()
            if Config.logger:
                Config.logger.info("HTTP client cleaned up successfully")
        except Exception as e:
            if Config.logger:
                Config.logger.error(f"Error cleaning up HTTP client: {e}")


# === FastAPI and MCP Initialization ===
app = FastAPI(title="MCP SSE Server", lifespan=lifespan)

# Add CORS with more restrictive settings
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: Replace with specific allowed origins in production
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def rate_limit_check(client_ip: str, max_requests: int = 100, window_seconds: int = 60):
    """Check if client has exceeded rate limit"""
    now = time.time()
    # Clean old requests
    request_counts[client_ip] = [
        req_time
        for req_time in request_counts[client_ip]
        if now - req_time < window_seconds
    ]

    if len(request_counts[client_ip]) >= max_requests:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    request_counts[client_ip].append(now)


# === SSE Event Generator ===
async def sse_event_generator(
    request: Request, client_id: int, username: str, queue: asyncio.Queue
) -> AsyncGenerator[str, None]:
    """Generate SSE events for connected clients with better error handling"""
    try:
        # Send connection event
        yield f"event: connected\ndata: {
            json.dumps(
                {'client_id': client_id, 'timestamp': pendulum.now('UTC').isoformat()}
            )
        }\n\n"

        while not await request.is_disconnected():
            try:
                message = await asyncio.wait_for(queue.get(), timeout=15)
                data = json.dumps(jsonable_encoder(message))
                yield f"data: {data}\n\n"
            except asyncio.TimeoutError:
                # Send heartbeat
                heartbeat = json.dumps(
                    {
                        "client_id": client_id,
                        "timestamp": pendulum.now("UTC").isoformat(),
                        "type": "heartbeat",
                    }
                )
                yield f"event: heartbeat\ndata: {heartbeat}\n\n"
            except Exception as e:
                if Config.logger:
                    Config.logger.error(
                        f"Error in SSE generator for client {client_id}: {e}"
                    )
                break

    except asyncio.CancelledError:
        if Config.logger:
            Config.logger.info(f"SSE generator cancelled for client {client_id}")
    except Exception as e:
        if Config.logger:
            Config.logger.error(
                f"Fatal error in SSE generator for client {client_id}: {e}"
            )
    finally:
        # Cleanup
        await sse_manager.remove_client(client_id, username)


# === Broadcast Logic ===
async def broadcast_to_clients(message: Dict) -> int:
    """Send an event to all connected clients and return success count"""
    return await sse_manager.broadcast_message(message)


async def send_to_client(cid: int, message: Dict[str, Any]) -> bool:
    """Unicast a message to one client"""
    return await sse_manager.send_to_client(cid, message)


async def send_to_user(username: str, message: dict[str, Any]) -> bool:
    """Send a message to all live connections for a user."""
    return await sse_manager.send_to_user(username, message)


def current_user(request: Request) -> Dict:
    """Get current authenticated user"""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


@app.get("/me")
def me(user: Dict = Depends(current_user)) -> Dict:
    """Get current user info"""
    return user


# === GET /sse Endpoint ===
@app.get("/{endpoint_id}/sse")
async def get_sse_stream(
    endpoint_id: str,
    request: Request,
    user: Dict = Depends(current_user),
    origin: str = Header(None),
) -> StreamingResponse:
    """Handle SSE stream connections with improved security and error handling"""
    # Validate endpoint_id
    if not endpoint_id or not endpoint_id.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid endpoint_id")

    # TODO: Uncomment and configure for production
    # allowed_origins = ["https://your-allowed-domain.com"]
    # if origin and origin not in allowed_origins:
    #     raise HTTPException(status_code=403, detail="Forbidden origin")

    # Rate limiting
    client_ip = request.client.host
    rate_limit_check(client_ip, max_requests=50, window_seconds=60)

    # Create new client with SSE manager
    client_id, queue = await sse_manager.add_client(user["username"])

    # Handle message replay
    last_event_id = request.headers.get("last-event-id")
    missed_messages = await sse_manager.get_missed_messages(last_event_id)
    for msg in missed_messages:
        try:
            await queue.put(msg)
        except asyncio.QueueFull:
            if Config.logger:
                Config.logger.warning(
                    f"Queue full during replay for client {client_id}"
                )
            break

    # Send initialization metadata
    metadata = {
        "type": "mcp_activity",
        "method": "initialize",
        "response": {
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                    "prompts": {"listChanged": False},
                },
                "serverInfo": {"name": "MCP SSE Server", "version": "1.0.0"},
            }
        },
    }
    try:
        await queue.put(metadata)
    except asyncio.QueueFull:
        await sse_manager.remove_client(client_id, user["username"])
        raise HTTPException(status_code=503, detail="Server too busy")

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",  # Disable nginx buffering
    }
    return StreamingResponse(
        sse_event_generator(request, client_id, user["username"], queue),
        media_type="text/event-stream",
        headers=headers,
    )


@app.post("/{endpoint_id}/sse")
async def post_sse_message(
    endpoint_id: str, request: Request, user: Dict = Depends(current_user)
) -> Dict:
    """Handle MCP protocol messages with improved validation and error handling"""
    # Rate limiting
    client_ip = request.client.host
    rate_limit_check(client_ip)

    # Validate endpoint_id
    if not endpoint_id or not endpoint_id.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid endpoint_id")

    message = None
    try:
        partition_key, part_id = _get_partition_key(endpoint_id, request)
        message = await request.json()

        # Validate message structure
        if not isinstance(message, dict) or "method" not in message:
            raise HTTPException(status_code=400, detail="Invalid message format")

        response = await process_mcp_message(partition_key, message)

        # Send to user clients
        delivered = await send_to_user(
            user["username"],
            {
                "type": "mcp_activity",
                "method": message["method"],
                "request": jsonable_encoder(message),
                "response": jsonable_encoder(response),
                "timestamp": pendulum.now("UTC").isoformat(),
            },
        )

        if not delivered and Config.logger:
            Config.logger.warning(
                f"Failed to deliver message to user {user['username']}"
            )

        return jsonable_encoder(response)

    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    except HTTPException:
        raise
    except Exception as e:
        if Config.logger:
            Config.logger.error(f"Error processing SSE message: {e}")
        return {
            "jsonrpc": "2.0",
            "id": getattr(message, "id", None) if message else None,
            "error": {"code": -32603, "message": "Internal error", "data": str(e)},
        }


@app.post("/{endpoint_id}/mcp")
async def post_mcp_message(
    endpoint_id: str, request: Request, user: Dict = Depends(current_user)
) -> Dict:
    """Handle MCP protocol messages with validation"""
    # Rate limiting
    client_ip = request.client.host
    rate_limit_check(client_ip)

    # Validate endpoint_id
    if not endpoint_id or not endpoint_id.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid endpoint_id")

    message = None
    try:
        partition_key, part_id = _get_partition_key(endpoint_id, request)

        message = await request.json()

        # Validate message structure
        if not isinstance(message, dict):
            raise HTTPException(status_code=400, detail="Invalid message format")

        response = await process_mcp_message(partition_key, message)
        return jsonable_encoder(response)

    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    except HTTPException:
        raise
    except Exception as e:
        if Config.logger:
            Config.logger.error(f"Error processing MCP message: {e}")
        return {
            "jsonrpc": "2.0",
            "id": getattr(message, "id", None) if message else None,
            "error": {"code": -32603, "message": "Internal error", "data": str(e)},
        }


# === Diagnostics ===
@app.get("/health")
async def health_check() -> Dict[str, Any]:
    """Check server health status"""
    stats = await sse_manager.get_stats()
    return {
        "status": "healthy",
        "timestamp": pendulum.now("UTC").isoformat(),
        "sse_stats": stats,
    }


@app.get("/metrics")
async def get_metrics() -> Dict[str, Any]:
    """Get detailed server metrics"""
    stats = await sse_manager.get_stats()
    return {
        "timestamp": pendulum.now("UTC").isoformat(),
        "sse_manager": stats,
        "rate_limiting": {
            "active_ips": len(request_counts),
            "total_tracked_requests": sum(
                len(reqs) for reqs in request_counts.values()
            ),
        },
        "mcp_cache": {
            "cached_partitions": list(Config.mcp_configuration.keys()),
            "cache_size": len(Config.mcp_configuration),
        },
    }


# === Admin Cache Management Endpoints ===
@app.post("/{endpoint_id}/admin/cache/refresh")
async def refresh_mcp_cache(
    endpoint_id: str, request: Request, user: Dict = Depends(current_user)
) -> Dict[str, Any]:
    """Refresh MCP configuration cache for a specific endpoint"""
    # Validate endpoint_id
    if not endpoint_id or not endpoint_id.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid endpoint_id")

    try:
        partition_key, part_id = _get_partition_key(endpoint_id, request)

        # Force refresh the configuration
        config = Config.refresh_mcp_configuration(partition_key)

        return {
            "status": "success",
            "message": f"Cache refreshed for partition: {partition_key}",
            "timestamp": pendulum.now("UTC").isoformat(),
            "cache_stats": {
                "partition_key": partition_key,
                "tools_count": len(config.get("tools", [])),
                "resources_count": len(config.get("resources", [])),
                "prompts_count": len(config.get("prompts", [])),
                "modules_count": len(config.get("modules", [])),
            },
        }
    except Exception as e:
        if Config.logger:
            Config.logger.error(f"Failed to refresh cache for {partition_key}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to refresh cache: {str(e)}"
        )


@app.delete("/{endpoint_id}/admin/cache")
async def clear_endpoint_cache(
    endpoint_id: str, request: Request, user: Dict = Depends(current_user)
) -> Dict[str, Any]:
    """Clear MCP configuration cache for a specific endpoint"""
    # Validate endpoint_id
    if not endpoint_id or not endpoint_id.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid endpoint_id")

    partition_key, part_id = _get_partition_key(endpoint_id, request)

    Config.clear_mcp_configuration_cache(partition_key)

    return {
        "status": "success",
        "message": f"Cache cleared for partition: {partition_key}",
        "timestamp": pendulum.now("UTC").isoformat(),
    }


@app.delete("/admin/cache")
async def clear_all_cache(user: Dict = Depends(current_user)) -> Dict[str, Any]:
    """Clear MCP configuration cache for all endpoints"""
    cached_partitions = list(Config.mcp_configuration.keys())
    Config.clear_mcp_configuration_cache()

    return {
        "status": "success",
        "message": "All MCP configuration cache cleared",
        "timestamp": pendulum.now("UTC").isoformat(),
        "cleared_partitions": cached_partitions,
    }


@app.get("/{endpoint_id}/admin/cache/status")
async def get_cache_status(
    endpoint_id: str, request: Request, user: Dict = Depends(current_user)
) -> Dict[str, Any]:
    """Get cache status for a specific endpoint"""
    # Validate endpoint_id
    if not endpoint_id or not endpoint_id.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid endpoint_id")

    partition_key, part_id = _get_partition_key(endpoint_id, request)

    is_cached = partition_key in Config.mcp_configuration
    config = Config.mcp_configuration.get(partition_key, {})

    return {
        "partition_key": partition_key,
        "is_cached": is_cached,
        "timestamp": pendulum.now("UTC").isoformat(),
        "cache_info": (
            {
                "tools_count": len(config.get("tools", [])),
                "resources_count": len(config.get("resources", [])),
                "prompts_count": len(config.get("prompts", [])),
                "modules_count": len(config.get("modules", [])),
                "module_links_count": len(config.get("module_links", [])),
            }
            if is_cached
            else None
        ),
    }


@app.get("/{endpoint_id}")
async def root(endpoint_id: str, request: Request) -> Dict[str, Any]:
    """Get endpoint info including tools, resources and prompts"""
    # Validate endpoint_id
    if not endpoint_id or not endpoint_id.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid endpoint_id")

    try:
        partition_key, part_id = _get_partition_key(endpoint_id, request)

        tools = await list_tools(partition_key)
        resources = await list_resources(partition_key)
        prompts = await list_prompts(partition_key)
        stats = await sse_manager.get_stats()

        return {
            "server": "MCP SSE Server",
            "version": "1.0.0",
            "partition_key": partition_key,
            "sse_stats": stats,
            "tools": jsonable_encoder(tools),
            "resources": jsonable_encoder(resources),
            "prompts": jsonable_encoder(prompts),
        }
    except Exception as e:
        if Config.logger:
            Config.logger.error(f"Error getting endpoint info for {endpoint_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# === GraphQL Endpoint ===
@app.post("/{endpoint_id}/mcp_core_graphql")
async def mcp_core_graphql(endpoint_id: str, request: Request) -> Dict:
    """Handle GraphQL queries with automatic cache invalidation"""
    params = await request.json()
    partition_key, part_id = _get_partition_key(endpoint_id, request)

    if not params.get("context"):
        params["context"] = {}

    params["context"] = {
        "partition_key": partition_key,
        "part_id": part_id,
    }
    params["part_id"] = part_id
    params["partition_key"] = partition_key

    # Check if this is a mutation that modifies MCP configuration
    query = params.get("query", "")
    is_config_mutation = any(
        mutation_name in query
        for mutation_name in [
            "insertUpdateMcpFunction",
            "deleteMcpFunction",
            "insertUpdateMcpModule",
            "deleteMcpModule",
            "insertUpdateMcpSetting",
            "deleteMcpSetting",
        ]
    )

    # Execute the GraphQL query
    response = Config.mcp_core.mcp_core_graphql(**params)
    result = Serializer.json_loads(response.get("body", response))

    # If it was a successful configuration mutation, clear the cache
    if is_config_mutation and "errors" not in result:
        try:
            Config.clear_mcp_configuration_cache(endpoint_id)

            if Config.logger:
                Config.logger.info(
                    f"Cleared MCP configuration cache for {endpoint_id} after mutation"
                )
        except Exception as e:
            if Config.logger:
                Config.logger.warning(f"Failed to clear cache after mutation: {e}")

    return result
