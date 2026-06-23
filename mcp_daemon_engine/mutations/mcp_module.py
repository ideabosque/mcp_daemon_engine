# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

import traceback
from typing import Any, Dict

from graphene import Boolean, Field, Mutation, String, List
from silvaengine_utility import JSONCamelCase

from ..models.repositories import get_repo
from ..types.mcp_module import MCPModuleType


class InsertUpdateMcpModule(Mutation):
    mcp_module = Field(MCPModuleType)

    class Arguments:
        module_name = String(required=True)
        package_name = String(required=True)
        classes = List(JSONCamelCase, required=False)
        source = String(required=False)
        updated_by = String(required=True)

    @staticmethod
    def mutate(
        root: Any, info: Any, **kwargs: Dict[str, Any]
    ) -> "InsertUpdateMcpModule":
        try:
            mcp_module = get_repo("mcp_module").insert_update(info, **kwargs)
        except Exception as e:
            log = traceback.format_exc()
            info.context.get("logger").error(log)
            raise e

        return InsertUpdateMcpModule(mcp_module=mcp_module)


class DeleteMcpModule(Mutation):
    ok = Boolean()

    class Arguments:
        module_name = String(required=True)

    @staticmethod
    def mutate(root: Any, info: Any, **kwargs: Dict[str, Any]) -> "DeleteMcpModule":
        try:
            ok = get_repo("mcp_module").delete(info, **kwargs)
        except Exception as e:
            log = traceback.format_exc()
            info.context.get("logger").error(log)
            raise e

        return DeleteMcpModule(ok=ok)