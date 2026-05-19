# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

import traceback
from typing import Any, Dict

from graphene import Boolean, Field, Mutation, String
from silvaengine_utility import JSONCamelCase

from ..handlers.mcp_handlers import load_mcp_configuration_into_models


class LoadMcpConfiguration(Mutation):
    ok = Boolean()
    message = String()

    class Arguments:
        package_name = String(required=False)
        module_name = String(required=False)
        source = String(required=False)
        mcp_configuration = JSONCamelCase(required=False)
        variables = JSONCamelCase(required=False)
        updated_by = String(required=True)

    @staticmethod
    def mutate(
        root: Any, info: Any, **kwargs: Dict[str, Any]
    ) -> "LoadMcpConfiguration":
        try:
            stats = load_mcp_configuration_into_models(info, **kwargs)

            message = (
                f"Successfully loaded MCP configuration: "
                f"{stats['tools']} tools, {stats['resources']} resources, "
                f"{stats['prompts']} prompts, {stats['modules']} modules, "
                f"{stats['settings']} settings"
            )

            return LoadMcpConfiguration(ok=True, message=message)

        except Exception as e:
            log = traceback.format_exc()
            info.context.get("logger").error(log)
            return LoadMcpConfiguration(
                ok=False, message=f"Failed to load MCP configuration: {str(e)}"
            )
