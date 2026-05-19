#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

from typing import Any, Dict

from graphene import ResolveInfo

from silvaengine_utility import method_cache

from ..handlers.config import Config

from ..models import mcp_function
from ..types.mcp_function import MCPFunctionListType, MCPFunctionType


def resolve_mcp_function(
    info: ResolveInfo, **kwargs: Dict[str, Any]
) -> MCPFunctionType | None:
    return mcp_function.resolve_mcp_function(info, **kwargs)


@method_cache(
    ttl=Config.get_cache_ttl(),
    cache_name=Config.get_cache_name("queries", "mcp_function"),
    cache_enabled=Config.is_cache_enabled,
)
def resolve_mcp_function_list(
    info: ResolveInfo, **kwargs: Dict[str, Any]
) -> MCPFunctionListType:
    return mcp_function.resolve_mcp_function_list(info, **kwargs)
