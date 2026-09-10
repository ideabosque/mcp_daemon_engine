#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Capability Engine MCP compatibility GraphQL types.

Banyanos-shaped projections over daemon-native rows (MCPModule / MCPFunction /
MCPFunctionCall). Provider and MCP Server are read views over a single
MCPModule row plus its MCPSetting — no daemon-side Provider table is created.
"""
from __future__ import print_function

__author__ = "bibow"

from graphene import (
    Boolean,
    DateTime,
    Enum,
    Field,
    ID,
    Int,
    List,
    ObjectType,
    String,
)
from silvaengine_utility import JSONCamelCase


class CapabilityMcpTransport(Enum):
    """Transport classification for the compatibility read view.

    REMOTE  — daemon external MCP server (source="external").
    CUSTOM  — uploaded or Git-installed module package.
    BUILTIN — module name appears in Config.setting["builtin_modules"].
    """

    REMOTE = "remote"
    CUSTOM = "custom"
    BUILTIN = "builtin"


class CapabilityMcpProvider(ObjectType):
    """Provider / MCP Server compatibility view over MCPModule + MCPSetting."""

    id = ID(required=True)
    name = String(required=True)
    display_name = String()
    description = String()
    endpoint = String()
    transport = CapabilityMcpTransport(required=True)
    auth_type = String()
    protocol_version = String()
    status = String()
    register_status = String()
    tool_count = Int()
    config = Field(JSONCamelCase)
    created_at = DateTime()
    updated_at = DateTime()
    updated_by = String()


class CapabilityMcpProviderConnection(ObjectType):
    """Paginated provider list in the Banyanos connection shape."""

    capability_mcp_provider_list = List(CapabilityMcpProvider)
    page_number = Int()
    pages = Int()
    total = Int()


class CapabilityMcpTool(ObjectType):
    """Tool compatibility view over MCPFunction."""

    id = ID(required=True)
    provider_id = ID()
    mcp_server_id = ID()
    name = String(required=True)
    display_name = String()
    description = String()
    input_schema = Field(JSONCamelCase)
    output_schema = Field(JSONCamelCase)
    status = String()
    is_deprecated = Boolean()
    config = Field(JSONCamelCase)
    created_at = DateTime()
    updated_at = DateTime()


class CapabilityMcpToolConnection(ObjectType):
    capability_mcp_tool_list = List(CapabilityMcpTool)
    page_number = Int()
    pages = Int()
    total = Int()


class CapabilityMcpSyncStats(ObjectType):
    """Registration/sync statistics in the shared payload."""

    tools = Int()
    resources = Int()
    prompts = Int()
    modules = Int()
    settings = Int()


class CapabilityMcpRegistrationPayload(ObjectType):
    """Shared payload for remote/custom/git registration mutations."""

    ok = Boolean(required=True)
    message = String()
    provider = Field(CapabilityMcpProvider)
    transport = CapabilityMcpTransport()
    stats = Field(CapabilityMcpSyncStats)
    resolved_commit = String()
    installed_package_version = String()
    action = String()


class CapabilityMcpGitVersionInfo(ObjectType):
    """Version-check payload for Git-installed custom MCP packages."""

    ok = Boolean(required=True)
    message = String()
    needs_refresh = Boolean()
    local_commit = String()
    remote_commit = String()
    installed_package_version = String()
    latest_remote_version = String()
    last_checked_at = DateTime()


class CapabilityMcpUnregistrationPayload(ObjectType):
    """Result payload for compatibility deregister mutations.

    Reports the deletion outcome: how many function rows were removed,
    whether the shared setting row was deleted or preserved (because
    another module still references it). Never touches disk or S3.
    """

    ok = Boolean(required=True)
    message = String()
    module_name = String()
    transport = String()
    source = String()
    deleted_functions = Int()
    deleted_setting = Boolean()
    setting_kept = Boolean()


class CapabilityMcpInvocation(ObjectType):
    """Invocation compatibility view over MCPFunctionCall + execution result."""

    id = ID(required=True)
    invocation_id = ID()
    tool_name = String()
    mcp_type = String()
    status = String()
    request_payload = Field(JSONCamelCase)
    response_payload = Field(JSONCamelCase)
    trace_id = String()
    agent_id = String()
    duration_ms = Int()
    error_message = String()
    created_at = DateTime()
    updated_at = DateTime()


class CapabilityMcpInvocationConnection(ObjectType):
    capability_mcp_invocation_list = List(CapabilityMcpInvocation)
    page_number = Int()
    pages = Int()
    total = Int()