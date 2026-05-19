# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

import traceback
from typing import Any, Dict

from graphene import Boolean, Field, Int, List, Mutation, String
from silvaengine_utility import JSONCamelCase

from ..models.mcp_function import delete_mcp_function, insert_update_mcp_function
from ..types.mcp_function import MCPFunctionType


class InsertUpdateMcpFunction(Mutation):
    mcp_function = Field(MCPFunctionType)

    class Arguments:
        name = String(required=True)
        mcp_type = String(required=True)
        description = String(required=False)
        data = JSONCamelCase(required=False)
        annotations = String(required=False)
        module_name = String(required=False)
        class_name = String(required=False)
        function_name = String(required=False)
        return_type = String(required=False)
        is_async = Boolean(required=False)
        updated_by = String(required=True)

    @staticmethod
    def mutate(
        root: Any, info: Any, **kwargs: Dict[str, Any]
    ) -> "InsertUpdateMcpFunction":
        try:
            mcp_function = insert_update_mcp_function(info, **kwargs)
        except Exception as e:
            log = traceback.format_exc()
            info.context.get("logger").error(log)
            raise e

        return InsertUpdateMcpFunction(mcp_function=mcp_function)


class DeleteMcpFunction(Mutation):
    ok = Boolean()

    class Arguments:
        # Based on model attributes from comment
        name = String(required=True)

    @staticmethod
    def mutate(root: Any, info: Any, **kwargs: Dict[str, Any]) -> "DeleteMcpFunction":
        try:
            ok = delete_mcp_function(info, **kwargs)
        except Exception as e:
            log = traceback.format_exc()
            info.context.get("logger").error(log)
            raise e

        return DeleteMcpFunction(ok=ok)
