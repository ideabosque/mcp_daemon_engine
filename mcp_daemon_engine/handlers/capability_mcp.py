#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Capability Engine MCP compatibility handler.

Business logic for the compatibility layer — registration dispatch,
metadata storage, provider resolution, and deregistration. The
mutations module (``mutations/capability_mcp.py``) contains only thin
graphene class definitions that delegate here.
"""
from __future__ import print_function

__author__ = "bibow"

import traceback
from typing import Any, Dict, Optional

from ..handlers.config import Config
from ..handlers.mcp_external import sync_external_mcp_server
from ..handlers.mcp_git import (
    install_mcp_package_from_git,
)
from ..handlers.mcp_handlers import (
    process_base64_package,
    process_mcp_package,
)
from ..models.repositories import get_repo
from ..queries.capability_mcp import (
    _info_stub,
    _module_rows,
    _module_setting,
    _provider_view_dict,
)
from ..types.capability_mcp import (
    CapabilityMcpProvider,
    CapabilityMcpRegistrationPayload,
    CapabilityMcpSyncStats,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _stats_type(stats: Dict[str, Any]) -> CapabilityMcpSyncStats:
    stats = stats or {}
    return CapabilityMcpSyncStats(
        tools=stats.get("tools", 0),
        resources=stats.get("resources", 0),
        prompts=stats.get("prompts", 0),
        modules=stats.get("modules", 0),
        settings=stats.get("settings", 0),
    )


def resolve_provider(info, module_name: str):
    """Best-effort provider view for the registration payload."""
    partition_key = info.context.get("partition_key")
    try:
        for module in _module_rows(partition_key):
            if module.get("module_name") == module_name:
                view = _provider_view_dict(partition_key, module)
                if view:
                    return CapabilityMcpProvider(**view)
    except Exception:
        pass
    return None


def store_capability_metadata(
    info, module_name: str, display_name: str = None, description: str = None
) -> None:
    """Persist display/description overrides into
    MCPSetting.setting.capability_metadata after a successful registration."""
    partition_key = info.context.get("partition_key")
    if not display_name and not description:
        return
    try:
        setting = _module_setting(partition_key, module_name)
        setting_id = _resolve_setting_id(partition_key, module_name, setting)
        if not setting_id:
            if info.context.get("logger"):
                info.context["logger"].warning(
                    f"Cannot store capability metadata for '{module_name}': "
                    f"no setting_id found"
                )
            return

        metadata = dict(setting.get("capability_metadata") or {})
        if display_name:
            metadata["display_name"] = display_name
        if description:
            metadata["description"] = description

        from ..handlers.config import _dispatch_internal_graphql

        _dispatch_internal_graphql(
            context={"partition_key": partition_key},
            query="""
            mutation storeCapabilityMetadata(
                $settingId: String!
                $setting: JSONCamelCase
                $updatedBy: String!
            ) {
                insertUpdateMcpSetting(
                    settingId: $settingId
                    setting: $setting
                    updatedBy: $updatedBy
                ) { mcpSetting { settingId } }
            }
            """,
            variables={
                "settingId": setting_id,
                "setting": {
                    **{k: v for k, v in setting.items() if k != "setting_id"},
                    "capability_metadata": metadata,
                },
                "updatedBy": "capability_compat",
            },
        )
        Config.clear_mcp_configuration_cache(partition_key)
    except Exception as e:
        if info.context.get("logger"):
            info.context["logger"].warning(
                f"Failed to store capability metadata for '{module_name}': {e}"
            )


def register_and_respond(
    info,
    *,
    handler_fn,
    handler_kwargs: Dict[str, Any],
    module_name: str,
    transport: str = "custom",
    display_name: str = None,
    description: str = None,
    success_message: str = None,
    error_prefix: str = "Failed to register MCP provider",
    extra_payload: Dict[str, Any] = None,
) -> CapabilityMcpRegistrationPayload:
    """Shared boilerplate for all registration mutations.

    1. Call the underlying handler.
    2. Store display/description metadata if provided.
    3. Resolve the provider view from the freshly-written rows.
    4. Return a CapabilityMcpRegistrationPayload (ok=False on error).
    """
    try:
        result = handler_fn(info, **handler_kwargs)

        if isinstance(result, dict) and "action" in result:
            stats = result.get("stats") or {}
            message = result.get("message", success_message or "")
        else:
            stats = result or {}
            message = success_message or ""

        store_capability_metadata(
            info, module_name,
            display_name=display_name, description=description,
        )
        provider = resolve_provider(info, module_name)

        payload = {
            "ok": True,
            "message": message,
            "provider": provider,
            "transport": transport,
            "stats": _stats_type(stats),
        }
        if extra_payload:
            for k in ("resolved_commit", "installed_package_version", "action"):
                if k in extra_payload:
                    payload[k] = extra_payload[k]
        elif isinstance(result, dict):
            for k in ("resolved_commit", "installed_package_version", "action"):
                if result.get(k):
                    payload[k] = result[k]

        return CapabilityMcpRegistrationPayload(**payload)

    except Exception as e:
        log = traceback.format_exc()
        if info.context.get("logger"):
            info.context["logger"].error(log)
        return CapabilityMcpRegistrationPayload(
            ok=False, message=f"{error_prefix}: {str(e)}",
        )


# ---------------------------------------------------------------------------
# Registration dispatch (for the unified registerCapabilityMcp mutation)
# ---------------------------------------------------------------------------


def register_capability_mcp(info, **kwargs) -> CapabilityMcpRegistrationPayload:
    """Unified registration dispatcher — called by RegisterCapabilityMcp."""
    transport = (kwargs.get("transport") or "").lower().strip()
    module_name = kwargs["module_name"]

    if transport == "remote":
        if not kwargs.get("base_url"):
            return CapabilityMcpRegistrationPayload(
                ok=False, message="baseUrl is required for transport=remote",
            )
        return register_and_respond(
            info,
            handler_fn=sync_external_mcp_server,
            handler_kwargs={
                "server_name": module_name,
                "base_url": kwargs["base_url"],
                "bearer_token": kwargs.get("bearer_token"),
                "headers": kwargs.get("headers"),
                "name_prefix": kwargs.get("name_prefix"),
                "updated_by": kwargs["updated_by"],
                "timeout": kwargs.get("timeout"),
            },
            module_name=module_name,
            transport="remote",
            display_name=kwargs.get("display_name"),
            description=kwargs.get("description"),
            error_prefix="Failed to register remote MCP server",
        )

    if transport == "s3":
        if not kwargs.get("s3_key"):
            return CapabilityMcpRegistrationPayload(
                ok=False, message="s3Key is required for transport=s3",
            )
        if not Config.enable_s3_package_upload:
            return CapabilityMcpRegistrationPayload(
                ok=False, message="S3 package upload is disabled; use transport=git.",
            )
        handler_kwargs = {
            "s3_key": kwargs["s3_key"],
            "module_name": module_name,
            "package_name": kwargs.get("package_name") or module_name,
            "source": "s3",
            "updated_by": kwargs["updated_by"],
        }
        if kwargs.get("variables"):
            handler_kwargs["variables"] = kwargs["variables"]
        return register_and_respond(
            info,
            handler_fn=process_mcp_package,
            handler_kwargs=handler_kwargs,
            module_name=module_name,
            transport="custom",
            display_name=kwargs.get("display_name"),
            description=kwargs.get("description"),
            error_prefix="Failed to register S3 MCP package",
        )

    if transport == "base64":
        if not kwargs.get("package_base64"):
            return CapabilityMcpRegistrationPayload(
                ok=False, message="packageBase64 is required for transport=base64",
            )
        if not Config.enable_s3_package_upload:
            return CapabilityMcpRegistrationPayload(
                ok=False, message="Base64 package loading is disabled; use transport=git.",
            )
        handler_kwargs = {
            "package_base64": kwargs["package_base64"],
            "module_name": module_name,
            "package_name": kwargs.get("package_name") or module_name,
            "source": "s3",
            "updated_by": kwargs["updated_by"],
        }
        if kwargs.get("variables"):
            handler_kwargs["variables"] = kwargs["variables"]
        return register_and_respond(
            info,
            handler_fn=process_base64_package,
            handler_kwargs=handler_kwargs,
            module_name=module_name,
            transport="custom",
            display_name=kwargs.get("display_name"),
            description=kwargs.get("description"),
            error_prefix="Failed to register Base64 MCP package",
        )

    if transport == "git":
        if not kwargs.get("git_url"):
            return CapabilityMcpRegistrationPayload(
                ok=False, message="gitUrl is required for transport=git",
            )
        handler_kwargs = {
            "git_url": kwargs["git_url"],
            "git_ref": kwargs.get("ref"),
            "git_subdirectory": kwargs.get("subdirectory"),
            "version_strategy": kwargs.get("version_strategy"),
            "distribution_name": kwargs.get("distribution_name"),
            "module_name": module_name,
            "package_name": kwargs.get("package_name") or module_name,
            "updated_by": kwargs["updated_by"],
        }
        if kwargs.get("variables"):
            handler_kwargs["variables"] = kwargs["variables"]
        return register_and_respond(
            info,
            handler_fn=install_mcp_package_from_git,
            handler_kwargs=handler_kwargs,
            module_name=module_name,
            transport="custom",
            display_name=kwargs.get("display_name"),
            description=kwargs.get("description"),
            error_prefix="Failed to register Git MCP package",
        )

    return CapabilityMcpRegistrationPayload(
        ok=False,
        message=f"Unknown transport '{transport}'. Supported: remote, s3, base64, git.",
    )


# ---------------------------------------------------------------------------
# Deregistration
# ---------------------------------------------------------------------------


def _resolve_setting_id(
    partition_key: str, module_name: str, setting: Dict[str, Any]
) -> Optional[str]:
    """Extract setting_id from the cached setting or the MCPModule row."""
    setting_id = setting.get("setting_id")
    if setting_id:
        return setting_id
    try:
        module_row = get_repo("mcp_module").get(
            partition_key=partition_key, module_name=module_name
        )
        return _module_setting_id(module_row)
    except Exception:
        return None


def _module_setting_id(module: Any) -> Optional[str]:
    """Extract setting_id from a module's first class entry."""
    classes = (
        module.get("classes") if isinstance(module, dict)
        else getattr(module, "classes", None)
    ) or []
    for cls in classes:
        if isinstance(cls, dict):
            if cls.get("setting_id"):
                return cls["setting_id"]
        elif hasattr(cls, "setting_id") and cls.setting_id:
            return cls.setting_id
        elif hasattr(cls, "as_dict"):
            sid = cls.as_dict().get("setting_id")
            if sid:
                return sid
    return None


def _setting_still_referenced(
    partition_key: str, setting_id: Any, excluding_module_name: str
) -> bool:
    """True if another module still references ``setting_id``. Fail-safe."""
    try:
        result = get_repo("mcp_module").list(_info_stub(partition_key), limit=10000)
        for m in getattr(result, "mcp_module_list", None) or []:
            name = (
                m.get("module_name") if isinstance(m, dict)
                else getattr(m, "module_name", None)
            )
            if name == excluding_module_name:
                continue
            if _module_setting_id(m) == setting_id:
                return True
    except Exception:
        return True
    return False


def _resolve_expected_sources(hint: Any) -> tuple:
    """Translate a caller-provided transport hint into a source filter."""
    if not hint:
        return ()
    normalized = str(hint).strip().lower()
    return {
        "remote": ("external",),
        "external": ("external",),
        "s3": ("s3",),
        "git": ("git",),
        "custom": ("s3", "git"),
        "builtin": (),
    }.get(normalized, ())


def unregister_capability_mcp(
    info, module_name: str, expected_transport: str = None,
    updated_by: str = "capability_compat",
) -> Dict[str, Any]:
    """Delete a module's rows: functions → module → orphaned setting."""
    partition_key = info.context["partition_key"]
    logger = info.context.get("logger") or Config.logger
    expected_sources = _resolve_expected_sources(expected_transport)

    # 1. Look up the module row
    try:
        module_row = get_repo("mcp_module").get(
            partition_key=partition_key, module_name=module_name
        )
    except Exception as lookup_err:
        logger.warning(f"MCPModule lookup failed for '{module_name}': {lookup_err!r}")
        module_row = None
    if not module_row:
        return {
            "ok": False, "message": f"Module '{module_name}' not found",
            "module_name": module_name, "transport": None,
        }

    source = (
        module_row.get("source") if isinstance(module_row, dict)
        else getattr(module_row, "source", None)
    )
    transport_label = "remote" if source == "external" else "custom"
    if expected_sources and source not in expected_sources:
        return {
            "ok": False,
            "message": (
                f"Module '{module_name}' has source={source!r}, cannot "
                f"unregister (expected one of {list(expected_sources)})"
            ),
            "module_name": module_name,
            "transport": transport_label, "source": source,
        }

    setting_id = _module_setting_id(module_row)

    # 2. Delete every MCPFunction row for this module
    deleted_functions = 0
    try:
        function_names = []
        cached = Config.fetch_mcp_configuration(partition_key) or {}
        for fn in cached.get("tools", []) + cached.get("resources", []) + cached.get("prompts", []):
            if fn.get("module_name") == module_name and fn.get("name"):
                function_names.append(fn["name"])
        if not function_names:
            live = get_repo("mcp_function").list(
                _info_stub(partition_key), module_name=module_name, limit=10000,
            )
            for fn in getattr(live, "mcp_function_list", None) or []:
                name = fn.get("name") if isinstance(fn, dict) else getattr(fn, "name", None)
                if name:
                    function_names.append(name)
        for name in function_names:
            try:
                get_repo("mcp_function").delete(info, name=name)
                deleted_functions += 1
            except Exception as e:
                logger.warning(f"Failed to delete function '{name}': {e}")
    except Exception as e:
        logger.warning(f"Failed to enumerate functions: {e}")

    # 3. Delete the MCPModule row
    try:
        get_repo("mcp_module").delete(info, module_name=module_name)
    except Exception as e:
        logger.error(f"Failed to delete module '{module_name}': {e}")
        return {
            "ok": False, "message": f"Failed to delete module row: {e}",
            "module_name": module_name, "transport": transport_label,
            "source": source, "deleted_functions": deleted_functions,
        }

    # 4. Delete the shared MCPSetting iff nothing else uses it
    setting_kept = False
    deleted_setting = False
    if setting_id:
        if _setting_still_referenced(partition_key, setting_id, module_name):
            setting_kept = True
        else:
            try:
                get_repo("mcp_setting").delete(info, setting_id=setting_id)
                deleted_setting = True
            except Exception as e:
                logger.warning(f"Failed to delete orphaned setting '{setting_id}': {e}")

    return {
        "ok": True,
        "message": (
            f"Unregistered '{module_name}' (source={source}): deleted "
            f"{deleted_functions} function(s) and the module row"
            + (
                "; also deleted the orphaned setting" if deleted_setting
                else ("; kept shared setting" if setting_kept else "")
            )
        ),
        "module_name": module_name,
        "transport": transport_label, "source": source,
        "deleted_functions": deleted_functions,
        "deleted_setting": deleted_setting, "setting_kept": setting_kept,
    }


__all__ = [
    "register_and_respond",
    "register_capability_mcp",
    "resolve_provider",
    "store_capability_metadata",
    "unregister_capability_mcp",
]