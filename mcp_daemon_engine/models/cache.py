# -*- coding: utf-8 -*-
from __future__ import annotations

__author__ = "bibow"

import logging
from functools import lru_cache
from typing import Any, Dict, Optional, Set

from silvaengine_dynamodb_base.cache_utils import (
    CacheConfigResolvers,
    CascadingCachePurger,
)


def _extract_module_setting_ids(raw_classes: Any) -> Set[str]:
    setting_ids: Set[str] = set()
    if not raw_classes:
        return setting_ids

    for class_item in raw_classes:
        if class_item is None:
            continue

        payload = class_item
        if hasattr(class_item, "as_dict"):
            try:
                payload = class_item.as_dict()
            except Exception:
                payload = None
        elif hasattr(class_item, "attribute_values"):
            payload = getattr(class_item, "attribute_values", None)

        if payload is None:
            continue

        if not isinstance(payload, dict):
            try:
                payload = dict(payload)
            except Exception:
                continue

        setting_id = payload.get("setting_id")
        if isinstance(setting_id, str) and setting_id:
            setting_ids.add(setting_id)

    return setting_ids


@lru_cache(maxsize=1)
def _get_cascading_cache_purger() -> CascadingCachePurger:
    from ..handlers.config import Config

    return CascadingCachePurger(
        CacheConfigResolvers(
            get_cache_entity_config=Config.get_cache_entity_config,
            get_cache_relationships=Config.get_cache_relationships,
            queries_module_base="mcp_daemon_engine.queries",
        )
    )


def purge_entity_cascading_cache(
    logger: logging.Logger,
    entity_type: str,
    context_keys: Optional[Dict[str, Any]] = None,
    entity_keys: Optional[Dict[str, Any]] = None,
    cascade_depth: int = 3,
) -> Dict[str, Any]:
    """Universal function to purge entity cache with cascading child cache support."""
    purger = _get_cascading_cache_purger()
    return purger.purge_entity_cascading_cache(
        logger,
        entity_type,
        context_keys=context_keys,
        entity_keys=entity_keys,
        cascade_depth=cascade_depth,
    )


__all__ = [
    "purge_entity_cascading_cache",
    "_extract_module_setting_ids",
]
