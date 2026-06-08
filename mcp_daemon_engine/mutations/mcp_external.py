# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

import traceback
from typing import Any, Dict

from graphene import Boolean, Field, Mutation, String
from silvaengine_utility import JSONCamelCase

from ..handlers.mcp_external import sync_external_mcp_server
from ..types.mcp_configuration_stats import McpConfigurationStats


class SyncExternalMcpServer(Mutation):
    ok = Boolean(required=True)
    message = String()
    stats = Field(McpConfigurationStats)

    class Arguments:
        server_name = String(required=True)
        base_url = String(required=True)
        bearer_token = String(required=False)
        headers = JSONCamelCase(required=False)
        name_prefix = String(required=False)
        updated_by = String(required=True)

    @staticmethod
    def mutate(
        root: Any, info: Any, **kwargs: Dict[str, Any]
    ) -> "SyncExternalMcpServer":
        try:
            stats = sync_external_mcp_server(
                info,
                server_name=kwargs["server_name"],
                base_url=kwargs["base_url"],
                bearer_token=kwargs.get("bearer_token"),
                headers=kwargs.get("headers"),
                name_prefix=kwargs.get("name_prefix"),
                updated_by=kwargs["updated_by"],
            )

            message = (
                f"Successfully synced external MCP server "
                f"'{kwargs['server_name']}': "
                f"{stats['tools']} tools, {stats['resources']} resources, "
                f"{stats['prompts']} prompts, {stats['modules']} modules, "
                f"{stats['settings']} settings"
            )

            return SyncExternalMcpServer(
                ok=True,
                message=message,
                stats=McpConfigurationStats(**stats),
            )

        except Exception as e:
            log = traceback.format_exc()
            if info.context.get("logger"):
                info.context["logger"].error(log)
            return SyncExternalMcpServer(
                ok=False,
                message=f"Failed to sync external MCP server: {str(e)}",
            )