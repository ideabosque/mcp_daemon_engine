# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

import traceback
from typing import Any, Dict

from graphene import Boolean, DateTime, Field, Mutation, String
from silvaengine_utility import JSONCamelCase

from ..handlers.mcp_handlers import (
    generate_upload_url,
    process_mcp_package,
)
from ..types.mcp_configuration_stats import McpConfigurationStats


class GenerateMcpPackageUploadUrl(Mutation):
    ok = Boolean(required=True)
    message = String()
    upload_url = String()
    s3_key = String()
    expires_at = DateTime()

    class Arguments:
        package_name = String(required=True)

    @staticmethod
    def mutate(
        root: Any, info: Any, **kwargs: Dict[str, Any]
    ) -> "GenerateMcpPackageUploadUrl":
        try:
            package_name = kwargs["package_name"]

            result = generate_upload_url(
                package_name=package_name,
                logger=info.context.get("logger"),
            )

            return GenerateMcpPackageUploadUrl(
                ok=True,
                upload_url=result["upload_url"],
                s3_key=result["s3_key"],
                expires_at=result["expires_at"],
            )

        except Exception as e:
            log = traceback.format_exc()
            if info.context.get("logger"):
                info.context["logger"].error(log)
            return GenerateMcpPackageUploadUrl(
                ok=False, message=f"Failed to generate upload URL: {str(e)}"
            )


class ProcessMcpPackage(Mutation):
    ok = Boolean(required=True)
    message = String()
    stats = Field(McpConfigurationStats)

    class Arguments:
        s3_key = String(required=True)
        module_name = String(required=True)
        package_name = String(required=True)
        source = String(required=False)
        variables = JSONCamelCase(required=False)
        updated_by = String(required=True)

    @staticmethod
    def mutate(
        root: Any, info: Any, **kwargs: Dict[str, Any]
    ) -> "ProcessMcpPackage":
        try:
            stats = process_mcp_package(info, **kwargs)

            message = (
                f"Successfully loaded MCP configuration: "
                f"{stats['tools']} tools, {stats['resources']} resources, "
                f"{stats['prompts']} prompts, {stats['modules']} modules, "
                f"{stats['settings']} settings"
            )

            return ProcessMcpPackage(
                ok=True,
                message=message,
                stats=McpConfigurationStats(**stats),
            )

        except Exception as e:
            log = traceback.format_exc()
            if info.context.get("logger"):
                info.context["logger"].error(log)
            return ProcessMcpPackage(
                ok=False, message=f"Failed to process MCP package: {str(e)}"
            )
