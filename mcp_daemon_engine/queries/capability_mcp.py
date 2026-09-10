#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Capability Engine MCP compatibility queries.

Read views over daemon-native rows (MCPModule / MCPSetting / MCPFunction).
Provider is a projection — no daemon-side Provider table. Transport
classification: BUILTIN (module in Config.setting["builtin_modules"]) >
REMOTE (source="external") > CUSTOM. partition_key always comes from
info.context, never from arguments.
"""
from __future__ import print_function

__author__ = "bibow"

from typing import Any, Dict, Optional

from graphene import ResolveInfo
from silvaengine_utility import JSONSnakeCase, Serializer

from ..handlers.config import Config
from ..models.repositories import get_repo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _builtin_modules() -> set:
    """Read the deploy-time built-in module list from Config.setting."""
    raw = (Config.setting or {}).get("builtin_modules") or []
    if isinstance(raw, str):
        raw = [m.strip() for m in raw.split(",") if m.strip()]
    return set(raw)


def _module_setting(partition_key: str, module_name: str) -> Dict[str, Any]:
    """Read a module's setting dict from the cached MCP configuration.

    The cached settings were serialized via JSONSnakeCase with the raw
    ``headers`` sub-dict restored (commit bc0b3d4) — return the dict as-is;
    do NOT re-serialize it here, or header keys like "Part-Id" would be
    snake-case mangled again.
    """
    try:
        config = Config.fetch_mcp_configuration(partition_key)
    except Exception:
        return {}
    for module in config.get("modules", []):
        if module.get("module_name") == module_name:
            setting = module.get("setting")
            return setting if isinstance(setting, dict) else {}
    return {}


def _paginate(rows: list, page_number: int, limit: int) -> tuple:
    total = len(rows)
    if not limit or limit <= 0:
        limit = 10
    pages = max(1, (total + limit - 1) // limit)
    offset = (max(1, page_number) - 1) * limit
    return rows[offset : offset + limit], pages, total


# ---------------------------------------------------------------------------
# Transport / provider projection
# ---------------------------------------------------------------------------


def _resolve_transport(
    module: Dict[str, Any], setting: Dict[str, Any]
) -> str:
    """Deployment-config check first: it can promote a row that would
    otherwise resolve as CUSTOM."""
    module_name = module.get("module_name", "")
    if module_name in _builtin_modules():
        return "builtin"
    if module.get("source") == "external":
        return "remote"
    return "custom"


def _resolve_endpoint(
    module: Dict[str, Any], setting: Dict[str, Any]
) -> Optional[str]:
    module_name = module.get("module_name", "")
    if module_name in _builtin_modules():
        return f"builtin://{module_name}"

    source = module.get("source")
    if source == "external":
        return setting.get("base_url") or None
    if source == "s3":
        bucket = Config.funct_bucket_name
        package = module.get("package_name") or module.get("module_name")
        return f"s3://{bucket}/{package}.zip" if bucket else None
    if source == "git":
        git_url = setting.get("git_url")
        if not git_url:
            return None
        commit = setting.get("resolved_commit")
        if commit:
            return f"{git_url}@{commit}"
        return git_url
    return None


def _provider_view_dict(
    partition_key: str,
    module: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Project MCPModule + MCPSetting into the Banyanos provider shape (dict)."""
    module_name = module.get("module_name")
    if not module_name:
        return None

    setting = _module_setting(partition_key, module_name)

    # capability_metadata carries display/description overrides written by
    # the compatibility registration mutations.
    metadata = setting.get("capability_metadata") or {}

    # Count enabled tools for this module.
    try:
        functions = get_repo("mcp_function").list(
            _info_stub(partition_key), module_name=module_name, limit=10000
        )
        fn_list = getattr(functions, "mcp_function_list", None) or []
    except Exception:
        fn_list = []
    tool_rows = []
    for fn in fn_list:
        # list() may return type instances (attribute access) or dicts
        if isinstance(fn, dict):
            mcp_type = fn.get("mcp_type")
            status = fn.get("status")
        else:
            mcp_type = getattr(fn, "mcp_type", None)
            status = getattr(fn, "status", None)
        if mcp_type == "tool" and status != 0:
            tool_rows.append(fn)

    transport = _resolve_transport(module, setting)

    status = "ACTIVE" if tool_rows else "INACTIVE"

    auth_type = "NONE"
    if setting.get("bearer_token"):
        auth_type = "BEARER"

    config: Dict[str, Any] = {
        "source": module.get("source"),
        "source_raw": module.get("source"),
        "classes": [
            c.get("class_name") if isinstance(c, dict) else getattr(c, "class_name", None)
            for c in (module.get("classes") or [])
        ],
    }
    if transport:
        config["transport_raw"] = transport
    for key in ("name_prefix", "timeout", "protocol_version"):
        if setting.get(key) not in (None, ""):
            config[key] = setting.get(key)

    created_at = getattr(module, "created_at", None) or module.get("created_at")
    updated_at = getattr(module, "updated_at", None) or module.get("updated_at")
    updated_by = getattr(module, "updated_by", None) or module.get("updated_by")

    return {
        "id": module_name,
        "name": module_name,
        "display_name": metadata.get("display_name") or module_name,
        "description": metadata.get("description"),
        "endpoint": _resolve_endpoint(module, setting),
        "transport": transport,
        "auth_type": auth_type,
        "protocol_version": setting.get("protocol_version") or "2025-03-26",
        "status": status,
        "register_status": "SUCCESS",
        "tool_count": len(tool_rows),
        "config": config,
        "created_at": created_at,
        "updated_at": updated_at,
        "updated_by": updated_by,
    }


class _InfoStub:
    """Minimal info-like object for repository list calls made outside a
    GraphQL execution (carries context for partition_key/logger)."""

    def __init__(self, partition_key: str):
        self.context = {
            "partition_key": partition_key,
            "logger": Config.get_logger(),
        }


def _info_stub(partition_key: str) -> _InfoStub:
    return _InfoStub(partition_key)


def _module_rows(partition_key: str) -> list:
    """List all MCPModule rows for the partition as normalized dicts."""
    repo = get_repo("mcp_module")
    modules = repo.list(_info_stub(partition_key), limit=1000)
    raw = getattr(modules, "mcp_module_list", None) or []
    rows = []
    for m in raw:
        if isinstance(m, dict):
            rows.append(m)
            continue
        rows.append(
            {
                "module_name": getattr(m, "module_name", None),
                "package_name": getattr(m, "package_name", None),
                "source": getattr(m, "source", None),
                "created_at": getattr(m, "created_at", None),
                "updated_at": getattr(m, "updated_at", None),
                "updated_by": getattr(m, "updated_by", None),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Provider / MCP Server queries
# ---------------------------------------------------------------------------


def resolve_capability_mcp_provider(
    info: ResolveInfo, **kwargs: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    partition_key = info.context["partition_key"]
    module_id = kwargs.get("id")
    if not module_id:
        return None

    try:
        module_row = get_repo("mcp_module").get(
            partition_key=partition_key, module_name=module_id
        )
    except Exception:
        module_row = None
    if not module_row:
        return None

    module = module_row if isinstance(module_row, dict) else {
        "module_name": module_row.get("module_name"),
        "package_name": module_row.get("package_name"),
        "source": module_row.get("source"),
        "created_at": module_row.get("created_at"),
        "updated_at": module_row.get("updated_at"),
        "updated_by": module_row.get("updated_by"),
    }
    return _provider_view(info, partition_key, module)


def _provider_view(
    info: ResolveInfo, partition_key: str, module: Dict[str, Any]
) -> Optional["CapabilityMcpProviderType"]:
    view = _provider_view_dict(partition_key, module)
    if view is None:
        return None
    return CapabilityMcpProviderType(**view)


def resolve_capability_mcp_provider_list(
    info: ResolveInfo, **kwargs: Dict[str, Any]
) -> Dict[str, Any]:
    partition_key = info.context["partition_key"]
    transport_filter = kwargs.get("transport")
    status_filter = kwargs.get("status")
    keyword = kwargs.get("keyword")
    page_number = kwargs.get("page_number") or 1
    limit = kwargs.get("limit") or 10

    rows = []
    for module in _module_rows(partition_key):
        view = _provider_view_dict(partition_key, module)
        if view is None:
            continue
        if transport_filter and view["transport"] != str(transport_filter.value if hasattr(transport_filter, "value") else transport_filter):
            continue
        if status_filter and view["status"] != status_filter:
            continue
        if keyword:
            hay = " ".join(
                filter(
                    None,
                    [
                        view["name"],
                        view.get("display_name"),
                        view.get("description"),
                    ],
                )
            ).lower()
            if keyword.lower() not in hay:
                continue
        rows.append(view)

    rows.sort(key=lambda v: v["name"])
    page_rows, pages, total = _paginate(rows, page_number, limit)

    return CapabilityMcpProviderConnectionType(
        capability_mcp_provider_list=[
            CapabilityMcpProviderType(**r) for r in page_rows
        ],
        page_number=page_number,
        pages=pages,
        total=total,
    )


def resolve_capability_mcp_server(
    info: ResolveInfo, **kwargs: Dict[str, Any]
) -> Optional[Any]:
    """MCP Server view — same projection as provider."""
    return resolve_capability_mcp_provider(info, **kwargs)


def resolve_capability_mcp_server_tools(
    info: ResolveInfo, **kwargs: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """List tools belonging to one provider/server (MCPFunction rows by
    module_name)."""
    partition_key = info.context["partition_key"]
    module_id = kwargs.get("id")
    if not module_id:
        return None

    kwargs.setdefault("provider_id", module_id)
    kwargs.pop("id", None)
    return resolve_capability_mcp_tool_list(info, **kwargs)


# ---------------------------------------------------------------------------
# Tool queries
# ---------------------------------------------------------------------------


def _tool_view_dict(
    partition_key: str, fn: Any, module_source: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    if isinstance(fn, dict):
        name = fn.get("name")
        description = fn.get("description")
        data = fn.get("data") or {}
        status = fn.get("status")
        created_at = fn.get("created_at")
        updated_at = fn.get("updated_at")
        module_name = fn.get("module_name")
        class_name = fn.get("class_name")
        function_name = fn.get("function_name")
        return_type = fn.get("return_type")
        is_async = fn.get("is_async")
        external_name = None
    else:
        name = getattr(fn, "name", None)
        description = getattr(fn, "description", None)
        data = getattr(fn, "data", None) or {}
        status = getattr(fn, "status", None)
        created_at = getattr(fn, "created_at", None)
        updated_at = getattr(fn, "updated_at", None)
        module_name = getattr(fn, "module_name", None)
        class_name = getattr(fn, "class_name", None)
        function_name = getattr(fn, "function_name", None)
        return_type = getattr(fn, "return_type", None)
        is_async = getattr(fn, "is_async", None)
        external_name = None

    if isinstance(data, str):
        try:
            data = Serializer.json_loads(data)
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}

    is_deprecated = status == 0
    status_str = "UNAVAILABLE" if is_deprecated else "AVAILABLE"

    config = {
        "class_name": class_name,
        "function_name": function_name,
        "return_type": return_type,
        "is_async": is_async,
    }
    if module_source:
        config["source"] = module_source
    if external_name:
        config["external_name"] = external_name

    return {
        "id": name,
        "provider_id": module_name,
        "mcp_server_id": module_name,
        "name": name,
        "display_name": data.get("display_name") or name,
        "description": description,
        "input_schema": data.get("inputSchema"),
        "output_schema": data.get("outputSchema"),
        "status": status_str,
        "is_deprecated": is_deprecated,
        "config": config,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def resolve_capability_mcp_tool(
    info: ResolveInfo, **kwargs: Dict[str, Any]
) -> Optional[Any]:
    partition_key = info.context["partition_key"]
    tool_id = kwargs.get("id")
    if not tool_id:
        return None

    try:
        fn_row = get_repo("mcp_function").get(
            partition_key=partition_key, name=tool_id
        )
    except Exception:
        fn_row = None
    if not fn_row:
        return None

    view = _tool_view_dict(partition_key, fn_row)
    return CapabilityMcpToolType(**view) if view else None


def resolve_capability_mcp_tool_list(
    info: ResolveInfo, **kwargs: Dict[str, Any]
) -> Dict[str, Any]:
    partition_key = info.context["partition_key"]
    provider_filter = kwargs.get("provider_id")
    status_filter = kwargs.get("status")
    keyword = kwargs.get("keyword")
    page_number = kwargs.get("page_number") or 1
    limit = kwargs.get("limit") or 10

    try:
        functions = get_repo("mcp_function").list(
            _info_stub(partition_key), limit=1000
        )
        raw = getattr(functions, "mcp_function_list", None) or []
    except Exception:
        raw = []

    rows = []
    for fn in raw:
        view = _tool_view_dict(partition_key, fn)
        if view is None:
            continue
        if provider_filter and view["provider_id"] != str(provider_filter):
            continue
        if status_filter and view["status"] != status_filter:
            continue
        if keyword:
            hay = " ".join(
                filter(None, [view["name"], view.get("display_name"), view.get("description")])
            ).lower()
            if keyword.lower() not in hay:
                continue
        rows.append(view)

    rows.sort(key=lambda v: (v["provider_id"] or "", v["name"] or ""))
    page_rows, pages, total = _paginate(rows, page_number, limit)

    return CapabilityMcpToolConnectionType(
        capability_mcp_tool_list=[CapabilityMcpToolType(**r) for r in page_rows],
        page_number=page_number,
        pages=pages,
        total=total,
    )


def resolve_capability_mcp_invocation_list(
    info: ResolveInfo, **kwargs: Dict[str, Any]
) -> Dict[str, Any]:
    """Invocation history over MCPFunctionCall rows (compatibility view)."""
    partition_key = info.context["partition_key"]
    tool_filter = kwargs.get("tool_name")
    status_filter = kwargs.get("status")
    page_number = kwargs.get("page_number") or 1
    limit = kwargs.get("limit") or 10

    try:
        repo = get_repo("mcp_function_call")
        result = repo.list(_info_stub(partition_key), limit=limit, page_number=page_number)
        raw = getattr(result, "mcp_function_call_list", None) or []
        total = getattr(result, "total", None) or len(raw)
    except Exception:
        raw, total = [], 0

    rows = []
    for fc in raw:
        view = _invocation_view_dict(fc)
        if view is None:
            continue
        if tool_filter and view["tool_name"] != tool_filter:
            continue
        if status_filter and view["status"] != status_filter:
            continue
        rows.append(view)

    pages = max(1, (int(total or 0) + int(limit) - 1) // int(limit))
    return CapabilityMcpInvocationConnectionType(
        capability_mcp_invocation_list=[
            CapabilityMcpInvocationType(**r) for r in rows
        ],
        page_number=page_number,
        pages=pages,
        total=total,
    )


def _invocation_view_dict(fc: Any) -> Optional[Dict[str, Any]]:
    if isinstance(fc, dict):
        uuid_val = fc.get("mcp_function_call_uuid")
        name = fc.get("name")
        mcp_type = fc.get("mcp_type")
        arguments = fc.get("arguments")
        content = fc.get("content")
        status = fc.get("status")
        notes = fc.get("notes")
        time_spent = fc.get("time_spent")
        created_at = fc.get("created_at")
        updated_at = fc.get("updated_at")
    else:
        uuid_val = getattr(fc, "mcp_function_call_uuid", None)
        name = getattr(fc, "name", None)
        mcp_type = getattr(fc, "mcp_type", None)
        arguments = getattr(fc, "arguments", None)
        content = getattr(fc, "content", None)
        status = getattr(fc, "status", None)
        notes = getattr(fc, "notes", None)
        time_spent = getattr(fc, "time_spent", None)
        created_at = getattr(fc, "created_at", None)
        updated_at = getattr(fc, "updated_at", None)

    trace_id, agent_id = None, None
    if isinstance(arguments, str):
        try:
            arguments = Serializer.json_loads(arguments)
        except Exception:
            arguments = {}
    if isinstance(arguments, dict):
        ctx = arguments.get("_capability_context") or {}
        if isinstance(ctx, dict):
            trace_id = ctx.get("trace_id")
            agent_id = ctx.get("agent_id")

    response_payload = None
    if content:
        try:
            response_payload = (
                Serializer.json_loads(content)
                if isinstance(content, str)
                else content
            )
        except Exception:
            response_payload = content

    return {
        "id": uuid_val,
        "invocation_id": uuid_val,
        "tool_name": name,
        "mcp_type": mcp_type,
        "status": status,
        "request_payload": arguments,
        "response_payload": response_payload,
        "trace_id": trace_id,
        "agent_id": agent_id,
        "duration_ms": time_spent,
        "error_message": notes,
        "created_at": created_at,
        "updated_at": updated_at,
    }


# ---------------------------------------------------------------------------
# Type aliases (imported late to avoid circulars with graphene types)
# ---------------------------------------------------------------------------

from ..types.capability_mcp import (  # noqa: E402
    CapabilityMcpInvocation as CapabilityMcpInvocationType,
    CapabilityMcpInvocationConnection as CapabilityMcpInvocationConnectionType,
    CapabilityMcpProvider as CapabilityMcpProviderType,
    CapabilityMcpProviderConnection as CapabilityMcpProviderConnectionType,
    CapabilityMcpTool as CapabilityMcpToolType,
    CapabilityMcpToolConnection as CapabilityMcpToolConnectionType,
)


__all__ = [
    "resolve_capability_mcp_provider",
    "resolve_capability_mcp_provider_list",
    "resolve_capability_mcp_server",
    "resolve_capability_mcp_server_tools",
    "resolve_capability_mcp_tool",
    "resolve_capability_mcp_tool_list",
    "resolve_capability_mcp_invocation_list",
]