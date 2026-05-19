#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

__author__ = "bibow"

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

# === FastAPI and MCP Initialization ===
server = Server("MCP SSE Server")


# === Tool Definitions ===
@server.list_tools()
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


@server.call_tool()
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


@server.list_resources()
async def list_resources(partition_key: str = "default") -> List[Resource]:
    """List available resources for the given endpoint"""
    config = get_mcp_configuration_with_retry(partition_key).get("resources", [])

    if isinstance(config, dict) and "resources" in config:
        resources = config.get("resources", [])

        if isinstance(resources, list):
            return [
                Resource(**resource)
                for resource in resources
                if isinstance(resource, dict) and "inputSchema" in resource
            ]
    return []


@server.read_resource()
async def read_resource(uri: str, partition_key: str = "default") -> Any:
    """Read content of a specific resource"""
    from .mcp_utility import get_mcp_configuration_with_retry

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


@server.list_prompts()
async def list_prompts(partition_key: str = "default") -> List[Prompt]:
    """List available prompts for the given endpoint"""
    config = get_mcp_configuration_with_retry(partition_key=partition_key)

    if isinstance(config, dict) and "prompts" in config:
        prompts = config.get("prompts", [])

        if isinstance(prompts, list):
            return [
                Prompt(
                    name=prompt["name"],
                    description=prompt["description"],
                    arguments=[
                        PromptArgument(**argument)
                        for argument in prompt.get("arguments", [])
                        if isinstance(argument, dict)
                    ],
                )
                for prompt in prompts
                if isinstance(prompt, dict) and "inputSchema" in prompt
            ]
    return []


@server.get_prompt()
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

    return execute_prompt_function(partition_key, name, arguments)


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
                    "contents": [
                        {
                            "uri": params["uri"],
                            "mimeType": "text/plain",
                            "text": content,
                            "_meta": {},
                        }
                    ]
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
