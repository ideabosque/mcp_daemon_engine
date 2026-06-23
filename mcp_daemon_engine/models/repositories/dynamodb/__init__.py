# -*- coding: utf-8 -*-
"""DynamoDB repositories — thin wrappers over existing PynamoDB model functions.

Each entity has its own repo file. The register_all function instantiates
all 4 repositories and registers them with the dispatch registry.
"""
from __future__ import print_function

__author__ = "bibow"

from typing import Dict

from ..base import EntityRepository


def register_all(registry: Dict[str, EntityRepository]) -> None:
    """Register all DynamoDB repositories into the given registry dict."""
    from .mcp_function_repo import MCPFunctionRepository
    from .mcp_module_repo import MCPModuleRepository
    from .mcp_setting_repo import MCPSettingRepository
    from .mcp_function_call_repo import MCPFunctionCallRepository

    repos = [
        MCPFunctionRepository(),
        MCPModuleRepository(),
        MCPSettingRepository(),
        MCPFunctionCallRepository(),
    ]
    for repo in repos:
        registry[repo.entity_type] = repo


__all__ = ["register_all"]