# -*- coding: utf-8 -*-
"""DynamoDB repository for MCPFunctionCall entity."""
from __future__ import print_function

__author__ = "bibow"

from typing import Any, Dict, Optional

from ..base import EntityRepository
from ._base import _normalize

from ...dynamodb import mcp_function_call as _call_mod


class MCPFunctionCallRepository(EntityRepository):
    """DynamoDB repository for MCPFunctionCall entity."""

    @property
    def entity_type(self) -> str:
        return "mcp_function_call"

    def get(self, **keys: Any) -> Optional[Dict[str, Any]]:
        partition_key = keys.get("partition_key")
        mcp_function_call_uuid = keys.get("mcp_function_call_uuid")
        if not partition_key or not mcp_function_call_uuid:
            return None
        count = _call_mod.get_mcp_function_call_count(
            partition_key, mcp_function_call_uuid
        )
        if count == 0:
            return None
        return _normalize(
            _call_mod.get_mcp_function_call(partition_key, mcp_function_call_uuid)
        )

    def count(self, **keys: Any) -> int:
        partition_key = keys.get("partition_key")
        mcp_function_call_uuid = keys.get("mcp_function_call_uuid")
        if not partition_key or not mcp_function_call_uuid:
            return 0
        return _call_mod.get_mcp_function_call_count(
            partition_key, mcp_function_call_uuid
        )

    def list(self, info: Any, **filters: Any) -> Any:
        return _call_mod.resolve_mcp_function_call_list(info, **filters)

    def insert_update(self, info: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return _call_mod.insert_update_mcp_function_call(info, **kwargs)

    def delete(self, info: Any, **kwargs: Any) -> bool:
        return _call_mod.delete_mcp_function_call(info, **kwargs)

    def get_type(self, info: Any, instance: Any) -> Any:
        return _call_mod.get_mcp_function_call_type(info, instance)

    def resolve_single(self, info: Any, **kwargs: Any) -> Any:
        return _call_mod.resolve_mcp_function_call(info, **kwargs)