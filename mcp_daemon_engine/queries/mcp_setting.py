#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

from typing import Any, Dict

from graphene import ResolveInfo

from silvaengine_utility import method_cache

from ..handlers.config import Config
from ..models.repositories import get_repo
from ..types.mcp_setting import MCPSettingListType, MCPSettingType


def resolve_mcp_setting(
    info: ResolveInfo, **kwargs: Dict[str, Any]
) -> MCPSettingType | None:
    return get_repo("mcp_setting").resolve_single(info, **kwargs)


@method_cache(
    ttl=Config.get_cache_ttl(),
    cache_name=Config.get_cache_name("queries", "mcp_setting"),
    cache_enabled=Config.is_cache_enabled,
)
def resolve_mcp_setting_list(
    info: ResolveInfo, **kwargs: Dict[str, Any]
) -> MCPSettingListType:
    return get_repo("mcp_setting").list(info, **kwargs)