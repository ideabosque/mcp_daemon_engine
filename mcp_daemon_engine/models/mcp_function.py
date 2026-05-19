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
    BooleanAttribute,
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
from ..types.mcp_function import MCPFunctionListType, MCPFunctionType


class MCPTypeIndex(LocalSecondaryIndex):
    """
    This class represents a local secondary index
    """

    class Meta:
        billing_mode = "PAY_PER_REQUEST"
        # All attributes are projected
        projection = AllProjection()
        index_name = "mcp_type-index"

    partition_key = UnicodeAttribute(hash_key=True)
    mcp_type = UnicodeAttribute(range_key=True)


class MCPFunctionModel(BaseModel):
    class Meta(BaseModel.Meta):
        table_name = "mcp-functions"

    partition_key = UnicodeAttribute(hash_key=True)
    name = UnicodeAttribute(range_key=True)
    mcp_type = UnicodeAttribute()
    description = UnicodeAttribute(attr_name="description", null=True)
    data = MapAttribute()
    annotations = UnicodeAttribute(null=True)
    module_name = UnicodeAttribute(null=True)
    class_name = UnicodeAttribute(null=True)
    function_name = UnicodeAttribute(null=True)
    return_type = UnicodeAttribute(null=True)
    is_async = BooleanAttribute(null=True)
    updated_by = UnicodeAttribute()
    created_at = UTCDateTimeAttribute()
    updated_at = UTCDateTimeAttribute()
    mcp_type_index = MCPTypeIndex()


def purge_cache():
    def actual_decorator(original_function):
        @functools.wraps(original_function)
        def wrapper_function(*args, **kwargs):
            try:
                # Execute original function first
                result = original_function(*args, **kwargs)

                # Then purge cache after successful operation
                from ..models.cache import purge_entity_cascading_cache

                # Get entity keys from kwargs or entity parameter
                entity_keys = {}
                partition_key = args[0].context.get("partition_key") or kwargs.get(
                    "partition_key"
                )

                # Try to get from entity parameter first (for updates)
                entity = kwargs.get("entity")
                if entity:
                    entity_keys["name"] = getattr(entity, "name", None)

                # Fallback to kwargs (for creates/deletes)
                if not entity_keys.get("name"):
                    entity_keys["name"] = kwargs.get("name")

                # Only purge if we have the required keys
                if entity_keys.get("name") and partition_key:
                    purge_entity_cascading_cache(
                        args[0].context.get("logger"),
                        entity_type="mcp_function",
                        context_keys={"partition_key": partition_key},
                        entity_keys=entity_keys,
                        cascade_depth=3,
                    )

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
    cache_name=Config.get_cache_name("models", "mcp_function"),
    cache_enabled=Config.is_cache_enabled,
)
def get_mcp_function(partition_key: str, name: str) -> MCPFunctionModel:
    return MCPFunctionModel.get(partition_key, name)


def get_mcp_function_count(partition_key: str, name: str) -> int:
    return MCPFunctionModel.count(partition_key, MCPFunctionModel.name == name)


def get_mcp_function_type(
    info: ResolveInfo, mcp_function: MCPFunctionModel
) -> MCPFunctionType:
    try:
        return MCPFunctionType(
            **Serializer.json_normalize(
                mcp_function.__dict__["attribute_values"],
            )
        )
    except Exception as e:
        log = traceback.format_exc()
        info.context.get("logger").exception(log)
        raise e


def resolve_mcp_function(
    info: ResolveInfo, **kwargs: Dict[str, Any]
) -> MCPFunctionType | None:
    count = get_mcp_function_count(info.context["partition_key"], kwargs["name"])
    if count == 0:
        return None

    return get_mcp_function_type(
        info, get_mcp_function(info.context["partition_key"], kwargs["name"])
    )


@monitor_decorator
@resolve_list_decorator(
    attributes_to_get=["partition_key", "name", "type"],
    list_type_class=MCPFunctionListType,
    type_funct=get_mcp_function_type,
)
def resolve_mcp_function_list(info: ResolveInfo, **kwargs: Dict[str, Any]) -> Any:
    partition_key = info.context["partition_key"]
    mcp_type = kwargs.get("mcp_type")
    description = kwargs.get("desc")
    module_name = kwargs.get("module_name")
    class_name = kwargs.get("class_name")
    function_name = kwargs.get("function_name")
    args = []
    inquiry_funct = MCPFunctionModel.scan
    count_funct = MCPFunctionModel.count
    the_filters = None

    if partition_key:
        args = [partition_key, None]
        inquiry_funct = MCPFunctionModel.query

        if mcp_type:
            inquiry_funct = MCPFunctionModel.mcp_type_index.query
            args[1] = MCPFunctionModel.mcp_type == mcp_type
            count_funct = MCPFunctionModel.mcp_type_index.count

    if description:
        the_filters &= MCPFunctionModel.description.contains(description)
    if module_name:
        the_filters &= MCPFunctionModel.module_name == module_name
    if class_name:
        the_filters &= MCPFunctionModel.class_name == class_name
    if function_name:
        the_filters &= MCPFunctionModel.function_name == function_name
    if the_filters is not None:
        args.append(the_filters)

    return inquiry_funct, count_funct, args


@insert_update_decorator(
    keys={
        "hash_key": "partition_key",
        "range_key": "name",
    },
    range_key_required=True,
    model_funct=get_mcp_function,
    count_funct=get_mcp_function_count,
    type_funct=get_mcp_function_type,
)
@purge_cache()
def insert_update_mcp_function(info: ResolveInfo, **kwargs: Dict[str, Any]) -> None:
    partition_key = kwargs.get("partition_key")
    name = kwargs.get("name")

    if kwargs.get("entity") is None:
        cols = {
            "mcp_type": kwargs["mcp_type"],
            "data": kwargs.get("data", {}),
            "updated_by": kwargs["updated_by"],
            "created_at": pendulum.now("UTC"),
            "updated_at": pendulum.now("UTC"),
        }
        for key in [
            "description",
            "annotations",
            "module_name",
            "class_name",
            "function_name",
            "return_type",
            "is_async",
        ]:
            if key in kwargs:
                cols[key] = kwargs[key]

        MCPFunctionModel(
            partition_key,
            name,
            **cols,
        ).save()
        return

    mcp_function = kwargs.get("entity")
    actions = [
        MCPFunctionModel.updated_by.set(kwargs["updated_by"]),
        MCPFunctionModel.updated_at.set(pendulum.now("UTC")),
    ]

    field_map = {
        "mcp_type": MCPFunctionModel.mcp_type,
        "description": MCPFunctionModel.description,
        "data": MCPFunctionModel.data,
        "annotations": MCPFunctionModel.annotations,
        "module_name": MCPFunctionModel.module_name,
        "class_name": MCPFunctionModel.class_name,
        "function_name": MCPFunctionModel.function_name,
        "return_type": MCPFunctionModel.return_type,
        "is_async": MCPFunctionModel.is_async,
    }

    for key, field in field_map.items():
        if key in kwargs:
            actions.append(field.set(kwargs[key]))

    mcp_function.update(actions=actions)
    return


@delete_decorator(
    keys={
        "hash_key": "partition_key",
        "range_key": "name",
    },
    model_funct=get_mcp_function,
)
@purge_cache()
def delete_mcp_function(info: ResolveInfo, **kwargs: Dict[str, Any]) -> bool:
    kwargs["entity"].delete()
    return True
