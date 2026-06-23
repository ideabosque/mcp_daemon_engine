# -*- coding: utf-8 -*-
"""DynamoDB repository for MCPSetting entity."""
from __future__ import print_function

__author__ = "bibow"

from typing import Any, Dict, Optional

from ..base import EntityRepository
from ._base import _normalize

from ...dynamodb import mcp_setting as _set_mod


class MCPSettingRepository(EntityRepository):
    """DynamoDB repository for MCPSetting entity."""

    @property
    def entity_type(self) -> str:
        return "mcp_setting"

    def get(self, **keys: Any) -> Optional[Dict[str, Any]]:
        partition_key = keys.get("partition_key")
        setting_id = keys.get("setting_id")
        if not partition_key or not setting_id:
            return None
        count = _set_mod.get_mcp_setting_count(partition_key, setting_id)
        if count == 0:
            return None
        return _normalize(_set_mod.get_mcp_setting(partition_key, setting_id))

    def count(self, **keys: Any) -> int:
        partition_key = keys.get("partition_key")
        setting_id = keys.get("setting_id")
        if not partition_key or not setting_id:
            return 0
        return _set_mod.get_mcp_setting_count(partition_key, setting_id)

    def list(self, info: Any, **filters: Any) -> Any:
        return _set_mod.resolve_mcp_setting_list(info, **filters)

    def insert_update(self, info: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        return _set_mod.insert_update_mcp_setting(info, **kwargs)

    def delete(self, info: Any, **kwargs: Any) -> bool:
        return _set_mod.delete_mcp_setting(info, **kwargs)

    def get_type(self, info: Any, instance: Any) -> Any:
        return _set_mod.get_mcp_setting_type(info, instance)

    def resolve_single(self, info: Any, **kwargs: Any) -> Any:
        return _set_mod.resolve_mcp_setting(info, **kwargs)