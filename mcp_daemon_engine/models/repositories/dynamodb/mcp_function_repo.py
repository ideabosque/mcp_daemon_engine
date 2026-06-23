# -*- coding: utf-8 -*-
"""DynamoDB repository for MCPFunction entity."""
from __future__ import print_function

__author__ = "bibow"

from typing import Any, Dict, Optional

from ..base import EntityRepository
from ._base import _normalize

from ...dynamodb import mcp_function as _fn_mod


class MCPFunctionRepository(EntityRepository):
    """DynamoDB repository for MCPFunction entity."""

    @property
    def entity_type(self) -> str:
        return "mcp_function"

    def get(self, **keys: Any) -> Optional[Dict[str, Any]]:
        partition_key = keys.get("partition_key")
        name = keys.get("name")
        if not partition_key or not name:
            return None
        count = _fn_mod.get_mcp_function_count(partition_key, name)
        if count == 0:
            return None
        return _normalize(_fn_mod.get_mcp_function(partition_key, name))

    def count(self, **keys: Any) -> int:
        partition_key = keys.get("partition_key")
        name = keys.get("name")
        if not partition_key or not name:
            return 0
        return _fn_mod.get_mcp_function_count(partition_key, name)

    def list(self, info: Any, **filters: Any) -> Any:
        return _fn_mod.resolve_mcp_function_list(info, **filters)

    def insert_update(self, info: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return _fn_mod.insert_update_mcp_function(info, **kwargs)

    def delete(self, info: Any, **kwargs: Any) -> bool:
        return _fn_mod.delete_mcp_function(info, **kwargs)

    def get_type(self, info: Any, instance: Any) -> Any:
        return _fn_mod.get_mcp_function_type(info, instance)

    def resolve_single(self, info: Any, **kwargs: Any) -> Any:
        return _fn_mod.resolve_mcp_function(info, **kwargs)