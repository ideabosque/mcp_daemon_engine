#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Capability Engine MCP compatibility mutations.

Thin graphene class definitions only — all business logic lives in
``handlers/capability_mcp.py`` and ``handlers/capability_mcp_invoke.py``.
"""
from __future__ import print_function

__author__ = "bibow"

import traceback

from graphene import Boolean, DateTime, Field, Int, Mutation, String
from silvaengine_utility import JSONCamelCase, JSONSnakeCase

from ..handlers.capability_mcp import (
    register_and_respond,
    register_capability_mcp as _register_capability_mcp,
    resolve_provider,
    unregister_capability_mcp as _unregister_capability_mcp,
)
from ..handlers.capability_mcp_invoke import invoke_capability_tool
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
    CapabilityMcpRegistrationPayload,
)


# ---------------------------------------------------------------------------
# 7.1 Remote MCP registration
# ---------------------------------------------------------------------------


class RegisterCapabilityRemoteMcp(Mutation):
    class Arguments:
        server_name = String(required=True)
        display_name = String(required=False)
        description = String(required=False)
        base_url = String(required=True)
        bearer_token = String(required=False)
        headers = JSONSnakeCase(required=False)
        name_prefix = String(required=False)
        timeout = Int(required=False)
        updated_by = String(required=True)

    Output = CapabilityMcpRegistrationPayload

    @staticmethod
    def mutate(root, info, **kwargs):
        return register_and_respond(
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
            if info.context.get("logger"):
                info.context["logger"].error(traceback.format_exc())
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
        handler_kwargs = {
            "s3_key": kwargs["s3_key"],
            "module_name": kwargs["module_name"],
            "package_name": kwargs["package_name"],
            "source": "s3",
            "updated_by": kwargs["updated_by"],
        }
        if kwargs.get("variables"):
            handler_kwargs["variables"] = kwargs["variables"]
        return register_and_respond(
            info,
            handler_fn=process_mcp_package,
            handler_kwargs=handler_kwargs,
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
                message="Base64 package loading is disabled; use the Git install path.",
            )
        handler_kwargs = dict(kwargs)
        handler_kwargs["source"] = "s3"
        return register_and_respond(
            info,
            handler_fn=process_base64_package,
            handler_kwargs=handler_kwargs,
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
        install_kwargs = {
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
            install_kwargs["variables"] = kwargs["variables"]
        return register_and_respond(
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
            provider = resolve_provider(info, kwargs["module_name"])
            from ..types.capability_mcp import CapabilityMcpSyncStats

            return CapabilityMcpRegistrationPayload(
                ok=True,
                message=result.get("message", ""),
                provider=provider,
                transport="custom",
                stats=CapabilityMcpSyncStats(
                    tools=(result.get("stats") or {}).get("tools", 0),
                    resources=(result.get("stats") or {}).get("resources", 0),
                    prompts=(result.get("stats") or {}).get("prompts", 0),
                    modules=(result.get("stats") or {}).get("modules", 0),
                    settings=(result.get("stats") or {}).get("settings", 0),
                ),
                resolved_commit=result.get("resolved_commit"),
                installed_package_version=result.get("installed_package_version"),
                action=result.get("action"),
            )
        except Exception as e:
            if info.context.get("logger"):
                info.context["logger"].error(traceback.format_exc())
            return CapabilityMcpRegistrationPayload(
                ok=False, message=f"Failed to refresh Git package: {str(e)}",
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
                installed_package_version=result.get("installed_package_version"),
                latest_remote_version=result.get("latest_remote_version"),
                last_checked_at=result.get("last_checked_at"),
            )
        except Exception as e:
            if info.context.get("logger"):
                info.context["logger"].error(traceback.format_exc())
            return CapabilityMcpGitVersionInfo(
                ok=False, message=f"Failed to check Git package version: {str(e)}",
            )


# ---------------------------------------------------------------------------
# 7.4 Unified Register (single face-outside mutation)
# ---------------------------------------------------------------------------


class RegisterCapabilityMcp(Mutation):
    """Register any kind of MCP provider through one mutation.

    transport: "remote" | "s3" | "base64" | "git"
    See handlers/capability_mcp.py:register_capability_mcp for dispatch.
    """

    class Arguments:
        transport = String(required=True, description="remote | s3 | base64 | git")
        module_name = String(required=True)
        package_name = String(required=False)
        display_name = String(required=False)
        description = String(required=False)
        variables = JSONCamelCase(required=False)
        updated_by = String(required=True)
        base_url = String(required=False)
        bearer_token = String(required=False)
        headers = JSONSnakeCase(required=False)
        name_prefix = String(required=False)
        timeout = Int(required=False)
        s3_key = String(required=False)
        package_base64 = String(required=False)
        git_url = String(required=False)
        ref = String(required=False)
        subdirectory = String(required=False)
        version_strategy = String(required=False)
        distribution_name = String(required=False)

    Output = CapabilityMcpRegistrationPayload

    @staticmethod
    def mutate(root, info, **kwargs):
        return _register_capability_mcp(info, **kwargs)


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
            invocation = invoke_capability_tool(
                info,
                name=kwargs["name"],
                arguments=kwargs.get("arguments"),
                agent_id=kwargs.get("agent_id"),
                trace_id=kwargs.get("trace_id"),
                updated_by=kwargs["updated_by"],
            )
            return CapabilityMcpInvocationType(
                **{k: v for k, v in invocation.items() if k != "message"}
            )
        except Exception as e:
            if info.context.get("logger"):
                info.context["logger"].error(traceback.format_exc())
            return CapabilityMcpInvocationType(
                id="", tool_name=kwargs.get("name", ""),
                status="failed", error_message=str(e),
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
            if info.context.get("logger"):
                info.context["logger"].error(traceback.format_exc())
            return TestCapabilityMcpTool(
                ok=False, message=f"Failed to test MCP tool: {str(e)}",
            )


# ---------------------------------------------------------------------------
# 7.6 Provider deregistration
# ---------------------------------------------------------------------------


class UnregisterCapabilityMcp(Mutation):
    """Remove a registered capability MCP provider by moduleName.

    expectedTransport (optional): "remote"/"external", "s3", "git",
    "custom", "builtin", or omitted. See handlers/capability_mcp.py.
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
            result = _unregister_capability_mcp(
                info,
                module_name=kwargs["module_name"],
                expected_transport=kwargs.get("expected_transport"),
                updated_by=kwargs["updated_by"],
            )
            return UnregisterCapabilityMcp(**result)
        except Exception as e:
            if info.context.get("logger"):
                info.context["logger"].error(traceback.format_exc())
            return UnregisterCapabilityMcp(
                ok=False,
                message=f"Failed to unregister capability MCP provider: {str(e)}",
            )