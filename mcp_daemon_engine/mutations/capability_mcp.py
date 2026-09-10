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
    CapabilityMcpProvider as CapabilityMcpProviderType,
    CapabilityMcpRegistrationPayload,
    CapabilityMcpSyncStats,
    CapabilityMcpTool as _CapabilityMcpToolType,
    CapabilityMcpTransport as CapabilityMcpTransportType,
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

    ok = Boolean(required=True)
    message = String()
    provider = Field(CapabilityMcpProviderType)
    transport = CapabilityMcpTransportType()
    stats = Field(CapabilityMcpSyncStats)

    @staticmethod
    def mutate(root, info, **kwargs):
        try:
            stats = sync_external_mcp_server(
                info,
                server_name=kwargs["server_name"],
                base_url=kwargs["base_url"],
                bearer_token=kwargs.get("bearer_token"),
                headers=kwargs.get("headers"),
                name_prefix=kwargs.get("name_prefix"),
                updated_by=kwargs["updated_by"],
                timeout=kwargs.get("timeout"),
            )
            _store_capability_metadata(
                info,
                kwargs["server_name"],
                display_name=kwargs.get("display_name"),
                description=kwargs.get("description"),
            )
            provider = _resolve_provider(info, kwargs["server_name"])
            return RegisterCapabilityRemoteMcp(
                ok=True,
                message=(
                    f"Successfully registered remote MCP server "
                    f"'{kwargs['server_name']}': {stats['tools']} tools, "
                    f"{stats['resources']} resources, {stats['prompts']} prompts."
                ),
                provider=provider,
                transport="remote",
                stats=_stats_type(stats),
            )
        except Exception as e:
            log = traceback.format_exc()
            if info.context.get("logger"):
                info.context["logger"].error(log)
            return RegisterCapabilityRemoteMcp(
                ok=False,
                message=f"Failed to register remote MCP server: {str(e)}",
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

    ok = Boolean(required=True)
    message = String()
    provider = Field(CapabilityMcpProviderType)
    transport = CapabilityMcpTransportType()
    stats = Field(CapabilityMcpSyncStats)

    @staticmethod
    def mutate(root, info, **kwargs):
        if not Config.enable_s3_package_upload:
            return RegisterCapabilityCustomMcpPackage(
                ok=False,
                message="S3 package upload is disabled; use the Git install path.",
            )
        try:
            process_kwargs = {
                "s3_key": kwargs["s3_key"],
                "module_name": kwargs["module_name"],
                "package_name": kwargs["package_name"],
                "source": "s3",
                "updated_by": kwargs["updated_by"],
            }
            if kwargs.get("variables"):
                process_kwargs["variables"] = kwargs["variables"]

            stats = process_mcp_package(info, **process_kwargs)
            _store_capability_metadata(
                info,
                kwargs["module_name"],
                display_name=kwargs.get("display_name"),
                description=kwargs.get("description"),
            )
            provider = _resolve_provider(info, kwargs["module_name"])
            return RegisterCapabilityCustomMcpPackage(
                ok=True,
                message=(
                    f"Successfully registered custom MCP package "
                    f"'{kwargs['module_name']}': {stats['tools']} tools, "
                    f"{stats['resources']} resources, {stats['prompts']} prompts."
                ),
                provider=provider,
                transport="custom",
                stats=_stats_type(stats),
            )
        except Exception as e:
            log = traceback.format_exc()
            if info.context.get("logger"):
                info.context["logger"].error(log)
            return RegisterCapabilityCustomMcpPackage(
                ok=False,
                message=f"Failed to register custom MCP package: {str(e)}",
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

    ok = Boolean(required=True)
    message = String()
    provider = Field(CapabilityMcpProviderType)
    transport = CapabilityMcpTransportType()
    stats = Field(CapabilityMcpSyncStats)

    @staticmethod
    def mutate(root, info, **kwargs):
        # Inherit the enable_s3_package_upload gate with a clear disabled
        # message so Banyanos clients don't silently retry (plan Â§10/Â§11).
        if not Config.enable_s3_package_upload:
            return RegisterCapabilityCustomMcpPackageBase64(
                ok=False,
                message="Base64 package loading is disabled (S3 package upload is disabled); use the Git install path.",
            )
        try:
            process_kwargs = dict(kwargs)
            process_kwargs["source"] = "s3"
            stats = process_base64_package(info, **process_kwargs)
            _store_capability_metadata(
                info,
                kwargs["module_name"],
                display_name=kwargs.get("display_name"),
                description=kwargs.get("description"),
            )
            provider = _resolve_provider(info, kwargs["module_name"])
            return RegisterCapabilityCustomMcpPackageBase64(
                ok=True,
                message=(
                    f"Successfully registered custom MCP package "
                    f"'{kwargs['module_name']}': {stats['tools']} tools, "
                    f"{stats['resources']} resources, {stats['prompts']} prompts."
                ),
                provider=provider,
                transport="custom",
                stats=_stats_type(stats),
            )
        except Exception as e:
            log = traceback.format_exc()
            if info.context.get("logger"):
                info.context["logger"].error(log)
            return RegisterCapabilityCustomMcpPackageBase64(
                ok=False,
                message=f"Failed to register custom MCP package: {str(e)}",
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

    ok = Boolean(required=True)
    message = String()
    provider = Field(CapabilityMcpProviderType)
    transport = CapabilityMcpTransportType()
    stats = Field(CapabilityMcpSyncStats)
    resolved_commit = String()
    installed_package_version = String()
    action = String()

    @staticmethod
    def mutate(root, info, **kwargs):
        try:
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

            result = install_mcp_package_from_git(info, **install_kwargs)
            if not result.get("action"):
                raise Exception(result.get("message", "install failed"))
            _store_capability_metadata(
                info, module_name,
                display_name=kwargs.get("display_name"),
                description=kwargs.get("description"),
            )
            provider = _resolve_provider(info, module_name)
            return RegisterCapabilityCustomMcpGitPackage(
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
            return RegisterCapabilityCustomMcpGitPackage(
                ok=False,
                message=f"Failed to register Git package: {str(e)}",
            )


class RefreshCapabilityCustomMcpGitPackage(Mutation):
    class Arguments:
        module_name = String(required=True)
        updated_by = String(required=True)

    ok = Boolean(required=True)
    message = String()
    provider = Field(CapabilityMcpProviderType)
    transport = CapabilityMcpTransportType()
    stats = Field(CapabilityMcpSyncStats)
    resolved_commit = String()
    installed_package_version = String()
    action = String()

    @staticmethod
    def mutate(root, info, **kwargs):
        try:
            result = refresh_mcp_git_package(
                info,
                module_name=kwargs["module_name"],
                updated_by=kwargs["updated_by"],
            )
            provider = _resolve_provider(info, kwargs["module_name"])
            return RefreshCapabilityCustomMcpGitPackage(
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
            return RefreshCapabilityCustomMcpGitPackage(
                ok=False,
                message=f"Failed to refresh Git package: {str(e)}",
            )


class CheckCapabilityCustomMcpGitPackageVersion(Mutation):
    class Arguments:
        module_name = String(required=True)

    ok = Boolean(required=True)
    message = String()
    needs_refresh = Boolean()
    local_commit = String()
    remote_commit = String()
    installed_package_version = String()
    latest_remote_version = String()
    last_checked_at = DateTime()

    @staticmethod
    def mutate(root, info, **kwargs):
        try:
            result = check_mcp_git_package_version(
                info, module_name=kwargs["module_name"]
            )
            return CheckCapabilityCustomMcpGitPackageVersion(
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
            return CheckCapabilityCustomMcpGitPackageVersion(
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
                agent_id=kwargs.get("agent_id"),
                trace_id=kwargs.get("trace_id"),
                updated_by=kwargs["updated_by"],
            )
            return InvokeCapabilityMcpTool(
                ok=invocation.get("status") != "failed",
                message=invocation.get("message", ""),
                invocation=CapabilityMcpInvocationType(**invocation),
            )
        except Exception as e:
            log = traceback.format_exc()
            if info.context.get("logger"):
                info.context["logger"].error(log)
            return InvokeCapabilityMcpTool(
                ok=False,
                message=f"Failed to invoke MCP tool: {str(e)}",
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
                invocation=CapabilityMcpInvocationType(**invocation),
            )
        except Exception as e:
            log = traceback.format_exc()
            if info.context.get("logger"):
                info.context["logger"].error(log)
            return TestCapabilityMcpTool(
                ok=False,
                message=f"Failed to test MCP tool: {str(e)}",
            )