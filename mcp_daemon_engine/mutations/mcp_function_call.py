# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

import traceback
from typing import Any, Dict

from graphene import Boolean, Field, Int, List, Mutation, String
from silvaengine_utility import JSONCamelCase

from ..models.mcp_function_call import (
    delete_mcp_function_call,
    insert_update_mcp_function_call,
)
from ..types.mcp_function_call import MCPFunctionCallType


class InsertUpdateMcpFunctionCall(Mutation):
    mcp_function_call = Field(MCPFunctionCallType)

    class Arguments:
        mcp_function_call_uuid = String(required=False)
        name = String(required=False)
        mcp_type = String(required=False)
        arguments = JSONCamelCase(required=False)
        content_in_s3 = Boolean(required=False)
        content = String(required=False)
        status = String(required=False)
        notes = String(required=False)
        time_spent = Int(required=False)
        updated_by = String(required=True)

    @staticmethod
    def mutate(
        root: Any, info: Any, **kwargs: Dict[str, Any]
    ) -> "InsertUpdateMcpFunctionCall":
        try:
            mcp_function_call = insert_update_mcp_function_call(info, **kwargs)
        except Exception as e:
            log = traceback.format_exc()
            info.context.get("logger").error(log)
            raise e

        return InsertUpdateMcpFunctionCall(mcp_function_call=mcp_function_call)


class DeleteMcpFunctionCall(Mutation):
    ok = Boolean()

    class Arguments:
        # Based on model attributes from comment
        mcp_function_call_uuid = String(required=True)

    @staticmethod
    def mutate(
        root: Any, info: Any, **kwargs: Dict[str, Any]
    ) -> "DeleteMcpFunctionCall":
        try:
            ok = delete_mcp_function_call(info, **kwargs)
        except Exception as e:
            log = traceback.format_exc()
            info.context.get("logger").error(log)
            raise e

        return DeleteMcpFunctionCall(ok=ok)
