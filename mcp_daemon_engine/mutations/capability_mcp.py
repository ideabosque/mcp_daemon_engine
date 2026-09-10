#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Capability Engine MCP compatibility mutations.

Thin wrappers over the existing daemon handlers:
- Remote MCP       -> sync_external_mcp_server()
- Custom MCP (S3)  -> generate_upload_url() / process_mcp_package()
- Custom MCP (B64) -> process_base64_package() (respects the
                     enable_s3_package_upload gate; surfaces a clear
                     disabled message instead of silently retrying)
- Custom MCP (Git) -> install_mcp_package_from_git() /
                     refresh_mcp_git_package() / check_mcp_git_package_version()
- Invocation       -> execute_tool_function() (existing runtime + audit path)

All successful registration paths clear and warm the partition MCP
configuration cache â€” the underlying handlers already do this internally.
partition_key comes from info.context only.
"""
from __future__ import print_function

__author__ = "bibow"

import traceback
from typing import Any, Dict

from graphene import Boolean, DateTime, Field, Int, Mutation, String
from silvaengine_utility import JSONCamelCase, JSONSnakeCase

from ..handlers.config import Config
from ..handlers.mcp_external import sync_external_mcp_server
from ..models.repositories import get_repo
from ..handlers.mcp_git import (
    check_mcp_git_package_version,
    install_mcp_package_from_git,
    refresh_mcp_git_package,
)
from ..handlers.mcp_handlers import (
    generate_upload_url,
    process_base64_package,
    process_mcp_package,
)
from ..types.capability_mcp import (
    CapabilityMcpGitVersionInfo,
    CapabilityMcpInvocation as CapabilityMcpInvocationType,
    CapabilityMcpRegistrationPayload,
    CapabilityMcpSyncStats,
)
from ..queries.capability_mcp import (
    _module_setting,
    _module_rows,
    _provider_view_dict,
)


# ---------------------------------------------------------------------------
# Helpers
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


def _resolve_provider(info, module_name: str):
    """Best-effort provider view for the registration payload."""
    partition_key = info.context.get("partition_key")
    try:
        for module in _module_rows(partition_key):
            if module.get("module_name") == module_name:
                view = _provider_view_dict(partition_key, module)
                if view:
                    from ..types.capability_mcp import CapabilityMcpProvider

                    return CapabilityMcpProvider(**view)
    except Exception:
        pass
    return None


def _store_capability_metadata(
    info, module_name: str, display_name: str = None, description: str = None
) -> None:
    """Persist display/description overrides into
    MCPSetting.setting.capability_metadata after a successful registration."""
    partition_key = info.context.get("partition_key")
    if not display_name and not description:
        return
    try:
        setting = _module_setting(partition_key, module_name)
        setting_id = setting.get("setting_id")
        if not setting_id:
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
                ) {
                    mcpSetting { settingId }
                }
            }
            """,
            variables={
                "settingId": setting_id,
                "setting": {
                    **{
                        k: v
                        for k, v in setting.items()
                        if k != "setting_id"
                    },
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


def _register_and_respond(
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
) -> "CapabilityMcpRegistrationPayload":
    """Shared boilerplate for all registration mutations.

    1. Call the underlying handler (sync_external_mcp_server,
       process_mcp_package, process_base64_package,
       install_mcp_package_from_git, refresh_mcp_git_package).
    2. Store display/description metadata if provided.
    3. Resolve the provider view from the freshly-written rows.
    4. Return a CapabilityMcpRegistrationPayload.

    On failure, returns ``ok=False`` with a clear message — never raises.

    ``extra_payload`` carries mutation-specific fields (resolved_commit,
    installed_package_version, action) that only some handlers produce.
    """
    try:
        result = handler_fn(info, **handler_kwargs)

        # install_mcp_package_from_git returns a dict with "action";
        # the S3/external handlers return a stats dict. Normalize.
        if isinstance(result, dict) and "action" in result:
            stats = result.get("stats") or {}
            message = result.get("message", success_message or "")
        else:
            stats = result or {}
            message = success_message or ""

        _store_capability_metadata(
            info, module_name,
            display_name=display_name, description=description,
        )
        provider = _resolve_provider(info, module_name)

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
            ok=False,
            message=f"{error_prefix}: {str(e)}",
        )


# ---------------------------------------------------------------------------
# 7.1 Remote MCP registration
# ---------------------------------------------------------------------------


class RegisterCapabilityRemoteMcp(Mutation):
    """Register/sync a remote MCP server (Provider + tools in one action)."""

    class Arguments:
        server_name = String(required=True)
        display_name = String(required=False)
        description = String(required=False)
        base_url = String(required=True)
        bearer_token = String(required=False)
        # Key-preserving JSON: header names must reach the upstream
        # verbatim (see SyncExternalMcpServer).
        headers = JSONSnakeCase(required=False)
        name_prefix = String(required=False)
        timeout = Int(required=False)
        updated_by = String(required=True)

    Output = CapabilityMcpRegistrationPayload

    @staticmethod
    def mutate(root, info, **kwargs):
        return _register_and_respond(
            info,
            handler_fn=sync_external_mcp_server,
            handler_kwargs={
                "server_name": kwargs["server_name"],
                "base_url": kwargs["base_url"],
                "bearer_token": kwargs.get("bearer_token"),
                "headers": kwargs.get("headers"),
                "name_prefix": kwargs.get("name_prefix"),
                "updated_by": kwargs["updated_by"],
                "timeout": kwargs.get("timeout"),
            },
            module_name=kwargs["server_name"],
            transport="remote",
            display_name=kwargs.get("display_name"),
            description=kwargs.get("description"),
            error_prefix="Failed to register remote MCP server",
        )


# ---------------------------------------------------------------------------
# 7.2 Custom MCP upload (S3 presign + Base64)
# ---------------------------------------------------------------------------


class GenerateCapabilityCustomMcpUploadUrl(Mutation):
    class Arguments:
        package_name = String(required=True)

    ok = Boolean(required=True)
    message = String()
    upload_url = String()
    s3_key = String()
    expires_at = DateTime()

    @staticmethod
    def mutate(root, info, **kwargs):
        if not Config.enable_s3_package_upload:
            return GenerateCapabilityCustomMcpUploadUrl(
                ok=False,
                message="S3 package upload is disabled; use the Git install path.",
            )
        try:
            result = generate_upload_url(
                package_name=kwargs["package_name"],
                logger=info.context.get("logger"),
            )
            return GenerateCapabilityCustomMcpUploadUrl(
                ok=True,
                upload_url=result["upload_url"],
                s3_key=result["s3_key"],
                expires_at=result["expires_at"],
            )
        except Exception as e:
            log = traceback.format_exc()
            if info.context.get("logger"):
                info.context["logger"].error(log)
            return GenerateCapabilityCustomMcpUploadUrl(
                ok=False, message=f"Failed to generate upload URL: {str(e)}"
            )


class RegisterCapabilityCustomMcpPackage(Mutation):
    class Arguments:
        s3_key = String(required=True)
        module_name = String(required=True)
        package_name = String(required=True)
        display_name = String(required=False)
        description = String(required=False)
        variables = JSONCamelCase(required=False)
        updated_by = String(required=True)

    Output = CapabilityMcpRegistrationPayload

    @staticmethod
    def mutate(root, info, **kwargs):
        if not Config.enable_s3_package_upload:
            return CapabilityMcpRegistrationPayload(
                ok=False,
                message="S3 package upload is disabled; use the Git install path.",
            )
        process_kwargs = {
            "s3_key": kwargs["s3_key"],
            "module_name": kwargs["module_name"],
            "package_name": kwargs["package_name"],
            "source": "s3",
            "updated_by": kwargs["updated_by"],
        }
        if kwargs.get("variables"):
            process_kwargs["variables"] = kwargs["variables"]
        return _register_and_respond(
            info,
            handler_fn=process_mcp_package,
            handler_kwargs=process_kwargs,
            module_name=kwargs["module_name"],
            transport="custom",
            display_name=kwargs.get("display_name"),
            description=kwargs.get("description"),
            error_prefix="Failed to register custom MCP package",
        )


class RegisterCapabilityCustomMcpPackageBase64(Mutation):
    class Arguments:
        package_base64 = String(required=True)
        module_name = String(required=True)
        package_name = String(required=True)
        display_name = String(required=False)
        description = String(required=False)
        variables = JSONCamelCase(required=False)
        updated_by = String(required=True)

    Output = CapabilityMcpRegistrationPayload

    @staticmethod
    def mutate(root, info, **kwargs):
        if not Config.enable_s3_package_upload:
            return CapabilityMcpRegistrationPayload(
                ok=False,
                message="Base64 package loading is disabled (S3 package upload is disabled); use the Git install path.",
            )
        process_kwargs = dict(kwargs)
        process_kwargs["source"] = "s3"
        return _register_and_respond(
            info,
            handler_fn=process_base64_package,
            handler_kwargs=process_kwargs,
            module_name=kwargs["module_name"],
            transport="custom",
            display_name=kwargs.get("display_name"),
            description=kwargs.get("description"),
            error_prefix="Failed to register custom MCP package",
        )


# ---------------------------------------------------------------------------
# 7.3 Custom MCP Git install
# ---------------------------------------------------------------------------


class RegisterCapabilityCustomMcpGitPackage(Mutation):
    class Arguments:
        git_url = String(required=True)
        module_name = String(required=True)
        package_name = String(required=False)
        ref = String(required=False)
        subdirectory = String(required=False)
        version_strategy = String(required=False)
        distribution_name = String(required=False)
        display_name = String(required=False)
        description = String(required=False)
        variables = JSONCamelCase(required=False)
        updated_by = String(required=True)

    Output = CapabilityMcpRegistrationPayload

    @staticmethod
    def mutate(root, info, **kwargs):
        module_name = kwargs["module_name"]
        package_name = kwargs.get("package_name") or module_name
        install_kwargs = {
            "git_url": kwargs["git_url"],
            "git_ref": kwargs.get("ref"),
            "git_subdirectory": kwargs.get("subdirectory"),
            "version_strategy": kwargs.get("version_strategy"),
            "distribution_name": kwargs.get("distribution_name"),
            "module_name": module_name,
            "package_name": package_name,
            "updated_by": kwargs["updated_by"],
        }
        if kwargs.get("variables"):
            install_kwargs["variables"] = kwargs["variables"]
        return _register_and_respond(
            info,
            handler_fn=install_mcp_package_from_git,
            handler_kwargs=install_kwargs,
            module_name=module_name,
            transport="custom",
            display_name=kwargs.get("display_name"),
            description=kwargs.get("description"),
            error_prefix="Failed to register Git package",
        )


class RefreshCapabilityCustomMcpGitPackage(Mutation):
    class Arguments:
        module_name = String(required=True)
        updated_by = String(required=True)

    Output = CapabilityMcpRegistrationPayload

    @staticmethod
    def mutate(root, info, **kwargs):
        try:
            result = refresh_mcp_git_package(
                info,
                module_name=kwargs["module_name"],
                updated_by=kwargs["updated_by"],
            )
            provider = _resolve_provider(info, kwargs["module_name"])
            return CapabilityMcpRegistrationPayload(
                ok=True,
                message=result.get("message", ""),
                provider=provider,
                transport="custom",
                stats=_stats_type(result.get("stats")),
                resolved_commit=result.get("resolved_commit"),
                installed_package_version=result.get(
                    "installed_package_version"
                ),
                action=result.get("action"),
            )
        except Exception as e:
            log = traceback.format_exc()
            if info.context.get("logger"):
                info.context["logger"].error(log)
            return CapabilityMcpRegistrationPayload(
                ok=False,
                message=f"Failed to refresh Git package: {str(e)}",
            )


# ---------------------------------------------------------------------------
# 7.4 Unified Register (single face-outside mutation)
# ---------------------------------------------------------------------------


class RegisterCapabilityMcp(Mutation):
    """Register any kind of MCP provider through one mutation.

    The ``transport`` argument selects the registration path; all other
    arguments are optional and only meaningful for specific transports.
    This is the recommended face-outside entry point for Banyanos clients.
    The individual ``registerCapability*`` mutations remain registered
    for backward compatibility and for callers that want schema-level
    required-field enforcement on a single path.

    Transport dispatch:

    - ``remote``  — registers a remote HTTP MCP server (requires
      ``baseUrl``; optional ``bearerToken``, ``headers``, ``namePrefix``,
      ``timeout``).  Delegates to ``sync_external_mcp_server()``.
    - ``s3``      — processes an already-uploaded S3 ZIP (requires
      ``s3Key``, ``moduleName``, ``packageName``; optional ``variables``).
      Delegates to ``process_mcp_package(source="s3")``.  Respects the
      ``enable_s3_package_upload`` gate.
    - ``base64``  — processes an inline Base64 ZIP (requires
      ``packageBase64``, ``moduleName``, ``packageName``; optional
      ``variables``).  Delegates to ``process_base64_package()``.  Respects
      the ``enable_s3_package_upload`` gate.
    - ``git``     — installs a package from a Git repository (requires
      ``gitUrl``, ``moduleName``; optional ``ref``, ``subdirectory``,
      ``versionStrategy``, ``distributionName``, ``packageName``,
      ``variables``).  Delegates to ``install_mcp_package_from_git()``.

    Common optional arguments: ``displayName``, ``description``,
    ``updatedBy`` (required).

    Returns ``CapabilityMcpRegistrationPayload`` for all paths.
    """

    class Arguments:
        # --- transport selector ---
        transport = String(
            required=True,
            description="remote | s3 | base64 | git",
        )

        # --- common ---
        module_name = String(required=True)
        package_name = String(required=False)
        display_name = String(required=False)
        description = String(required=False)
        variables = JSONCamelCase(required=False)
        updated_by = String(required=True)

        # --- remote ---
        base_url = String(required=False)
        bearer_token = String(required=False)
        headers = JSONSnakeCase(required=False)
        name_prefix = String(required=False)
        timeout = Int(required=False)

        # --- s3 ---
        s3_key = String(required=False)

        # --- base64 ---
        package_base64 = String(required=False)

        # --- git ---
        git_url = String(required=False)
        ref = String(required=False)
        subdirectory = String(required=False)
        version_strategy = String(required=False)
        distribution_name = String(required=False)

    Output = CapabilityMcpRegistrationPayload

    @staticmethod
    def mutate(root, info, **kwargs):
        transport = (kwargs.get("transport") or "").lower().strip()
        module_name = kwargs["module_name"]

        if transport == "remote":
            if not kwargs.get("base_url"):
                return CapabilityMcpRegistrationPayload(
                    ok=False,
                    message="baseUrl is required for transport=remote",
                )
            return _register_and_respond(
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
                    ok=False,
                    message="s3Key is required for transport=s3",
                )
            if not Config.enable_s3_package_upload:
                return CapabilityMcpRegistrationPayload(
                    ok=False,
                    message="S3 package upload is disabled; use transport=git.",
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
            return _register_and_respond(
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
                    ok=False,
                    message="packageBase64 is required for transport=base64",
                )
            if not Config.enable_s3_package_upload:
                return CapabilityMcpRegistrationPayload(
                    ok=False,
                    message="Base64 package loading is disabled; use transport=git.",
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
            return _register_and_respond(
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
                    ok=False,
                    message="gitUrl is required for transport=git",
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
            return _register_and_respond(
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
            message=(
                f"Unknown transport '{transport}'. "
                f"Supported: remote, s3, base64, git."
            ),
        )


class CheckCapabilityCustomMcpGitPackageVersion(Mutation):
    class Arguments:
        module_name = String(required=True)

    Output = CapabilityMcpGitVersionInfo

    @staticmethod
    def mutate(root, info, **kwargs):
        try:
            result = check_mcp_git_package_version(
                info, module_name=kwargs["module_name"]
            )
            return CapabilityMcpGitVersionInfo(
                ok=True,
                message=result.get("message", ""),
                needs_refresh=result.get("needs_refresh", False),
                local_commit=result.get("local_commit"),
                remote_commit=result.get("remote_commit"),
                installed_package_version=result.get(
                    "installed_package_version"
                ),
                latest_remote_version=result.get("latest_remote_version"),
                last_checked_at=result.get("last_checked_at"),
            )
        except Exception as e:
            log = traceback.format_exc()
            if info.context.get("logger"):
                info.context["logger"].error(log)
            return CapabilityMcpGitVersionInfo(
                ok=False,
                message=f"Failed to check Git package version: {str(e)}",
            )


# ---------------------------------------------------------------------------
# 7.5 Invocation
# ---------------------------------------------------------------------------


class InvokeCapabilityMcpTool(Mutation):
    class Arguments:
        name = String(required=True)
        arguments = JSONCamelCase(required=False)
        agent_id = String(required=False)
        trace_id = String(required=False)
        updated_by = String(required=True)

    Output = CapabilityMcpInvocationType

    @staticmethod
    def mutate(root, info, **kwargs):
        try:
            from ..handlers.capability_mcp_invoke import invoke_capability_tool

            invocation = invoke_capability_tool(
                info,
                name=kwargs["name"],
                arguments=kwargs.get("arguments"),
                agent_id=kwargs.get("agent_id"),
                trace_id=kwargs.get("trace_id"),
                updated_by=kwargs["updated_by"],
            )
            # invoke_capability_tool includes a UI-friendly "message" that
            # the invocation type doesn't declare (it belongs on
            # TestCapabilityMcpTool's payload). Strip it before splatting.
            return CapabilityMcpInvocationType(
                **{k: v for k, v in invocation.items() if k != "message"}
            )
        except Exception as e:
            log = traceback.format_exc()
            if info.context.get("logger"):
                info.context["logger"].error(log)
            return CapabilityMcpInvocationType(
                id="",
                tool_name=kwargs.get("name", ""),
                status="failed",
                error_message=str(e),
            )


class TestCapabilityMcpTool(Mutation):
    class Arguments:
        name = String(required=True)
        arguments = JSONCamelCase(required=False)
        updated_by = String(required=True)

    ok = Boolean(required=True)
    message = String()
    invocation = Field(CapabilityMcpInvocationType)

    @staticmethod
    def mutate(root, info, **kwargs):
        try:
            from ..handlers.capability_mcp_invoke import invoke_capability_tool

            invocation = invoke_capability_tool(
                info,
                name=kwargs["name"],
                arguments=kwargs.get("arguments"),
                updated_by=kwargs["updated_by"],
            )
            return TestCapabilityMcpTool(
                ok=invocation.get("status") != "failed",
                message=invocation.get("message", ""),
                invocation=CapabilityMcpInvocationType(
                    **{k: v for k, v in invocation.items() if k != "message"}
                ),
            )
        except Exception as e:
            log = traceback.format_exc()
            if info.context.get("logger"):
                info.context["logger"].error(log)
            return TestCapabilityMcpTool(
                ok=False,
                message=f"Failed to test MCP tool: {str(e)}",
            )


# ---------------------------------------------------------------------------
# 7.6 Provider deregistration (remove/undeploy)
# ---------------------------------------------------------------------------
#
# Symmetric removal for each register path. Each unregister mutation:
#   1. Looks up the MCPModule row for module_name in the current partition.
#   2. Verifies its source matches the mutation's transport (remote -> external,
#      s3 -> "s3", git -> "git"). Refuses with ok=false otherwise, so callers
#      can't accidentally delete the wrong kind of provider through the wrong
#      mutation.
#   3. Deletes every MCPFunction row for this module_name.
#   4. Deletes the MCPModule row.
#   5. Deletes the shared MCPSetting row iff no other module still references
#      its setting_id (fail-safe: if we cannot verify, we keep the setting).
#   6. Cache invalidation is automatic — these mutations are in
#      _CONFIG_MUTATIONS.
#
# Never touches disk (funct_extract_path) or the S3 bucket. Those artifacts
# outlive a single provider's DB registration by design; a separate garbage
# collector can prune stale extracts/objects.


def _module_setting_id(module: Any) -> Any:
    """Extract the setting_id from a module's first class entry, if present."""
    classes = (
        module.get("classes")
        if isinstance(module, dict)
        else getattr(module, "classes", None)
    ) or []
    for cls in classes:
        cls_dict = (
            cls
            if isinstance(cls, dict)
            else {
                "class_name": getattr(cls, "class_name", None),
                "setting_id": getattr(cls, "setting_id", None),
            }
        )
        if cls_dict.get("setting_id"):
            return cls_dict["setting_id"]
    return None


def _setting_still_referenced(
    partition_key: str, setting_id: Any, excluding_module_name: str
) -> bool:
    """True if some module OTHER than ``excluding_module_name`` still points
    at ``setting_id``. Fail-safe: returns True on any error, so a partial
    view never leads to deleting a shared setting."""
    from ..queries.capability_mcp import _info_stub

    try:
        result = get_repo("mcp_module").list(_info_stub(partition_key), limit=10000)
        for m in getattr(result, "mcp_module_list", None) or []:
            name = (
                m.get("module_name")
                if isinstance(m, dict)
                else getattr(m, "module_name", None)
            )
            if name == excluding_module_name:
                continue
            if _module_setting_id(m) == setting_id:
                return True
    except Exception:
        return True
    return False


def _transport_label_for_source(source: Any) -> str:
    """Map an MCPModule.source value to its compatibility transport label."""
    if source == "external":
        return "remote"
    return "custom"


def _resolve_expected_sources(hint: Any) -> tuple:
    """Translate a caller-provided transport/source hint into an
    MCPModule.source filter. Empty tuple = accept any source.

    Accepts (case-insensitive): "remote" or "external" → external only;
    "s3" → S3 upload only; "git" → Git install only; "custom" → s3 or git;
    "builtin" → no restriction (BUILTIN is a read-view classification;
    to remove a built-in you remove its underlying install).
    Unknown values fall through to no restriction.
    """
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


def _unregister_module(
    info,
    module_name: str,
    expected_sources: tuple = (),
) -> Dict[str, Any]:
    """Delete a module's rows in the fixed order functions → module → setting.

    ``expected_sources`` is an optional safety guard — if non-empty, the
    delete is refused unless the module's stored source is one of the
    listed values. Pass an empty tuple to allow any source.

    The transport label in the return payload is derived from the actual
    stored source so callers can identify what was removed.
    """
    partition_key = info.context["partition_key"]
    logger = info.context.get("logger") or Config.logger

    try:
        module_row = get_repo("mcp_module").get(
            partition_key=partition_key, module_name=module_name
        )
    except Exception as lookup_err:
        # Log so a "not found" caused by a session/RLS/permission failure
        # is visible instead of silently masked as "module doesn't exist".
        logger.warning(
            f"MCPModule lookup failed for '{module_name}' in partition "
            f"'{partition_key}': {lookup_err!r}"
        )
        module_row = None
    if not module_row:
        return {
            "ok": False,
            "message": f"Module '{module_name}' not found",
            "module_name": module_name,
            "transport": None,
        }

    source = (
        module_row.get("source")
        if isinstance(module_row, dict)
        else getattr(module_row, "source", None)
    )
    transport_label = _transport_label_for_source(source)
    if expected_sources and source not in expected_sources:
        return {
            "ok": False,
            "message": (
                f"Module '{module_name}' has source={source!r}, cannot "
                f"unregister via this mutation (expected one of "
                f"{list(expected_sources)})"
            ),
            "module_name": module_name,
            "transport": transport_label,
            "source": source,
        }

    setting_id = _module_setting_id(module_row)

    # 1. Delete every MCPFunction row for this module (list from the cached
    #    config to avoid a filtered repo query per module — see review §7).
    from ..queries.capability_mcp import _info_stub

    deleted_functions = 0
    try:
        function_names = []
        cached = Config.fetch_mcp_configuration(partition_key) or {}
        for fn in cached.get("tools", []) + cached.get("resources", []) + cached.get("prompts", []):
            if fn.get("module_name") == module_name and fn.get("name"):
                function_names.append(fn["name"])
        # Fallback to a live list if the cache was empty/stale.
        if not function_names:
            live = get_repo("mcp_function").list(
                _info_stub(partition_key),
                module_name=module_name,
                limit=10000,
            )
            for fn in getattr(live, "mcp_function_list", None) or []:
                name = (
                    fn.get("name")
                    if isinstance(fn, dict)
                    else getattr(fn, "name", None)
                )
                if name:
                    function_names.append(name)

        for name in function_names:
            try:
                get_repo("mcp_function").delete(info, name=name)
                deleted_functions += 1
            except Exception as e:
                logger.warning(
                    f"Failed to delete function '{name}' for module "
                    f"'{module_name}': {e}"
                )
    except Exception as e:
        logger.warning(
            f"Failed to enumerate functions for module '{module_name}': {e}"
        )

    # 2. Delete the MCPModule row.
    module_deleted = False
    try:
        get_repo("mcp_module").delete(info, module_name=module_name)
        module_deleted = True
    except Exception as e:
        logger.error(f"Failed to delete module '{module_name}': {e}")
        return {
            "ok": False,
            "message": f"Failed to delete module row: {e}",
            "module_name": module_name,
            "transport": transport_label,
            "source": source,
            "deleted_functions": deleted_functions,
        }

    # 3. Delete the shared MCPSetting iff nothing else uses it.
    setting_kept = False
    deleted_setting = False
    if setting_id:
        if _setting_still_referenced(
            partition_key, setting_id, excluding_module_name=module_name
        ):
            setting_kept = True
        else:
            try:
                get_repo("mcp_setting").delete(info, setting_id=setting_id)
                deleted_setting = True
            except Exception as e:
                logger.warning(
                    f"Failed to delete orphaned setting '{setting_id}': {e}"
                )

    return {
        "ok": True,
        "message": (
            f"Unregistered '{module_name}' (source={source}): deleted "
            f"{deleted_functions} function(s) and the module row"
            + (
                "; also deleted the orphaned setting"
                if deleted_setting
                else ("; kept shared setting" if setting_kept else "")
            )
        ),
        "module_name": module_name,
        "transport": transport_label,
        "source": source,
        "deleted_functions": deleted_functions,
        "deleted_setting": deleted_setting,
        "setting_kept": setting_kept,
    }


class UnregisterCapabilityMcp(Mutation):
    """Remove a registered capability MCP provider by ``moduleName``.

    Handles remote (``source="external"``), S3-uploaded (``source="s3"``),
    and Git-installed (``source="git"``) providers uniformly. The stored
    source is looked up from the module row, so callers only need the
    module name.

    An optional ``expectedTransport`` guard refuses the delete if the
    stored source doesn't match the caller's expectation — useful for UI
    flows that want to confirm they're removing the right kind of provider.
    Accepted values (case-insensitive):
        - ``"remote"`` or ``"external"`` — external HTTP MCP only
        - ``"s3"``   — S3-uploaded packages only
        - ``"git"``  — Git-installed packages only
        - ``"custom"`` — S3 or Git (either kind of custom package)
        - ``"builtin"`` — no restriction (BUILTIN is a read-view label
          computed from deployment config; the underlying provider is
          still one of the above sources)
        - omitted / unknown value — accept any source

    Does NOT delete the extracted install dir under ``funct_extract_path``
    or the S3 ZIP object — those artifacts outlive a single DB registration
    by design. Cache is invalidated automatically via ``_CONFIG_MUTATIONS``.
    """

    class Arguments:
        module_name = String(required=True)
        expected_transport = String(required=False)
        updated_by = String(required=True)

    ok = Boolean(required=True)
    message = String()
    module_name = String()
    transport = String()
    source = String()
    deleted_functions = Int()
    deleted_setting = Boolean()
    setting_kept = Boolean()

    @staticmethod
    def mutate(root, info, **kwargs):
        try:
            result = _unregister_module(
                info,
                module_name=kwargs["module_name"],
                expected_sources=_resolve_expected_sources(
                    kwargs.get("expected_transport")
                ),
            )
            return UnregisterCapabilityMcp(**result)
        except Exception as e:
            log = traceback.format_exc()
            if info.context.get("logger"):
                info.context["logger"].error(log)
            return UnregisterCapabilityMcp(
                ok=False,
                message=f"Failed to unregister capability MCP provider: {str(e)}",
            )