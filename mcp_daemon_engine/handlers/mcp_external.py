#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

import asyncio
import concurrent.futures
import re
from typing import Any, Dict, List

from mcp_http_client import MCPHttpClient

from .config import Config
from .mcp_handlers import load_mcp_configuration_into_models, validate_manifest

_PACKAGE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _validate_external_server_name(server_name: str) -> None:
    if not server_name or not _PACKAGE_NAME_RE.match(server_name):
        raise Exception(
            f"Invalid server name '{server_name}': must match ^[A-Za-z][A-Za-z0-9_]*$"
        )


def _validate_base_url(base_url: str) -> None:
    if not base_url or not (
        base_url.startswith("http://") or base_url.startswith("https://")
    ):
        raise Exception(
            f"Invalid base_url '{base_url}': must start with http:// or https://"
        )


def _run_async(coro):
    """Run a coroutine to completion from sync code.

    If we're already inside an event loop (e.g. SSE transport dispatched the
    GraphQL resolver through sync_call_async_compatible, or AWS Lambda's
    handler is running on an active loop), asyncio.run() would raise
    "cannot be called from a running event loop". In that case we offload
    to a worker thread whose asyncio.run() owns its own loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor() as executor:
        return executor.submit(asyncio.run, coro).result()


async def _fetch_external_inventory(
    logger, base_url, bearer_token=None, headers=None
) -> Dict[str, List]:
    client_settings = {"base_url": base_url}
    if bearer_token:
        client_settings["bearer_token"] = bearer_token
    if headers:
        client_settings["headers"] = headers

    async with MCPHttpClient(logger, **client_settings) as client:
        tools = await client.list_tools()
        resources = await client.list_resources()
        prompts = await client.list_prompts()

    return {
        "tools": tools,
        "resources": resources,
        "prompts": prompts,
    }


def _build_manifest(
    server_name: str, inventory: Dict[str, List], name_prefix: str
) -> Dict[str, Any]:
    def _pfx(raw_name: str) -> str:
        return f"{name_prefix}{raw_name}" if name_prefix else raw_name

    tools = [
        {
            "name": _pfx(tool.name),
            "description": tool.description,
            "inputSchema": tool.input_schema,
            "external_name": tool.name,
            "is_async": False,
        }
        for tool in inventory["tools"]
    ]

    resources = [
        {
            "name": _pfx(res.name),
            "description": res.description,
            "uri": res.uri,
            "mimeType": res.mime_type,
            "external_name": res.name,
        }
        for res in inventory["resources"]
    ]

    prompts = [
        {
            "name": _pfx(prompt.name),
            "description": prompt.description,
            "arguments": prompt.arguments,
            "external_name": prompt.name,
        }
        for prompt in inventory["prompts"]
    ]

    module_links = []
    for tool in tools:
        module_links.append(
            {
                "type": "tool",
                "name": tool["name"],
                "module_name": server_name,
                "class_name": "ExternalMCPProxy",
                "function_name": "call_tool",
                "return_type": "text",
                "is_async": False,
            }
        )
    for res in resources:
        module_links.append(
            {
                "type": "resource",
                "name": res["name"],
                "module_name": server_name,
                "class_name": "ExternalMCPProxy",
                "function_name": "read_resource",
                "return_type": "text",
                "is_async": False,
            }
        )
    for prompt in prompts:
        module_links.append(
            {
                "type": "prompt",
                "name": prompt["name"],
                "module_name": server_name,
                "class_name": "ExternalMCPProxy",
                "function_name": "get_prompt",
                "return_type": "text",
                "is_async": False,
            }
        )

    return {
        "tools": tools,
        "resources": resources,
        "prompts": prompts,
        "module_links": module_links,
        "modules": [
            {
                "module_name": server_name,
                "package_name": server_name,
                "class_name": "ExternalMCPProxy",
                "setting": {
                    "base_url": "",
                    "bearer_token": "",
                    "headers": {},
                    "name_prefix": "",
                },
                "source": "external",
            }
        ],
    }


def sync_external_mcp_server(
    info, *, server_name, base_url, bearer_token, headers, name_prefix, updated_by
) -> Dict[str, Any]:
    _validate_external_server_name(server_name)
    _validate_base_url(base_url)

    logger = info.context.get("logger") or Config.logger

    inventory = _run_async(
        _fetch_external_inventory(
            logger=logger,
            base_url=base_url,
            bearer_token=bearer_token,
            headers=headers or {},
        )
    )

    mcp_configuration = _build_manifest(
        server_name=server_name,
        inventory=inventory,
        name_prefix=name_prefix or "",
    )

    validate_manifest(
        mcp_configuration,
        logger=logger,
        module_name=server_name,
    )

    partition_key = info.context.get("partition_key")
    Config.clear_mcp_configuration_cache(partition_key)

    load_kwargs = {
        "mcp_configuration": mcp_configuration,
        "module_name": server_name,
        "package_name": server_name,
        "source": "external",
        "variables": {
            "base_url": base_url,
            "bearer_token": bearer_token or "",
            "headers": headers or {},
            "name_prefix": name_prefix or "",
        },
        "updated_by": updated_by,
    }

    stats = load_mcp_configuration_into_models(info, **load_kwargs)

    try:
        Config.fetch_mcp_configuration(partition_key, force_refresh=True)
        logger.info(f"Cache warmed for partition_key: {partition_key}")
    except Exception as e:
        logger.warning(f"Cache warm failed for {partition_key}: {e}")

    return stats