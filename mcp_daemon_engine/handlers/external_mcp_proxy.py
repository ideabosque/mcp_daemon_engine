#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

import asyncio
import concurrent.futures
import json

from mcp_http_client import MCPHttpClient

from .config import Config


class ExternalMCPProxy:
    """Adapter that makes a remote HTTP MCP server look like a local tool class.

    Instantiated by _get_class() in mcp_utility.py via the standard
    (logger, **setting) contract used by every other MCP module.

    The dispatcher injects _mcp_function_name on the instance before
    calling call_tool, following the same pattern used for endpoint_id
    and part_id.

    endpoint_id and part_id are predeclared at the class level so the
    dispatcher's `hasattr(obj, "endpoint_id") and hasattr(obj, "part_id")`
    guard stamps the active partition onto each instance before a call.
    Without these declarations partition_key resolution would silently
    fall back to "default".
    """

    endpoint_id: str | None = None
    part_id: str | None = None

    def __init__(self, logger, **setting):
        self.logger = logger
        self.base_url = setting.get("base_url", "")
        self.bearer_token = setting.get("bearer_token") or None
        self.headers = setting.get("headers") or {}
        self.name_prefix = setting.get("name_prefix") or ""
        self._mcp_function_name: str | None = None

        if not self.base_url:
            raise ValueError("ExternalMCPProxy requires 'base_url' in setting")

    def call_tool(self, **arguments):
        name = self._mcp_function_name
        external_name = self._resolve_external_name(name, "tool")
        result = self._run_async(
            self._client_call_tool(external_name, arguments)
        )
        return self._content_to_text(result)

    def read_resource(self, uri: str):
        result = self._run_async(self._client_read_resource(uri))
        return self._read_resource_to_text(result)

    def get_prompt(self, name: str, **arguments):
        external_name = self._resolve_external_name(name, "prompt")
        result = self._run_async(
            self._client_get_prompt(external_name, arguments)
        )
        return self._prompt_to_text(result)

    def _resolve_external_name(self, local_name, mcp_type):
        bucket_key = {
            "tool": "tools",
            "resource": "resources",
            "prompt": "prompts",
        }.get(mcp_type)

        config = Config.fetch_mcp_configuration(
            self._partition_key(), force_refresh=False
        )

        if bucket_key:
            bucket = config.get(bucket_key, [])
            match = next(
                (x for x in bucket if x.get("name") == local_name), None
            )
            if match and match.get("external_name"):
                return match["external_name"]

        if self.name_prefix and local_name.startswith(self.name_prefix):
            return local_name[len(self.name_prefix):]

        return local_name

    def _partition_key(self):
        if hasattr(self, "part_id") and self.part_id:
            return f"{self.endpoint_id}#{self.part_id}"
        return getattr(self, "endpoint_id", "default")

    def _run_async(self, coro):
        """Run a coroutine to completion from sync code.

        If we're already inside an event loop (e.g. SSE transport, or AWS
        Lambda handler running on an active loop), asyncio.run() would raise.
        In that case offload to a worker thread whose asyncio.run() owns its
        own loop.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        with concurrent.futures.ThreadPoolExecutor() as executor:
            return executor.submit(asyncio.run, coro).result()

    async def _client_call_tool(self, name, arguments):
        async with MCPHttpClient(
            self.logger, **self._client_settings()
        ) as client:
            return await client.call_tool(name, arguments or {})

    async def _client_read_resource(self, uri):
        async with MCPHttpClient(
            self.logger, **self._client_settings()
        ) as client:
            return await client.read_resource(uri)

    async def _client_get_prompt(self, name, arguments):
        async with MCPHttpClient(
            self.logger, **self._client_settings()
        ) as client:
            return await client.get_prompt(name, arguments or {})

    def _client_settings(self):
        s = {"base_url": self.base_url}
        if self.bearer_token:
            s["bearer_token"] = self.bearer_token
        if self.headers:
            s["headers"] = self.headers
        return s

    def _content_to_text(self, content):
        if not content:
            return ""
        joined = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if text:
                    joined.append(text)
                else:
                    joined.append(json.dumps(item, default=str))
            else:
                joined.append(str(item))
        return "\n".join(joined)

    def _read_resource_to_text(self, payload):
        if isinstance(payload, dict):
            contents = payload.get("contents")
            if isinstance(contents, list) and contents:
                return contents[0].get("text", "")
        return json.dumps(payload, default=str)

    def _prompt_to_text(self, payload):
        if isinstance(payload, dict):
            msgs = payload.get("messages")
            if isinstance(msgs, list):
                return "\n".join(
                    m.get("content", {}).get("text", "")
                    for m in msgs
                    if isinstance(m, dict)
                )
        return json.dumps(payload, default=str)