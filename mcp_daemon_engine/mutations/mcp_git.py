# -*- coding: utf-8 -*-
"""GraphQL mutations for Git-based MCP package installation.

Implements:
- ``InstallMcpPackageFromGit`` — install or refresh a module from a Git URL.
- ``CheckMcpGitPackageVersion`` — check whether a Git module needs refresh.
- ``RefreshMcpGitPackage`` — refresh an already-installed Git module.

See ``docs/GIT_MODULE_INSTALL_DEV_PLAN.md`` for the full design.
"""
from __future__ import print_function

__author__ = "bibow"

import traceback
from typing import Any, Dict

from graphene import Boolean, DateTime, Field, Mutation, String
from silvaengine_utility import JSONCamelCase

from ..handlers.mcp_git import (
    check_mcp_git_package_version,
    install_mcp_package_from_git,
    refresh_mcp_git_package,
)
from ..types.mcp_configuration_stats import McpConfigurationStats


class InstallMcpPackageFromGit(Mutation):
    """Install or refresh an MCP module from a Git repository."""

    class Arguments:
        git_url = String(required=True)
        git_ref = String(required=False)
        git_subdirectory = String(required=False)
        version_strategy = String(required=False)
        distribution_name = String(required=False)
        force_refresh = Boolean(required=False)
        module_name = String(required=True)
        package_name = String(required=True)
        variables = JSONCamelCase(required=False)
        updated_by = String(required=True)

    ok = Boolean(required=True)
    message = String()
    stats = Field(McpConfigurationStats)
    resolved_commit = String()
    installed_package_version = String()
    action = String()

    @staticmethod
    def mutate(
        root: Any, info: Any, **kwargs: Dict[str, Any]
    ) -> "InstallMcpPackageFromGit":
        try:
            result = install_mcp_package_from_git(info, **kwargs)
            return InstallMcpPackageFromGit(
                ok=True,
                message=result.get("message", ""),
                stats=McpConfigurationStats(**result.get("stats", {
                    "tools": 0, "resources": 0, "prompts": 0,
                    "modules": 0, "settings": 0,
                })),
                resolved_commit=result.get("resolved_commit", ""),
                installed_package_version=result.get(
                    "installed_package_version", ""
                ),
                action=result.get("action", ""),
            )
        except Exception as e:
            log = traceback.format_exc()
            if info.context.get("logger"):
                info.context["logger"].error(log)
            return InstallMcpPackageFromGit(
                ok=False,
                message=f"Failed to install MCP package from Git: {str(e)}",
            )


class CheckMcpGitPackageVersion(Mutation):
    """Check whether a Git-installed MCP module needs refresh."""

    class Arguments:
        module_name = String(required=True)
        force_check = Boolean(required=False)

    ok = Boolean(required=True)
    message = String()
    needs_refresh = Boolean()
    local_commit = String()
    remote_commit = String()
    installed_package_version = String()
    latest_remote_version = String()
    last_checked_at = DateTime()

    @staticmethod
    def mutate(
        root: Any, info: Any, **kwargs: Dict[str, Any]
    ) -> "CheckMcpGitPackageVersion":
        try:
            result = check_mcp_git_package_version(info, **kwargs)
            return CheckMcpGitPackageVersion(
                ok=True,
                message=result.get("message", ""),
                needs_refresh=result.get("needs_refresh", False),
                local_commit=result.get("local_commit", ""),
                remote_commit=result.get("remote_commit", ""),
                installed_package_version=result.get(
                    "installed_package_version", ""
                ),
                latest_remote_version=result.get(
                    "latest_remote_version", ""
                ),
                last_checked_at=result.get("last_checked_at"),
            )
        except Exception as e:
            log = traceback.format_exc()
            if info.context.get("logger"):
                info.context["logger"].error(log)
            return CheckMcpGitPackageVersion(
                ok=False,
                message=f"Failed to check Git package version: {str(e)}",
            )


class RefreshMcpGitPackage(Mutation):
    """Refresh an already-installed Git MCP module using persisted metadata."""

    class Arguments:
        module_name = String(required=True)
        force_refresh = Boolean(required=False)
        updated_by = String(required=True)

    ok = Boolean(required=True)
    message = String()
    stats = Field(McpConfigurationStats)
    resolved_commit = String()
    installed_package_version = String()
    action = String()

    @staticmethod
    def mutate(
        root: Any, info: Any, **kwargs: Dict[str, Any]
    ) -> "RefreshMcpGitPackage":
        try:
            result = refresh_mcp_git_package(info, **kwargs)
            return RefreshMcpGitPackage(
                ok=True,
                message=result.get("message", ""),
                stats=McpConfigurationStats(**result.get("stats", {
                    "tools": 0, "resources": 0, "prompts": 0,
                    "modules": 0, "settings": 0,
                })),
                resolved_commit=result.get("resolved_commit", ""),
                installed_package_version=result.get(
                    "installed_package_version", ""
                ),
                action=result.get("action", ""),
            )
        except Exception as e:
            log = traceback.format_exc()
            if info.context.get("logger"):
                info.context["logger"].error(log)
            return RefreshMcpGitPackage(
                ok=False,
                message=f"Failed to refresh Git MCP package: {str(e)}",
            )