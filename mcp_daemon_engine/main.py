#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

import json
import logging
from typing import Any, Dict, List

from graphene import Schema
from silvaengine_utility import Graphql, Invoker

from .handlers.config import Config
from .schema import Mutations, Query, type_class
from .utils.exceptions import InvalidRequestError


def deploy() -> List:
    return [
        {
            "service": "MCP Daemon",
            "class": "MCPDaemonEngine",
            "functions": {
                "mcp_daemon_graphql": {
                    "is_static": False,
                    "label": "MCP Daemon GraphQL",
                    "query": [
                        {"action": "ping", "label": "Ping"},
                        {"action": "mcpFunction", "label": "View MCP Function"},
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
                        {"action": "mcpModule", "label": "View MCP Module"},
                        {"action": "mcpModuleList", "label": "View MCP Module List"},
                        {"action": "mcpSetting", "label": "View MCP Setting"},
                        {
                            "action": "mcpSettingList",
                            "label": "View MCP Setting List",
                        },
                    ],
                    "mutation": [
                        {
                            "action": "insertUpdateMcpFunction",
                            "label": "Create Update MCP Function",
                        },
                        {"action": "deleteMcpFunction", "label": "Delete MCP Function"},
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
                        {"action": "deleteMcpModule", "label": "Delete MCP Module"},
                        {
                            "action": "insertUpdateMcpSetting",
                            "label": "Create Update MCP Setting",
                        },
                        {"action": "deleteMcpSetting", "label": "Delete MCP Setting"},
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
                    "disabled_in_resources": True,
                },
                "mcp": {
                    "is_static": False,
                    "label": "MCP JSON-RPC",
                    "type": "RequestResponse",
                    "support_methods": ["POST"],
                    "is_auth_required": False,
                    "is_graphql": False,
                    "settings": "beta_core_ai_agent",
                    "disabled_in_resources": True,
                },
                "async_execute_tool_function": {
                    "is_static": False,
                    "label": "Async Execute Tool Function",
                    "type": "Event",
                    "support_methods": ["POST"],
                    "is_auth_required": False,
                    "is_graphql": False,
                    "settings": "beta_core_ai_agent",
                    "disabled_in_resources": True,
                },
            },
        }
    ]


class MCPDaemonEngine(Graphql):
    def __init__(self, logger: logging.Logger, **setting: Dict[str, Any]) -> None:
        Graphql.__init__(self, logger, **setting)
        self.logger = logger
        self.setting = setting

        # BaseModel.Meta setup is now owned by Config._initialize_dynamodb_meta(),
        # called during Config.initialize(). No need to set it here.

    def mcp_daemon_graphql(self, **params: Dict[str, Any]) -> Any:
        self._apply_partition_defaults(params)
        query = params.get("query", "")
        is_config_mutation = any(name in query for name in _CONFIG_MUTATIONS)
        response = self.execute(self.__class__.build_graphql_schema(), **params)

        if is_config_mutation:
            self._clear_cache_after_successful_mutation(response, params)

        return response

    def mcp(self, **params: Dict[str, Any]) -> Any:
        """Process one MCP JSON-RPC message."""
        from .handlers.mcp_server import process_mcp_message

        self._apply_partition_defaults(params)
        message = self._extract_json_rpc_message(params)

        result = Invoker.sync_call_async_compatible(
            process_mcp_message(params.get("partition_key", ""), message)
        )
        return self._unwrap_gateway_result(result)

    def async_execute_tool_function(self, **params: Dict[str, Any]) -> Dict[str, Any]:
        """Execute an MCP tool call from a gateway background task.

        Delegates to mcp_utility.async_execute_tool_function which spawns a
        background thread and polls for up to 3 seconds. If the tool completes
        within the poll window, returns the result directly. Otherwise returns
        a resource reference with mcp_function_call_uuid so the caller can poll
        for completion via the mcp_function_call GraphQL query.
        """
        from .handlers.mcp_utility import async_execute_tool_function as _async_exec

        self._apply_partition_defaults(params)
        partition_key = params.get("partition_key")
        name = params.get("name")
        arguments = params.get("arguments")
        mcp_function_call_uuid = params.get("mcp_function_call_uuid")

        if name is None or arguments is None:
            raise InvalidRequestError(
                "Missing required parameters: name and arguments must be provided"
            )

        # If caller provides a function_call_uuid, inject it into arguments
        # so async_execute_tool_function can check for prior completion
        if mcp_function_call_uuid and isinstance(arguments, dict):
            arguments = {**arguments, "mcp_function_call_uuid": mcp_function_call_uuid}

        result = _async_exec(partition_key, name, arguments)

        # Serialize MCP content objects for JSON transport
        serialized = []
        for item in result:
            if hasattr(item, "model_dump"):
                serialized.append(item.model_dump(mode="json", exclude_none=True))
            else:
                serialized.append({"type": getattr(item, "type", "text"), "text": str(item)})

        return {"content": serialized}

    def sse_message(self, **params: Dict[str, Any]) -> Any:
        """Process an MCP message and push activity to connected SSE clients."""
        import pendulum

        from .handlers.mcp_server import process_mcp_message
        from .handlers.sse_manager import sse_manager

        self._apply_partition_defaults(params)
        message = self._extract_json_rpc_message(params)
        username = params.get("context", {}).get("user", {}).get("username", "")
        partition_key = params.get("partition_key", "")

        result = Invoker.sync_call_async_compatible(
            process_mcp_message(partition_key, message)
        )

        if username:
            try:
                Invoker.sync_call_async_compatible(
                    sse_manager.send_to_user(
                        username,
                        {
                            "type": "mcp_activity",
                            "method": message.get("method", ""),
                            "request": message,
                            "response": result,
                            "timestamp": pendulum.now("UTC").isoformat(),
                        },
                        partition_key=partition_key,
                    )
                )
            except Exception as e:
                self.logger.warning(
                    f"Failed to deliver SSE message to user {username}: {e}"
                )

        return self._unwrap_gateway_result(result)

    def refresh_cache(self, **params: Dict[str, Any]) -> Dict[str, Any]:
        """Refresh the MCP configuration cache for the gateway partition."""
        self._apply_partition_defaults(params)
        partition_key = params.get("partition_key", "")
        config = Config.refresh_mcp_configuration(partition_key)
        return {
            "status": "success",
            "message": f"Cache refreshed for partition: {partition_key}",
            "partition_key": partition_key,
            "cache_stats": {
                "tools_count": len(config.get("tools", [])),
                "resources_count": len(config.get("resources", [])),
                "prompts_count": len(config.get("prompts", [])),
                "modules_count": len(config.get("modules", [])),
            },
        }

    def clear_cache(self, **params: Dict[str, Any]) -> Dict[str, Any]:
        """Clear the MCP configuration cache for one or all partitions."""
        self._apply_partition_defaults(params)
        partition_key = params.get("partition_key")
        if partition_key:
            Config.clear_mcp_configuration_cache(partition_key)
            return {
                "status": "success",
                "message": f"Cache cleared for partition: {partition_key}",
                "partition_key": partition_key,
            }

        cached_partitions = list(Config.mcp_configuration.keys())
        Config.clear_mcp_configuration_cache()
        return {
            "status": "success",
            "message": "All MCP configuration cache cleared",
            "cleared_partitions": cached_partitions,
        }

    def mcp_info(self, **params: Dict[str, Any]) -> Dict[str, Any]:
        """Return endpoint info and configured tools/resources/prompts."""
        from .handlers.mcp_server import list_prompts, list_resources, list_tools
        from .handlers.sse_manager import sse_manager

        self._apply_partition_defaults(params)
        partition_key = params.get("partition_key", "")
        tools = Invoker.sync_call_async_compatible(list_tools(partition_key))
        resources = Invoker.sync_call_async_compatible(list_resources(partition_key))
        prompts = Invoker.sync_call_async_compatible(list_prompts(partition_key))
        stats = Invoker.sync_call_async_compatible(sse_manager.get_stats())

        return {
            "server": "MCP SSE Server",
            "version": "1.0.0",
            "partition_key": partition_key,
            "sse_stats": stats,
            "tools": tools,
            "resources": resources,
            "prompts": prompts,
        }

    def _apply_partition_defaults(self, params: Dict[str, Any]) -> None:
        endpoint_id = params.get("endpoint_id", self.setting.get("endpoint_id"))
        part_id = params.get(
            "part_id",
            params.get("metadata", {}).get("part_id", self.setting.get("part_id")),
        )

        if params.get("context") is None:
            params["context"] = {}

        if endpoint_id and "endpoint_id" not in params["context"]:
            params["context"]["endpoint_id"] = endpoint_id
        if part_id and "part_id" not in params["context"]:
            params["context"]["part_id"] = part_id

        if not params.get("partition_key"):
            if endpoint_id and part_id:
                params["partition_key"] = f"{endpoint_id}#{part_id}"
            elif endpoint_id:
                params["partition_key"] = endpoint_id

        if params.get("partition_key") and "partition_key" not in params["context"]:
            params["context"]["partition_key"] = params["partition_key"]

    @staticmethod
    def _extract_json_rpc_message(params: Dict[str, Any]) -> Dict[str, Any]:
        """Return the JSON-RPC message from gateway params.

        The gateway injects routing context keys (partition_key, endpoint_id,
        part_id, context) into the flat params dict before calling dispatch.
        This method strips those injected keys to isolate the original JSON-RPC
        message the client sent.

        If the caller wrapped the message in a ``message`` key, that takes
        precedence and is returned directly.
        """
        message = params.get("message")
        if isinstance(message, dict):
            return message

        # Gateway-injected keys to strip (denylist)
        _GATEWAY_INJECTED_KEYS = frozenset({
            "partition_key", "endpoint_id", "part_id", "context",
        })

        return {
            key: value
            for key, value in params.items()
            if key not in _GATEWAY_INJECTED_KEYS
        }

    @staticmethod
    def _unwrap_gateway_result(result: Any) -> Any:
        if isinstance(result, dict) and "statusCode" in result and "body" in result:
            body = result["body"]
            try:
                return json.loads(body) if isinstance(body, str) else body
            except (json.JSONDecodeError, TypeError):
                return body
        return result

    def _clear_cache_after_successful_mutation(
        self, response: Any, params: Dict[str, Any]
    ) -> None:
        try:
            body = (
                response.get("body", response)
                if isinstance(response, dict)
                else response
            )
            result = json.loads(body) if isinstance(body, str) else body
            if isinstance(result, dict) and "errors" not in result:
                Config.clear_mcp_configuration_cache(params.get("partition_key"))
        except Exception as e:
            self.logger.warning(f"Failed to clear MCP cache after mutation: {e}")

    @staticmethod
    def build_graphql_schema() -> Schema:
        return Schema(
            query=Query,
            mutation=Mutations,
            types=type_class(),
        )


_CONFIG_MUTATIONS = {
    "insertUpdateMcpFunction",
    "deleteMcpFunction",
    "insertUpdateMcpFunctionCall",
    "deleteMcpFunctionCall",
    "insertUpdateMcpModule",
    "deleteMcpModule",
    "insertUpdateMcpSetting",
    "deleteMcpSetting",
    "loadMcpConfiguration",
    "syncExternalMcpServer",
    "generateMcpPackageUploadUrl",
    "processMcpPackage",
}


# ---------------------------------------------------------------------------
# Module-level dispatch functions for gateway integration
# ---------------------------------------------------------------------------
# These are called by silvaengine_gateway via the route manifest's dispatch
# field (e.g. "mcp_daemon_engine.main:dispatch_graphql"). They create a
# short-lived MCPDaemonEngine instance using the already-initialized Config
# singleton, matching the pattern used by knowledge_graph_engine.
# ---------------------------------------------------------------------------


def _engine() -> MCPDaemonEngine:
    return MCPDaemonEngine(Config.get_logger(), **Config.get_setting())


def dispatch_graphql(**params: Dict[str, Any]) -> Any:
    """Gateway dispatch entry point for MCP Daemon GraphQL.

    Requires Config.initialize() to have been called by gateway startup.
    """
    return _engine().mcp_daemon_graphql(**params)


def dispatch_mcp(**params: Dict[str, Any]) -> Any:
    """Gateway dispatch entry point for MCP JSON-RPC messages."""
    return _engine().mcp(**params)


def dispatch_mcp_async(**params: Dict[str, Any]) -> Any:
    """Gateway dispatch entry point for async tool execution (background task)."""
    return _engine().async_execute_tool_function(**params)


def dispatch_sse_message(**params: Dict[str, Any]) -> Any:
    """Gateway dispatch entry point for SSE POST messages."""
    return _engine().sse_message(**params)


def dispatch_cache_refresh(**params: Dict[str, Any]) -> Dict[str, Any]:
    """Gateway dispatch entry point for MCP cache refresh."""
    return _engine().refresh_cache(**params)


def dispatch_cache_clear(**params: Dict[str, Any]) -> Dict[str, Any]:
    """Gateway dispatch entry point for clearing MCP configuration cache."""
    return _engine().clear_cache(**params)


def dispatch_mcp_info(**params: Dict[str, Any]) -> Dict[str, Any]:
    """Gateway dispatch entry point for endpoint info (tools/resources/prompts listing)."""
    return _engine().mcp_info(**params)
