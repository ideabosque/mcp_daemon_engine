# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

import logging
from typing import  List

def initialize_tables(logger: logging.Logger) -> None:
    from .mcp_function import MCPFunctionModel
    from .mcp_module import MCPModuleModel
    from .mcp_setting import MCPSettingModel
    from .mcp_function_call import MCPFunctionCallModel

    models: List = [
        MCPFunctionModel,
        MCPModuleModel,
        MCPSettingModel,
        MCPFunctionCallModel,
    ]

    for model in models:
        if model.exists():
            continue

        table_name = model.Meta.table_name
        # Create with on-demand billing (PAY_PER_REQUEST)
        model.create_table(billing_mode="PAY_PER_REQUEST", wait=True)
        logger.info(f"The {table_name} table has been created.")

