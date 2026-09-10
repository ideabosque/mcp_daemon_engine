#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

import time
from typing import Any, Dict

from graphene import Boolean, DateTime, Field, ID, Int, ObjectType, ResolveInfo, String

from .mutations.capability_mcp import (
    CheckCapabilityCustomMcpGitPackageVersion,
    GenerateCapabilityCustomMcpUploadUrl,
    InvokeCapabilityMcpTool,
    RegisterCapabilityCustomMcpGitPackage,
    RegisterCapabilityCustomMcpPackage,
    RegisterCapabilityCustomMcpPackageBase64,
    RegisterCapabilityRemoteMcp,
    RefreshCapabilityCustomMcpGitPackage,
    TestCapabilityMcpTool,
)
from .mutations.mcp_configuration import LoadMcpConfiguration
from .mutations.mcp_external import SyncExternalMcpServer
from .mutations.mcp_function import DeleteMcpFunction, InsertUpdateMcpFunction
from .mutations.mcp_function_call import (
    DeleteMcpFunctionCall,
    InsertUpdateMcpFunctionCall,
)
from .mutations.mcp_git import (
    CheckMcpGitPackageVersion,
    InstallMcpPackageFromGit,
    RefreshMcpGitPackage,
)
from .mutations.mcp_module import DeleteMcpModule, InsertUpdateMcpModule
from .mutations.mcp_setting import DeleteMcpSetting, InsertUpdateMcpSetting
from .mutations.mcp_upload import (
    GenerateMcpPackageUploadUrl,
    ProcessMcpPackage,
)
from .queries.capability_mcp import (
    resolve_capability_mcp_invocation_list,
    resolve_capability_mcp_provider,
    resolve_capability_mcp_provider_list,
    resolve_capability_mcp_server,
    resolve_capability_mcp_server_tools,
    resolve_capability_mcp_tool,
    resolve_capability_mcp_tool_list,
)
from .queries.mcp_function import resolve_mcp_function, resolve_mcp_function_list
from .queries.mcp_function_call import (
    resolve_mcp_function_call,
    resolve_mcp_function_call_list,
)
from .queries.mcp_module import resolve_mcp_module, resolve_mcp_module_list
from .queries.mcp_setting import resolve_mcp_setting, resolve_mcp_setting_list
from .types.capability_mcp import (
    CapabilityMcpInvocation,
    CapabilityMcpInvocationConnection,
    CapabilityMcpProvider,
    CapabilityMcpProviderConnection,
    CapabilityMcpTool,
    CapabilityMcpToolConnection,
    CapabilityMcpTransport,
)
from .types.mcp_function import MCPFunctionListType, MCPFunctionType
from .types.mcp_function_call import MCPFunctionCallListType, MCPFunctionCallType
from .types.mcp_module import MCPModuleListType, MCPModuleType
from .types.mcp_configuration_stats import McpConfigurationStats
from .types.mcp_setting import MCPSettingListType, MCPSettingType


def type_class():
    return [
        MCPFunctionType,
        MCPFunctionListType,
        MCPFunctionCallType,
        MCPFunctionCallListType,
        MCPModuleType,
        MCPModuleListType,
        MCPSettingType,
        MCPSettingListType,
        McpConfigurationStats,
        CapabilityMcpProvider,
        CapabilityMcpProviderConnection,
        CapabilityMcpTool,
        CapabilityMcpToolConnection,
        CapabilityMcpTransport,
        CapabilityMcpInvocation,
        CapabilityMcpInvocationConnection,
    ]


class Query(ObjectType):
    ping = String()

    mcp_function = Field(
        MCPFunctionType,
        name=String(required=True),
    )

    mcp_function_list = Field(
        MCPFunctionListType,
        page_number=Int(required=False),
        limit=Int(required=False),
        mcp_type=String(required=False),
        desc=String(name="description", required=False),
        module_name=String(required=False),
        class_name=String(required=False),
        function_name=String(required=False),
        status=Int(required=False),
    )

    mcp_function_call = Field(
        MCPFunctionCallType,
        mcp_function_call_uuid=String(required=True),
    )

    mcp_function_call_list = Field(
        MCPFunctionCallListType,
        page_number=Int(required=False),
        limit=Int(required=False),
        mcp_type=String(required=False),
        name=String(required=False),
        status=String(required=False),
        updated_at_gt=DateTime(required=False),
        updated_at_lt=DateTime(required=False),
    )

    mcp_module = Field(
        MCPModuleType,
        module_name=String(required=True),
    )

    mcp_module_list = Field(
        MCPModuleListType,
        page_number=Int(required=False),
        limit=Int(required=False),
        mcp_type=String(required=False),
        desc=String(name="description", required=False),
        module_name=String(required=False),
        class_name=String(required=False),
        function_name=String(required=False),
    )

    mcp_setting = Field(
        MCPSettingType,
        setting_id=String(required=True),
    )

    mcp_setting_list = Field(
        MCPSettingListType,
        page_number=Int(required=False),
        limit=Int(required=False),
        setting_id=String(required=False),
    )

    def resolve_ping(self, info: ResolveInfo) -> str:
        return f"Hello at {time.strftime('%X')}!!"

    def resolve_mcp_function(
        self, info: ResolveInfo, **kwargs: Dict[str, Any]
    ) -> MCPFunctionType | None:
        return resolve_mcp_function(info, **kwargs)

    def resolve_mcp_function_list(
        self, info: ResolveInfo, **kwargs: Dict[str, Any]
    ) -> MCPFunctionListType:
        return resolve_mcp_function_list(info, **kwargs)

    def resolve_mcp_function_call(
        self, info: ResolveInfo, **kwargs: Dict[str, Any]
    ) -> MCPFunctionCallType | None:
        return resolve_mcp_function_call(info, **kwargs)

    def resolve_mcp_function_call_list(
        self, info: ResolveInfo, **kwargs: Dict[str, Any]
    ) -> MCPFunctionCallListType:
        return resolve_mcp_function_call_list(info, **kwargs)

    def resolve_mcp_module(
        self, info: ResolveInfo, **kwargs: Dict[str, Any]
    ) -> MCPModuleType | None:
        return resolve_mcp_module(info, **kwargs)

    def resolve_mcp_module_list(
        self, info: ResolveInfo, **kwargs: Dict[str, Any]
    ) -> MCPModuleListType:
        return resolve_mcp_module_list(info, **kwargs)

    def resolve_mcp_setting(
        self, info: ResolveInfo, **kwargs: Dict[str, Any]
    ) -> MCPSettingType | None:
        return resolve_mcp_setting(info, **kwargs)

    def resolve_mcp_setting_list(
        self, info: ResolveInfo, **kwargs: Dict[str, Any]
    ) -> MCPSettingListType:
        return resolve_mcp_setting_list(info, **kwargs)

    # ------------------------------------------------------------------
    # Capability Engine MCP compatibility queries
    # ------------------------------------------------------------------
    capability_mcp_provider = Field(
        CapabilityMcpProvider,
        id=ID(required=True),
    )

    capability_mcp_provider_list = Field(
        CapabilityMcpProviderConnection,
        page_number=Int(required=False),
        limit=Int(required=False),
        transport=CapabilityMcpTransport(required=False),
        status=String(required=False),
        keyword=String(required=False),
    )

    capability_mcp_server = Field(
        CapabilityMcpProvider,
        id=ID(required=True),
    )

    capability_mcp_server_tools = Field(
        CapabilityMcpToolConnection,
        id=ID(required=True),
        page_number=Int(required=False),
        limit=Int(required=False),
    )

    capability_mcp_tool = Field(
        CapabilityMcpTool,
        id=ID(required=True),
    )

    capability_mcp_tool_list = Field(
        CapabilityMcpToolConnection,
        page_number=Int(required=False),
        limit=Int(required=False),
        provider_id=ID(required=False),
        status=String(required=False),
        keyword=String(required=False),
    )

    capability_mcp_invocation_list = Field(
        CapabilityMcpInvocationConnection,
        page_number=Int(required=False),
        limit=Int(required=False),
        tool_name=String(required=False),
        status=String(required=False),
    )

    def resolve_capability_mcp_provider(
        self, info: ResolveInfo, **kwargs: Dict[str, Any]
    ):
        return resolve_capability_mcp_provider(info, **kwargs)

    def resolve_capability_mcp_provider_list(
        self, info: ResolveInfo, **kwargs: Dict[str, Any]
    ):
        return resolve_capability_mcp_provider_list(info, **kwargs)

    def resolve_capability_mcp_server(
        self, info: ResolveInfo, **kwargs: Dict[str, Any]
    ):
        return resolve_capability_mcp_server(info, **kwargs)

    def resolve_capability_mcp_server_tools(
        self, info: ResolveInfo, **kwargs: Dict[str, Any]
    ):
        return resolve_capability_mcp_server_tools(info, **kwargs)

    def resolve_capability_mcp_tool(
        self, info: ResolveInfo, **kwargs: Dict[str, Any]
    ):
        return resolve_capability_mcp_tool(info, **kwargs)

    def resolve_capability_mcp_tool_list(
        self, info: ResolveInfo, **kwargs: Dict[str, Any]
    ):
        return resolve_capability_mcp_tool_list(info, **kwargs)

    def resolve_capability_mcp_invocation_list(
        self, info: ResolveInfo, **kwargs: Dict[str, Any]
    ):
        return resolve_capability_mcp_invocation_list(info, **kwargs)


class Mutations(ObjectType):
    load_mcp_configuration = LoadMcpConfiguration.Field()
    sync_external_mcp_server = SyncExternalMcpServer.Field()
    generate_mcp_package_upload_url = GenerateMcpPackageUploadUrl.Field()
    process_mcp_package = ProcessMcpPackage.Field()
    install_mcp_package_from_git = InstallMcpPackageFromGit.Field()
    check_mcp_git_package_version = CheckMcpGitPackageVersion.Field()
    refresh_mcp_git_package = RefreshMcpGitPackage.Field()
    # ------------------------------------------------------------------
    # Capability Engine MCP compatibility mutations
    # ------------------------------------------------------------------
    register_capability_remote_mcp = RegisterCapabilityRemoteMcp.Field()
    generate_capability_custom_mcp_upload_url = (
        GenerateCapabilityCustomMcpUploadUrl.Field()
    )
    register_capability_custom_mcp_package = (
        RegisterCapabilityCustomMcpPackage.Field()
    )
    register_capability_custom_mcp_package_base64 = (
        RegisterCapabilityCustomMcpPackageBase64.Field()
    )
    register_capability_custom_mcp_git_package = (
        RegisterCapabilityCustomMcpGitPackage.Field()
    )
    refresh_capability_custom_mcp_git_package = (
        RefreshCapabilityCustomMcpGitPackage.Field()
    )
    check_capability_custom_mcp_git_package_version = (
        CheckCapabilityCustomMcpGitPackageVersion.Field()
    )
    invoke_capability_mcp_tool = InvokeCapabilityMcpTool.Field()
    test_capability_mcp_tool = TestCapabilityMcpTool.Field()
    insert_update_mcp_function = InsertUpdateMcpFunction.Field()
    delete_mcp_function = DeleteMcpFunction.Field()
    insert_update_mcp_function_call = InsertUpdateMcpFunctionCall.Field()
    delete_mcp_function_call = DeleteMcpFunctionCall.Field()
    insert_update_mcp_module = InsertUpdateMcpModule.Field()
    delete_mcp_module = DeleteMcpModule.Field()
    insert_update_mcp_setting = InsertUpdateMcpSetting.Field()
    delete_mcp_setting = DeleteMcpSetting.Field()
