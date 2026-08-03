#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP low-level server integration.

Two dispatch paths coexist:

1. **HTTP transport** (silvaengine_gateway → ``dispatch_mcp`` → ``mcp()``):
   the request payload is handed to ``process_mcp_message()`` which invokes
   the plain async handler functions below (``list_tools``, ``call_tool``,
   ``list_resources``, ``read_resource``, ``list_prompts``, ``get_prompt``)
   with the real ``partition_key`` from the URL. The SDK ``Server`` object
   is not used to dispatch — HTTP mode works regardless of SDK version.

2. **stdio transport** (``run_stdio`` → ``server.run(...)``): the SDK's own
   dispatcher calls the handlers registered on the ``Server`` object. As of
   MCP SDK **v2** these are registered via constructor kwargs (``on_list_tools``,
   ``on_call_tool``, …) rather than the removed v1 ``@server.list_tools()``
   decorators.

The v2 adapters at the bottom of this module wrap the plain handlers into the
``(ctx, params) -> Result`` shape the v2 SDK expects and are passed to the
``Server`` constructor. If any adapter kwarg is rejected (older SDK, renamed
kwarg), we fall back to a bare ``Server(name=…)`` — HTTP dispatch remains
functional; only stdio-mode dispatch is degraded, with a warning logged.
"""
from __future__ import annotations

import logging
import sys
import traceback
from typing import Any, Dict, List, Optional, Sequence, Union

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    EmbeddedResource,
    GetPromptResult,
    ImageContent,
    Prompt,
    PromptArgument,
    Resource,
    TextContent,
    Tool,
)
from silvaengine_utility import Debugger

from .mcp_utility import (
    async_execute_tool_function,
    execute_prompt_function,
    execute_resource_function,
    execute_tool_function,
    get_mcp_configuration_with_retry,
)

_log = logging.getLogger(__name__)


def _serialize_resource_result(result: Any, uri: str) -> List[Dict[str, Any]]:
    if isinstance(result, dict):
        contents = result.get("contents")
    elif hasattr(result, "model_dump"):
        contents = result.model_dump(mode="json", exclude_none=True).get("contents")
    else:
        contents = None

    if isinstance(contents, list):
        return [content for content in contents if isinstance(content, dict)]

    return [{"uri": uri, "mimeType": "text/plain", "text": str(result), "_meta": {}}]


# === Plain handlers — called directly by process_mcp_message (HTTP mode) ===
# The v2 adapters at the bottom of the file wrap these for stdio-mode dispatch.


async def list_tools(partition_key: str = "default") -> List[Tool]:
    """List available tools for the given endpoint"""
    config = get_mcp_configuration_with_retry(partition_key)

    if isinstance(config, dict) and "tools" in config:
        tools = config.get("tools", [])

        if isinstance(tools, list):
            return [
                Tool(**tool)
                for tool in tools
                if isinstance(tool, dict) and "inputSchema" in tool
            ]
    return []


async def call_tool(
    name: str,
    arguments: Optional[Dict[str, Any]],
    partition_key: str = "default",
) -> Sequence[Union[TextContent, ImageContent, EmbeddedResource]]:
    """Call a specific tool with given arguments"""
    config = get_mcp_configuration_with_retry(partition_key)
    name = str(name).strip()

    if (
        not isinstance(config, dict)
        or not isinstance(config.get("tools"), list)
        or not any(tool.get("name") == name for tool in config.get("tools", []))
    ):
        raise ValueError(f"Unknown tool: {name}")

    module_link = next(
        (
            module_link
            for module_link in config.get("module_links", [])
            if module_link.get("name") == name and module_link.get("type") == "tool"
        ),
        {},
    )

    if module_link.get("is_async", False):
        if partition_key == "default":
            raise ValueError(
                "Async tools are not supported with default partition_key - please provide a specific partition_key"
            )

        return async_execute_tool_function(partition_key, name, arguments)

    return execute_tool_function(partition_key, name, arguments)


async def list_resources(partition_key: str = "default") -> List[Resource]:
    """List available resources for the given endpoint"""
    config = get_mcp_configuration_with_retry(partition_key)
    resources = config.get("resources", []) if isinstance(config, dict) else []
    return [
        Resource(**resource)
        for resource in resources
        if isinstance(resource, dict) and resource.get("uri") and resource.get("name")
    ]


async def read_resource(uri: str, partition_key: str = "default") -> Any:
    """Read content of a specific resource"""
    config = get_mcp_configuration_with_retry(partition_key)
    uri = str(uri).strip()

    if (
        not isinstance(config, dict)
        or not isinstance(config.get("resources"), list)
        or not any(
            resource.get("uri") == uri for resource in config.get("resources", [])
        )
    ):
        raise ValueError(f"Unknown resource: {uri}")

    return execute_resource_function(partition_key, uri)


async def list_prompts(partition_key: str = "default") -> List[Prompt]:
    """List available prompts for the given endpoint"""
    config = get_mcp_configuration_with_retry(partition_key=partition_key)

    if isinstance(config, dict) and "prompts" in config:
        prompts = config.get("prompts", [])

        if isinstance(prompts, list):
            return [
                Prompt(
                    name=prompt["name"],
                    description=prompt.get("description", ""),
                    arguments=[
                        PromptArgument(**argument)
                        for argument in prompt.get("arguments", [])
                        if isinstance(argument, dict)
                    ],
                )
                for prompt in prompts
                if isinstance(prompt, dict) and "name" in prompt
            ]
    return []


async def get_prompt(
    name: str,
    arguments: Optional[Dict[str, Any]],
    partition_key: str = "default",
) -> GetPromptResult:
    """Get a specific prompt with given arguments"""
    config = get_mcp_configuration_with_retry(partition_key)
    name = str(name).strip()

    if (
        not isinstance(config, dict)
        or not isinstance(config.get("prompts"), list)
        or not any(prompt["name"] == name for prompt in config.get("prompts", []))
    ):
        raise ValueError(f"Unknown prompt: {name}")

    return execute_prompt_function(partition_key, name, arguments or {})


# === MCP SDK v2 adapters — bridge plain handlers into Server(on_*=...) =====
# stdio transport is single-tenant with no per-request partition_key, so all
# adapters use partition_key="default". HTTP mode calls the plain handlers
# above directly with the real partition_key from the URL.


async def _on_list_tools(ctx, params=None):
    from mcp.types import ListToolsResult

    tools = await list_tools(partition_key="default")
    return ListToolsResult(tools=list(tools))


async def _on_call_tool(ctx, params):
    from mcp.types import CallToolResult

    name = getattr(params, "name", None) or params["name"]
    arguments = getattr(params, "arguments", None) or params.get("arguments")
    content = await call_tool(name, arguments, partition_key="default")
    return CallToolResult(content=list(content))


async def _on_list_resources(ctx, params=None):
    from mcp.types import ListResourcesResult

    resources = await list_resources(partition_key="default")
    return ListResourcesResult(resources=list(resources))


async def _on_read_resource(ctx, params):
    from mcp.types import ReadResourceResult, TextResourceContents

    uri = getattr(params, "uri", None) or params["uri"]
    result = await read_resource(uri, partition_key="default")
    contents = _serialize_resource_result(result, str(uri))
    return ReadResourceResult(
        contents=[
            TextResourceContents(
                uri=c.get("uri", str(uri)),
                mimeType=c.get("mimeType", "text/plain"),
                text=c.get("text", ""),
            )
            for c in contents
        ]
    )


async def _on_list_prompts(ctx, params=None):
    from mcp.types import ListPromptsResult

    prompts = await list_prompts(partition_key="default")
    return ListPromptsResult(prompts=list(prompts))


async def _on_get_prompt(ctx, params):
    name = getattr(params, "name", None) or params["name"]
    arguments = getattr(params, "arguments", None) or params.get("arguments")
    return await get_prompt(name, arguments, partition_key="default")


def _make_server() -> Server:
    """Instantiate the SDK Server with v2 on_* handler wiring.

    Falls back to a bare ``Server(name=...)`` if the SDK rejects the on_*
    kwargs (older or renamed API). HTTP dispatch via
    ``process_mcp_message()`` remains functional in the fallback path;
    only stdio-mode dispatch through ``server.run(...)`` is degraded.
    """
    on_handlers = dict(
        on_list_tools=_on_list_tools,
        on_call_tool=_on_call_tool,
        on_list_resources=_on_list_resources,
        on_read_resource=_on_read_resource,
        on_list_prompts=_on_list_prompts,
        on_get_prompt=_on_get_prompt,
    )
    try:
        return Server("MCP SSE Server", **on_handlers)
    except TypeError as exc:
        _log.warning(
            "MCP SDK Server rejected on_* handler kwargs (%s). "
            "Instantiating without stdio handlers — HTTP dispatch still works, "
            "stdio-mode dispatch will not return tools/resources/prompts.",
            exc,
        )
        return Server("MCP SSE Server")


# === MCP SDK Initialization ===
# Instantiated AFTER handlers/adapters so on_* kwargs can reference them.
server = _make_server()


# === MCP Message Handling ===
async def process_mcp_message(partition_key: str, message: Dict) -> Dict:
    """Process incoming MCP messages"""
    try:
        if not partition_key:
            raise ValueError("Invalid partition key")
        elif not message or not isinstance(message, dict):
            raise ValueError("Invalid message")

        method = message.get("method")
        params = message.get("params", {})
        msg_id = message.get("id")

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"subscribe": False, "listChanged": False},
                        "prompts": {"listChanged": False},
                    },
                    "serverInfo": {"name": "SSE Server", "version": "1.0.0"},
                },
            }

        elif method == "tools/list":
            tools = await list_tools(partition_key=partition_key)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "tools": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "inputSchema": tool.inputSchema,
                        }
                        for tool in tools
                    ]
                },
            }

        elif method == "tools/call":
            result = await call_tool(
                params["name"], params.get("arguments"), partition_key=partition_key
            )
            # Convert content objects to dictionaries for JSON serialization
            serialized_content = []
            for item in result:
                if hasattr(item, "model_dump"):
                    # Use Pydantic model serialization if available with JSON mode for proper URL serialization
                    serialized_content.append(
                        item.model_dump(mode="json", exclude_none=True)
                    )
                else:
                    # Manual serialization for TextContent, ImageContent, etc.
                    content_dict = {
                        "type": item.type,
                    }
                    if hasattr(item, "text"):
                        content_dict["text"] = item.text
                    if hasattr(item, "data"):
                        content_dict["data"] = item.data
                    if hasattr(item, "mimeType"):
                        content_dict["mimeType"] = item.mimeType
                    if hasattr(item, "name"):
                        content_dict["name"] = item.name
                    if hasattr(item, "uri"):
                        content_dict["uri"] = item.uri
                    if hasattr(item, "resource"):
                        if hasattr(item.resource, "model_dump"):
                            content_dict["resource"] = item.resource.model_dump(
                                mode="json", exclude_none=True
                            )
                        else:
                            content_dict["resource"] = item.resource
                    # Only add annotations if it's not None
                    if hasattr(item, "annotations") and item.annotations is not None:
                        content_dict["annotations"] = item.annotations
                    # Add _meta field as empty object if not present
                    content_dict["_meta"] = getattr(item, "_meta", {})
                    serialized_content.append(content_dict)

            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"content": serialized_content},
            }

        elif method == "resources/list":
            resources = await list_resources(partition_key=partition_key)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "resources": [
                        {
                            "uri": str(resource.uri),
                            "name": resource.name,
                            "description": resource.description,
                            "mimeType": resource.mimeType,
                        }
                        for resource in resources
                    ]
                },
            }

        elif method == "resources/templates/list":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"resourceTemplates": []},
            }

        elif method == "resources/read":
            content = await read_resource(params["uri"], partition_key=partition_key)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "contents": _serialize_resource_result(content, params["uri"])
                },
            }

        # Handle MCP protocol messages
        elif method == "prompts/list":
            # Handle list prompts request
            prompts = await list_prompts(partition_key=partition_key)
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "prompts": [
                        {
                            "name": prompt.name,
                            "description": prompt.description,
                            "arguments": [
                                {
                                    "name": arg.name,
                                    "description": arg.description,
                                    "required": arg.required,
                                }
                                for arg in (prompt.arguments or [])
                            ],
                        }
                        for prompt in prompts
                    ]
                },
            }

        elif method == "prompts/get":
            # Handle get prompt request
            result = await get_prompt(
                params["name"], params.get("arguments"), partition_key=partition_key
            )
            # Serialize messages with proper content serialization
            serialized_messages = []
            for msg in result.messages:
                # Serialize the content object properly
                if hasattr(msg.content, "model_dump"):
                    content_dict = msg.content.model_dump(
                        mode="json", exclude_none=True
                    )
                else:
                    content_dict = {
                        "type": msg.content.type,
                        "text": msg.content.text,
                        "_meta": getattr(msg.content, "_meta", {}),
                    }

                serialized_messages.append(
                    {
                        "role": msg.role,
                        "content": content_dict,
                    }
                )

            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "description": result.description,
                    "messages": serialized_messages,
                },
            }

        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    except Exception as e:
        Debugger.info(
            variable=traceback.format_exc(),
            stage=f"{__name__}:process_mcp_message",
        )
        return {
            "jsonrpc": "2.0",
            "id": message.get("id") if isinstance(message, dict) else "",
            "error": {"code": -32603, "message": "Internal error", "data": str(e)},
        }


async def run_stdio(logger: logging.Logger) -> None:
    """Run MCP server with stdio transport"""
    logger.info("Starting MCP Server with stdio transport...")

    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream, server.create_initialization_options()
            )
    except Exception as e:
        logger.error(f"Stdio server error: {e}")
        sys.exit(1)
