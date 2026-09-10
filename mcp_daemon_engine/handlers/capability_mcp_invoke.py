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
    import pendulum

    from .mcp_utility import (
        _check_existing_function_call,
        _insert_update_mcp_function_call,
        execute_tool_function,
    )

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

    # --- Pre-create the audit row so we own its UUID ---
    # Without this, the post-execute "latest by name/partition" lookup was
    # racy: two concurrent invocations of the same tool would scramble each
    # other's rows. execute_decorator finds the pre-created row via
    # _check_existing_function_call(uuid) and updates it in place, so no
    # second row is created and no ambiguity remains.
    invocation_uuid = None
    created_at = None
    updated_at = None
    try:
        pre_row = _insert_update_mcp_function_call(
            partition_key,
            name=name,
            mcp_type="tool",
            arguments=call_args,
        )
        invocation_uuid = (
            pre_row.get("mcpFunctionCallUuid")
            if isinstance(pre_row, dict)
            else None
        )
        created_at = (
            pre_row.get("createdAt") if isinstance(pre_row, dict) else None
        )
    except Exception as e:
        logger.warning(
            f"Failed to pre-create invocation audit row for '{name}': {e} "
            f"— falling back to a best-effort post-hoc lookup"
        )

    start_time = pendulum.now("UTC")
    status = "completed"
    error_message = None
    result_payload: Any = None

    try:
        # execute_tool_function may internally call async code (e.g.
        # ExternalMCPProxy uses MCPHttpClient which is async). When called
        # from a sync GraphQL resolver inside uvicorn's running event loop,
        # Invoker.sync_call_async_compatible fails with "no running event
        # loop" / "already running" conflicts. Dispatch in a separate
        # thread so the async code gets its own clean event loop — same
        # pattern as async_execute_tool_function in mcp_utility.py.
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                execute_tool_function,
                partition_key,
                name,
                call_args,
                mcp_function_call_uuid=invocation_uuid,
            )
            content_items = future.result(timeout=300)

        # Normalize the MCP content list into a payload dict.
        payload_items = []
        for item in content_items or []:
            if hasattr(item, "model_dump"):
                payload_items.append(
                    item.model_dump(mode="json", exclude_none=True)
                )
            else:
                payload_items.append({"type": "text", "text": str(item)})
        result_payload = (
            payload_items[0] if len(payload_items) == 1 else payload_items
        )
    except Exception as e:
        status = "failed"
        error_message = traceback.format_exc()
        logger.error(error_message)
        result_payload = None

    time_spent_ms = int(
        (pendulum.now("UTC") - start_time).total_seconds() * 1000
    )

    # --- Fetch the final state of the audit row by UUID (deterministic) ---
    request_payload = call_args
    if invocation_uuid:
        try:
            row = _check_existing_function_call(partition_key, invocation_uuid)
            if row:
                created_at = row.get("createdAt") or created_at
                updated_at = row.get("updatedAt")
                status = row.get("status") or status
                time_spent_ms = row.get("timeSpent") or time_spent_ms
                args_row = row.get("arguments")
                if isinstance(args_row, str):
                    try:
                        args_row = Serializer.json_loads(args_row)
                    except Exception:
                        args_row = None
                if isinstance(args_row, dict):
                    request_payload = args_row
        except Exception as e:
            logger.warning(
                f"Failed to fetch final audit row {invocation_uuid!r}: {e}"
            )

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


__all__ = ["invoke_capability_tool"]