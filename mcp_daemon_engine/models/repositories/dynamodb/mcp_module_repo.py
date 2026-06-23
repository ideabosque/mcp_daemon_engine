# -*- coding: utf-8 -*-
"""DynamoDB repository for MCPModule entity."""
from __future__ import print_function

__author__ = "bibow"

from typing import Any, Dict, Optional

from ..base import EntityRepository
from ._base import _normalize

from ...dynamodb import mcp_module as _mod_mod


class MCPModuleRepository(EntityRepository):
    """DynamoDB repository for MCPModule entity."""

    @property
    def entity_type(self) -> str:
        return "mcp_module"

    def get(self, **keys: Any) -> Optional[Dict[str, Any]]:
        partition_key = keys.get("partition_key")
        module_name = keys.get("module_name")
        if not partition_key or not module_name:
            return None
        count = _mod_mod.get_mcp_module_count(partition_key, module_name)
        if count == 0:
            return None
        return _normalize(_mod_mod.get_mcp_module(partition_key, module_name))

    def count(self, **keys: Any) -> int:
        partition_key = keys.get("partition_key")
        module_name = keys.get("module_name")
        if not partition_key or not module_name:
            return 0
        return _mod_mod.get_mcp_module_count(partition_key, module_name)

    def list(self, info: Any, **filters: Any) -> Any:
        return _mod_mod.resolve_mcp_module_list(info, **filters)

    def insert_update(self, info: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return _mod_mod.insert_update_mcp_module(info, **kwargs)

    def delete(self, info: Any, **kwargs: Any) -> bool:
        return _mod_mod.delete_mcp_module(info, **kwargs)

    def get_type(self, info: Any, instance: Any) -> Any:
        return _mod_mod.get_mcp_module_type(info, instance)

    def resolve_single(self, info: Any, **kwargs: Any) -> Any:
        return _mod_mod.resolve_mcp_module(info, **kwargs)