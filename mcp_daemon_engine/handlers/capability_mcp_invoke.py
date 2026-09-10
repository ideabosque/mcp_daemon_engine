#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Capability Engine MCP invocation adapter.

Executes a daemon-managed MCP tool by name through the existing runtime
path (`execute_tool_function`), which already validates arguments against
the tool schema, dispatches by source (external/git/s3/local), and writes
the MCPFunctionCall audit record via execute_decorator().

The adapter returns a CapabilityMcpInvocation-shaped dict. It does NOT
reimplement Banyanos authorization/rate-limit/circuit-breaker governance —
those stay in the capability/agent layers until intentionally migrated.
"""
from __future__ import print_function

__author__ = "bibow"

import traceback
from typing import Any, Dict

from silvaengine_utility import Serializer

from .config import Config


def invoke_capability_tool(
    info: Any,
    *,
    name: str,
    arguments: Dict[str, Any] = None,
    agent_id: str = None,
    trace_id: str = None,
    updated_by: str = "capability_compat",
) -> Dict[str, Any]:
    """Invoke one MCP tool and return a CapabilityMcpInvocation-shaped dict.

    Flow:
        1. Look up the tool in the cached MCP configuration (must exist and
           be a tool-type function).
        2. Merge trace_id/agent_id into the request payload under
           ``_capability_context`` so the audit record carries them.
        3. Call ``execute_tool_function(partition_key, name, arguments)`` —
           its execute_decorator() creates/updates MCPFunctionCall with
           content, status, and time_spent.
        4. Fetch the audit record (mcp_function_call_uuid is generated
           inside the runtime path; the decorator's insert happens with the
           arguments containing the uuid if supplied) and build the
           invocation view.
    """
    from .mcp_utility import execute_tool_function

    logger = info.context.get("logger") or Config.logger
    partition_key = info.context.get("partition_key")

    # --- Validate the tool exists ---
    try:
        config = Config.fetch_mcp_configuration(partition_key)
    except Exception as e:
        raise Exception(f"Failed to load MCP configuration: {e}")

    tool = next(
        (t for t in config.get("tools", []) if t.get("name") == name), None
    )
    if tool is None:
        raise Exception(f"Tool not found: {name}")

    # --- Merge capability context into arguments ---
    call_args = dict(arguments or {})
    if agent_id or trace_id:
        call_args["_capability_context"] = {
            **(call_args.get("_capability_context") or {}),
            **({"agent_id": agent_id} if agent_id else {}),
            **({"trace_id": trace_id} if trace_id else {}),
        }

    start = Config.logger and None
    import pendulum

    start_time = pendulum.now("UTC")
    status = "completed"
    error_message = None
    result_payload: Any = None

    try:
        content_items = execute_tool_function(partition_key, name, call_args)

        # Normalize the MCP content list into a payload dict.
        payload_items = []
        for item in content_items or []:
            if hasattr(item, "model_dump"):
                payload_items.append(
                    item.model_dump(mode="json", exclude_none=True)
                )
            else:
                payload_items.append({"type": "text", "text": str(item)})
        result_payload = payload_items[0] if len(payload_items) == 1 else payload_items
    except Exception as e:
        status = "failed"
        error_message = traceback.format_exc()
        logger.error(error_message)
        result_payload = None

    time_spent_ms = int(
        (pendulum.now("UTC") - start_time).total_seconds() * 1000
    )

    # --- Fetch the audit record for the invocation id ---
    invocation_uuid = None
    created_at = None
    updated_at = None
    request_payload = call_args
    try:
        # The most recent audit record for this tool/partition is the one
        # the decorator just wrote.
        from ..models.repositories import get_repo

        result = get_repo("mcp_function_call").list(
            _InfoLike(partition_key), name=name, limit=1
        )
        rows = getattr(result, "mcp_function_call_list", None) or []
        if rows:
            row = rows[0]
            if isinstance(row, dict):
                invocation_uuid = row.get("mcp_function_call_uuid")
                created_at = row.get("created_at")
                updated_at = row.get("updated_at")
                status = row.get("status") or status
                time_spent_ms = row.get("time_spent") or time_spent_ms
                args_row = row.get("arguments")
            else:
                invocation_uuid = getattr(row, "mcp_function_call_uuid", None)
                created_at = getattr(row, "created_at", None)
                updated_at = getattr(row, "updated_at", None)
                status = getattr(row, "status", None) or status
                time_spent_ms = getattr(row, "time_spent", None) or time_spent_ms
                args_row = getattr(row, "arguments", None)
            if isinstance(args_row, str):
                try:
                    args_row = Serializer.json_loads(args_row)
                except Exception:
                    args_row = None
            if isinstance(args_row, dict):
                request_payload = args_row
    except Exception as e:
        logger.warning(f"Failed to read invocation audit record: {e}")

    message = (
        f"Tool '{name}' {status}"
        + (f" in {time_spent_ms}ms" if time_spent_ms else "")
    )
    if status == "failed":
        message = f"Tool '{name}' failed: {error_message.splitlines()[-1] if error_message else 'unknown error'}"

    return {
        "id": invocation_uuid,
        "invocation_id": invocation_uuid,
        "tool_name": name,
        "mcp_type": "tool",
        "status": status,
        "request_payload": request_payload,
        "response_payload": result_payload,
        "trace_id": trace_id,
        "agent_id": agent_id,
        "duration_ms": time_spent_ms,
        "error_message": error_message,
        "created_at": created_at,
        "updated_at": updated_at,
        "message": message,
    }


class _InfoLike:
    """Minimal info-like object for repository list calls."""

    def __init__(self, partition_key: str):
        self.context = {
            "partition_key": partition_key,
            "logger": Config.get_logger(),
        }


__all__ = ["invoke_capability_tool"]