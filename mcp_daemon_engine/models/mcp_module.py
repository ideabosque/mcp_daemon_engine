#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

import functools
import traceback
from typing import Any, Dict

import pendulum
from graphene import ResolveInfo
from pynamodb.attributes import (
    ListAttribute,
    MapAttribute,
    UnicodeAttribute,
    UTCDateTimeAttribute,
)
from pynamodb.indexes import AllProjection, LocalSecondaryIndex
from silvaengine_dynamodb_base import (
    BaseModel,
    delete_decorator,
    insert_update_decorator,
    monitor_decorator,
    resolve_list_decorator,
)
from silvaengine_utility import method_cache
from silvaengine_utility.serializer import Serializer
from tenacity import retry, stop_after_attempt, wait_exponential

from ..handlers.config import Config
from ..types.mcp_module import MCPModuleListType, MCPModuleType


class MCPPackgeIndex(LocalSecondaryIndex):
    """
    This class represents a local secondary index
    """

    class Meta:
        billing_mode = "PAY_PER_REQUEST"
        # All attributes are projected
        projection = AllProjection()
        index_name = "package_name-index"

    partition_key = UnicodeAttribute(hash_key=True)
    package_name = UnicodeAttribute(range_key=True)


class MCPModuleModel(BaseModel):
    class Meta(BaseModel.Meta):
        table_name = "mcp-modules"

    partition_key = UnicodeAttribute(hash_key=True)
    module_name = UnicodeAttribute(range_key=True)
    package_name = UnicodeAttribute()
    classes = ListAttribute(of=MapAttribute)
    source = UnicodeAttribute(null=True)
    updated_by = UnicodeAttribute()
    created_at = UTCDateTimeAttribute()
    updated_at = UTCDateTimeAttribute()
    mcp_package_index = MCPPackgeIndex()


def purge_cache():
    def actual_decorator(original_function):
        @functools.wraps(original_function)
        def wrapper_function(*args, **kwargs):
            try:
                # Execute original function first
                result = original_function(*args, **kwargs)

                # Then purge cache after successful operation
                from ..models.cache import (
                    _extract_module_setting_ids,
                    purge_entity_cascading_cache,
                )

                # Get entity keys from kwargs or entity parameter
                entity_keys = {}
                partition_key = args[0].context.get("partition_key") or kwargs.get(
                    "partition_key"
                )

                # Try to get from entity parameter first (for updates)
                entity = kwargs.get("entity")
                if entity:
                    entity_keys["module_name"] = getattr(entity, "module_name", None)

                # Fallback to kwargs (for creates/deletes)
                if not entity_keys.get("module_name"):
                    entity_keys["module_name"] = kwargs.get("module_name")

                # Only purge if we have the required keys
                if entity_keys.get("module_name") and partition_key:
                    purge_entity_cascading_cache(
                        args[0].context.get("logger"),
                        entity_type="mcp_module",
                        context_keys={"partition_key": partition_key},
                        entity_keys=entity_keys,
                        cascade_depth=3,
                    )

                    # Purge setting caches if module has classes with setting_ids
                    try:
                        # Extract setting_ids from entity or kwargs
                        classes = None
                        if entity:
                            classes = getattr(entity, "classes", None)
                        if not classes and "classes" in kwargs:
                            classes = kwargs.get("classes")

                        if classes:
                            setting_ids = _extract_module_setting_ids(classes)
                            for setting_id in setting_ids:
                                purge_entity_cascading_cache(
                                    args[0].context.get("logger"),
                                    entity_type="mcp_setting",
                                    context_keys={"partition_key": partition_key},
                                    entity_keys={"setting_id": setting_id},
                                    cascade_depth=3,
                                )
                    except Exception:
                        pass

                return result
            except Exception as e:
                log = traceback.format_exc()
                args[0].context.get("logger").error(log)
                raise e

        return wrapper_function

    return actual_decorator


@retry(
    reraise=True,
    wait=wait_exponential(multiplier=1, max=60),
    stop=stop_after_attempt(5),
)
@method_cache(
    ttl=Config.get_cache_ttl(),
    cache_name=Config.get_cache_name("models", "mcp_module"),
    cache_enabled=Config.is_cache_enabled,
)
def get_mcp_module(partition_key: str, module_name: str) -> MCPModuleModel:
    return MCPModuleModel.get(partition_key, module_name)


def get_mcp_module_count(partition_key: str, module_name: str) -> int:
    return MCPModuleModel.count(
        partition_key, MCPModuleModel.module_name == module_name
    )


def get_mcp_module_type(info: ResolveInfo, mcp_module: MCPModuleModel) -> MCPModuleType:
    try:
        mcp_module = mcp_module.__dict__["attribute_values"]
    except Exception as e:
        log = traceback.format_exc()
        info.context.get("logger").exception(log)
        raise e
    return MCPModuleType(**Serializer.json_normalize(mcp_module))


def resolve_mcp_module(
    info: ResolveInfo, **kwargs: Dict[str, Any]
) -> MCPModuleType | None:
    count = get_mcp_module_count(info.context["partition_key"], kwargs["module_name"])
    if count == 0:
        return None

    return get_mcp_module_type(
        info, get_mcp_module(info.context["partition_key"], kwargs["module_name"])
    )


@monitor_decorator
@resolve_list_decorator(
    attributes_to_get=["partition_key", "module_name", "package_name"],
    list_type_class=MCPModuleListType,
    type_funct=get_mcp_module_type,
)
def resolve_mcp_module_list(info: ResolveInfo, **kwargs: Dict[str, Any]) -> Any:
    partition_key = info.context["partition_key"]
    package_name = kwargs.get("package_name")
    module_name = kwargs.get("module_name")
    the_filters = None
    args = []
    inquiry_funct = MCPModuleModel.scan
    count_funct = MCPModuleModel.count

    if partition_key:
        args = [partition_key, None]
        inquiry_funct = MCPModuleModel.query

        if package_name:
            inquiry_funct = MCPModuleModel.mcp_package_index.query
            args[1] = MCPModuleModel.package_name == package_name
            count_funct = MCPModuleModel.mcp_package_index.count

    if module_name:
        the_filters &= MCPModuleModel.module_name.contains(module_name)

    if the_filters is not None:
        args.append(the_filters)

    return inquiry_funct, count_funct, args


@insert_update_decorator(
    keys={
        "hash_key": "partition_key",
        "range_key": "module_name",
    },
    range_key_required=True,
    model_funct=get_mcp_module,
    count_funct=get_mcp_module_count,
    type_funct=get_mcp_module_type,
)
@purge_cache()
def insert_update_mcp_module(info: ResolveInfo, **kwargs: Dict[str, Any]) -> None:
    partition_key = kwargs.get("partition_key")
    module_name = kwargs.get("module_name")

    if kwargs.get("entity") is None:
        cols = {
            "package_name": kwargs["package_name"],
            "classes": kwargs.get("classes", []),
            "updated_by": kwargs["updated_by"],
            "created_at": pendulum.now("UTC"),
            "updated_at": pendulum.now("UTC"),
        }
        for key in [
            "source",
        ]:
            if key in kwargs:
                cols[key] = kwargs[key]

        MCPModuleModel(
            partition_key,
            module_name,
            **cols,
        ).save()
        return

    mcp_module = kwargs.get("entity")
    actions = [
        MCPModuleModel.updated_by.set(kwargs["updated_by"]),
        MCPModuleModel.updated_at.set(pendulum.now("UTC")),
    ]

    field_map = {
        "package_name": MCPModuleModel.package_name,
        "classes": MCPModuleModel.classes,
        "source": MCPModuleModel.source,
    }

    for key, field in field_map.items():
        if key in kwargs:
            actions.append(field.set(kwargs[key]))

    mcp_module.update(actions=actions)
    return


@delete_decorator(
    keys={
        "hash_key": "partition_key",
        "range_key": "module_name",
    },
    model_funct=get_mcp_module,
)
@purge_cache()
def delete_mcp_module(info: ResolveInfo, **kwargs: Dict[str, Any]) -> bool:
    kwargs["entity"].delete()
    return True
