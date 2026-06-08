#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

import asyncio
import json
import logging
import os
import sys
from typing import Any, Dict, List

from silvaengine_utility import Debugger, Graphql, HttpResponse, Invoker, Serializer

from .handlers.config import Config
from .handlers.mcp_server import run_stdio


# Hook function applied to deployment
def deploy() -> List:
    return [
        {
            "service": "MCP Daemon",
            "class": "AIMCPDaemonEngine",
            "functions": {
                "mcp_core_graphql": {
                    "is_static": False,
                    "label": "MCP Core GraphQL",
                    "query": [
                        {"action": "ping", "label": "Ping"},
                        {
                            "action": "mcpFunction",
                            "label": "View MCP Function",
                        },
                        {
                            "action": "mcpFunctionList",
                            "label": "View MCP Function List",
                        },
                        {
                            "action": "mcpFunctionCall",
                            "label": "View MCP Function Call",
                        },
                        {
                            "action": "mcpFunctionCallList",
                            "label": "View MCP Function Call List",
                        },
                    ],
                    "mutation": [
                        {
                            "action": "insertUpdateMcpFunction",
                            "label": "Create Update MCP Function",
                        },
                        {
                            "action": "deleteMcpFunction",
                            "label": "Delete MCP Function",
                        },
                        {
                            "action": "insertUpdateMcpFunctionCall",
                            "label": "Create Update MCP Function Call",
                        },
                        {
                            "action": "deleteMcpFunctionCall",
                            "label": "Delete MCP Function Call",
                        },
                        {
                            "action": "insertUpdateMcpModule",
                            "label": "Create Update MCP Module",
                        },
                        {
                            "action": "deleteMcpModule",
                            "label": "Delete MCP Module",
                        },
                        {
                            "action": "insertUpdateMcpSetting",
                            "label": "Create Update MCP Setting",
                        },
                        {
                            "action": "deleteMcpSetting",
                            "label": "Delete MCP Setting",
                        },
                        {
                            "action": "loadMcpConfiguration",
                            "label": "Load MCP Configuration",
                        },
                        {
                            "action": "syncExternalMcpServer",
                            "label": "Sync External MCP Server",
                        },
                        {
                            "action": "generateMcpPackageUploadUrl",
                            "label": "Generate MCP Package Upload URL",
                        },
                        {
                            "action": "processMcpPackage",
                            "label": "Process MCP Package",
                        },
                    ],
                    "type": "RequestResponse",
                    "support_methods": ["POST"],
                    "is_auth_required": False,
                    "is_graphql": True,
                    "settings": "beta_core_ai_agent",
                    "disabled_in_resources": True,  # Ignore adding to resource list.
                },
                "mcp": {
                    "is_static": False,
                    "label": "MCP Server",
                    "type": "RequestResponse",
                    "support_methods": ["POST", "GET"],
                    "is_auth_required": False,
                    "is_graphql": False,
                    "settings": "beta_core_ai_agent",
                    "disabled_in_resources": True,  # Ignore adding to resource list.
                },
                "async_execute_tool_function": {
                    "is_static": False,
                    "label": "Async Execute Tool Function",
                    "type": "Event",
                    "support_methods": ["POST"],
                    "is_auth_required": False,
                    "is_graphql": False,
                    "settings": "beta_core_ai_agent",
                    "disabled_in_resources": True,  # Ignore adding to resource list.
                },
            },
        }
    ]


class AIMCPDaemonEngine(object):
    def __init__(self, logger: logging.Logger, **setting: Dict[str, Any]) -> None:
        # Initialize configuration via the Config class
        Config.initialize(logger, **setting)

        self.transport = str(setting.get("transport", "")).strip()
        self.port = setting.get("port", 8000)
        self.logger = logger
        self.setting = setting

    def _apply_partition_defaults(self, params: Dict[str, Any]) -> None:
        """
        Ensure endpoint_id/part_id defaults and assemble partition_key.
        """
        if params.get("endpoint_id") is None:
            params["endpoint_id"] = self.setting.get("endpoint_id")

        part_id = params.get("metadata", {}).get("part_id", self.setting.get("part_id"))
        endpoint_id = params.get("endpoint_id")
        params["partition_key"] = f"{endpoint_id}"

        if params.get("context") is None:
            params["context"] = {}

        if part_id:
            params["context"]["partition_key"] = f"{endpoint_id}#{part_id}"
            params["partition_key"] = f"{endpoint_id}#{part_id}"

    def mcp(self, **params: Dict[str, Any]) -> Dict[str, Any]:
        from .handlers.mcp_server import process_mcp_message

        self._apply_partition_defaults(params)

        return HttpResponse.format_response(
            data=Invoker.sync_call_async_compatible(
                process_mcp_message(
                    str(params.get("partition_key", "")).strip(),
                    params,
                )
            )
        )

    def async_execute_tool_function(self, **params: Dict[str, Any]) -> None:
        self._apply_partition_defaults(params)

        partition_key = params.pop("partition_key", None)
        name = params.get("name", None)
        arguments = params.get("arguments", None)
        mcp_function_call_uuid = params.get("mcp_function_call_uuid", None)

        if name is None or arguments is None or mcp_function_call_uuid is None:
            raise ValueError(
                "Missing required parameters: name, arguments and mcp_function_call_uuid must be provided"
            )

        from .handlers.mcp_utility import execute_tool_function

        execute_tool_function(
            partition_key,
            name,
            arguments,
            mcp_function_call_uuid=mcp_function_call_uuid,
        )
        return

    def mcp_core_graphql(self, **params: Dict[str, Any]) -> Any:
        if Config.mcp_core:
            self._apply_partition_defaults(params)

            return Config.mcp_core.mcp_core_graphql(**params)

        return Graphql.error_response("Invalid mcp graphql engine")

    def daemon(self):
        try:
            if self.transport == "sse":
                import uvicorn

                from .handlers.auth_router import router as auth_router
                from .handlers.mcp_app import app
                from .handlers.middleware import FlexJWTMiddleware

                # JWT guard second
                app.add_middleware(FlexJWTMiddleware, public_paths=["/health"])
                # mount /auth routes
                app.include_router(auth_router)

                self.logger.info("Running in SSE mode...")
                """Run SSE server using uvicorn."""
                config = uvicorn.Config(
                    app=app,
                    host="0.0.0.0",
                    port=self.port,
                    log_level="info",
                    access_log=True,
                    loop="asyncio",
                )
                server = uvicorn.Server(config)
                Invoker.sync_call_async_compatible(server.serve())
            else:
                self.logger.info("Running in stdio mode...")
                Invoker.sync_call_async_compatible(run_stdio(self.logger))
        except KeyboardInterrupt:
            self.logger.info("Daemon interrupted by user.")
        except Exception as e:
            self.logger.exception("Fatal daemon error")
            sys.exit(1)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    logger = logging.getLogger()

    if len(sys.argv) > 1:
        transport = "stdio"
        mcp_config_file = sys.argv[1].lower()
        logger.info(f"Using config file: {mcp_config_file}")
    else:
        transport = os.getenv("MCP_TRANSPORT", "sse").lower()
        mcp_config_file = os.getenv("MCP_CONFIG_FILE", None)

    mcp_daemon_engine = AIMCPDaemonEngine(
        logger,
        **{
            "region_name": os.getenv("REGION_NAME"),
            "aws_access_key_id": os.getenv("AWS_ACCESS_KEY_ID"),
            "aws_secret_access_key": os.getenv("AWS_SECRET_ACCESS_KEY"),
            "transport": transport,
            "port": int(os.getenv("PORT", "8000")),
            "mcp_configuration": (
                json.load(open(mcp_config_file, "r")) if mcp_config_file else None
            ),
            "auth_provider": os.getenv("AUTH_PROVIDER", "local").lower(),
            "local_user_file": os.getenv("LOCAL_USER_FILE"),
            "admin_static_token": os.getenv("ADMIN_STATIC_TOKEN"),
            "cognito_user_pool_id": os.getenv("COGNITO_USER_POOL_ID"),
            "cognito_app_client_id": os.getenv("COGNITO_APP_CLIENT_ID"),
            "cognito_app_secret": os.getenv("COGNITO_APP_SECRET"),
            "cognito_jwks_url": os.getenv("COGNITO_JWKS_URL"),
            "funct_bucket_name": os.getenv("FUNCT_BUCKET_NAME"),
            "funct_zip_path": os.getenv("FUNCT_ZIP_PATH"),
            "funct_extract_path": os.getenv("FUNCT_EXTRACT_PATH"),
        },
    )
    mcp_daemon_engine.daemon()
