#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

import time
from typing import Any, Dict

from graphene import DateTime, Field, Int, ObjectType, ResolveInfo, String

from ..mutations.mcp_configuration import LoadMcpConfiguration
from ..mutations.mcp_function import DeleteMcpFunction, InsertUpdateMcpFunction
from ..mutations.mcp_function_call import (
    DeleteMcpFunctionCall,
    InsertUpdateMcpFunctionCall,
)
from ..mutations.mcp_module import DeleteMcpModule, InsertUpdateMcpModule
from ..mutations.mcp_setting import DeleteMcpSetting, InsertUpdateMcpSetting
from ..queries.mcp_function import resolve_mcp_function, resolve_mcp_function_list
from ..queries.mcp_function_call import (
    resolve_mcp_function_call,
    resolve_mcp_function_call_list,
)
from ..queries.mcp_module import resolve_mcp_module, resolve_mcp_module_list
from ..queries.mcp_setting import resolve_mcp_setting, resolve_mcp_setting_list
from ..types.mcp_function import MCPFunctionListType, MCPFunctionType
from ..types.mcp_function_call import MCPFunctionCallListType, MCPFunctionCallType
from ..types.mcp_module import MCPModuleListType, MCPModuleType
from ..types.mcp_setting import MCPSettingListType, MCPSettingType


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


class Mutations(ObjectType):
    load_mcp_configuration = LoadMcpConfiguration.Field()
    insert_update_mcp_function = InsertUpdateMcpFunction.Field()
    delete_mcp_function = DeleteMcpFunction.Field()
    insert_update_mcp_function_call = InsertUpdateMcpFunctionCall.Field()
    delete_mcp_function_call = DeleteMcpFunctionCall.Field()
    insert_update_mcp_module = InsertUpdateMcpModule.Field()
    delete_mcp_module = DeleteMcpModule.Field()
    insert_update_mcp_setting = InsertUpdateMcpSetting.Field()
    delete_mcp_setting = DeleteMcpSetting.Field()
