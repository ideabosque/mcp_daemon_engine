#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

import functools
import traceback
import uuid
from typing import Any, Dict

import pendulum
from graphene import ResolveInfo
from pynamodb.attributes import (
    BooleanAttribute,
    MapAttribute,
    NumberAttribute,
    UnicodeAttribute,
    UTCDateTimeAttribute,
)
from pynamodb.indexes import AllProjection, LocalSecondaryIndex
from tenacity import retry, stop_after_attempt, wait_exponential

from silvaengine_dynamodb_base import (
    BaseModel,
    delete_decorator,
    insert_update_decorator,
    monitor_decorator,
    resolve_list_decorator,
)
from silvaengine_utility import method_cache
from silvaengine_utility.serializer import Serializer
from ..handlers.config import Config
from ..types.mcp_function_call import MCPFunctionCallListType, MCPFunctionCallType


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


class NameIndex(LocalSecondaryIndex):
    """
    This class represents a local secondary index
    """

    class Meta:
        billing_mode = "PAY_PER_REQUEST"
        # All attributes are projected
        projection = AllProjection()
        index_name = "name-index"

    partition_key = UnicodeAttribute(hash_key=True)
    name = UnicodeAttribute(range_key=True)


class UpdatedAtIndex(LocalSecondaryIndex):
    """
    This class represents a local secondary index
    """

    class Meta:
        billing_mode = "PAY_PER_REQUEST"
        # All attributes are projected
        projection = AllProjection()
        index_name = "updated_at-index"

    partition_key = UnicodeAttribute(hash_key=True)
    updated_at = UnicodeAttribute(range_key=True)


class MCPFunctionCallModel(BaseModel):
    class Meta(BaseModel.Meta):
        table_name = "mcp-function_calls"

    partition_key = UnicodeAttribute(hash_key=True)
    mcp_function_call_uuid = UnicodeAttribute(range_key=True)
    name = UnicodeAttribute()
    mcp_type = UnicodeAttribute()
    arguments = MapAttribute()
    content_in_s3 = BooleanAttribute(default=False)
    content = UnicodeAttribute(null=True)
    status = UnicodeAttribute(default="initial")
    notes = UnicodeAttribute(null=True)
    time_spent = NumberAttribute(null=True)
    updated_by = UnicodeAttribute()
    created_at = UTCDateTimeAttribute()
    updated_at = UTCDateTimeAttribute()
    mcp_type_index = MCPTypeIndex()
    name_index = NameIndex()
    updated_at_index = UpdatedAtIndex()


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
                    entity_keys["mcp_function_call_uuid"] = getattr(
                        entity, "mcp_function_call_uuid", None
                    )

                # Fallback to kwargs (for creates/deletes)
                if not entity_keys.get("mcp_function_call_uuid"):
                    entity_keys["mcp_function_call_uuid"] = kwargs.get(
                        "mcp_function_call_uuid"
                    )

                # Only purge if we have the required keys
                if entity_keys.get("mcp_function_call_uuid") and partition_key:
                    purge_entity_cascading_cache(
                        args[0].context.get("logger"),
                        entity_type="mcp_function_call",
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
    cache_name=Config.get_cache_name("models", "mcp_function_call"),
    cache_enabled=Config.is_cache_enabled,
)
def get_mcp_function_call(
    partition_key: str, mcp_function_call_uuid: str
) -> MCPFunctionCallModel:
    return MCPFunctionCallModel.get(partition_key, mcp_function_call_uuid)


def get_mcp_function_call_count(partition_key: str, mcp_function_call_uuid: str) -> int:
    return MCPFunctionCallModel.count(
        partition_key,
        MCPFunctionCallModel.mcp_function_call_uuid == mcp_function_call_uuid,
    )


def get_mcp_function_call_type(
    info: ResolveInfo, mcp_function_call_model: MCPFunctionCallModel
) -> MCPFunctionCallType:
    try:
        if mcp_function_call_model.content_in_s3:
            from ..handlers.config import Config

            s3_key = (
                f"mcp_content/{mcp_function_call_model.mcp_function_call_uuid}.json"
            )
            try:
                response = Config.aws_s3.get_object(
                    Bucket=Config.funct_bucket_name, Key=s3_key
                )
                content = response["Body"].read().decode("utf-8")
            except Exception as e:
                raise e
    except Exception as e:
        log = traceback.format_exc()
        info.context.get("logger").exception(log)
        raise e
    mcp_function_call: Dict[str, Any] = mcp_function_call_model.__dict__[
        "attribute_values"
    ]
    content_in_s3 = mcp_function_call.pop("content_in_s3")
    if content_in_s3:
        mcp_function_call["content"] = content
    return MCPFunctionCallType(**Serializer.json_normalize(mcp_function_call))


def resolve_mcp_function_call(
    info: ResolveInfo, **kwargs: Dict[str, Any]
) -> MCPFunctionCallType | None:
    count = get_mcp_function_call_count(
        info.context["partition_key"], kwargs["mcp_function_call_uuid"]
    )
    if count == 0:
        return None

    return get_mcp_function_call_type(
        info,
        get_mcp_function_call(
            info.context["partition_key"], kwargs["mcp_function_call_uuid"]
        ),
    )


@monitor_decorator
@resolve_list_decorator(
    attributes_to_get=["partition_key", "mcp_function_call_uuid", "name", "updated_at"],
    list_type_class=MCPFunctionCallListType,
    type_funct=get_mcp_function_call_type,
    scan_index_forward=False,
)
def resolve_mcp_function_call_list(info: ResolveInfo, **kwargs: Dict[str, Any]) -> Any:
    partition_key = info.context["partition_key"]
    mcp_type = kwargs.get("mcp_type")
    name = kwargs.get("name")
    status = kwargs.get("status")
    updated_at_gt = kwargs.get("updated_at_gt")
    updated_at_lt = kwargs.get("updated_at_lt")

    args = []
    inquiry_funct = MCPFunctionCallModel.scan
    count_funct = MCPFunctionCallModel.count
    range_key_condition = None
    if partition_key:
        if updated_at_gt is not None and updated_at_lt is not None:
            range_key_condition = MCPFunctionCallModel.updated_at.between(
                updated_at_gt, updated_at_lt
            )
        elif updated_at_gt is not None:
            range_key_condition = MCPFunctionCallModel.updated_at > updated_at_gt
        elif updated_at_lt is not None:
            range_key_condition = MCPFunctionCallModel.updated_at < updated_at_lt

        args = [partition_key, range_key_condition]
        inquiry_funct = MCPFunctionCallModel.updated_at_index.query
        count_funct = MCPFunctionCallModel.updated_at_index.count

        if mcp_type and range_key_condition is None:
            inquiry_funct = MCPFunctionCallModel.mcp_type_index.query
            args[1] = MCPFunctionCallModel.mcp_type == mcp_type
            count_funct = MCPFunctionCallModel.mcp_type_index.count
        elif name and range_key_condition is None:
            inquiry_funct = MCPFunctionCallModel.name_index.query
            args[1] = MCPFunctionCallModel.name == name
            count_funct = MCPFunctionCallModel.name_index.count

    the_filters = None
    if mcp_type and range_key_condition is not None:
        the_filters &= MCPFunctionCallModel.mcp_type == mcp_type
    if name and range_key_condition is not None:
        the_filters &= MCPFunctionCallModel.name == name
    if status:
        the_filters &= MCPFunctionCallModel.status == status
    if the_filters is not None:
        args.append(the_filters)

    return inquiry_funct, count_funct, args


def _save_content_to_s3(content: str, bucket_name: str, key: str) -> None:
    """Save content to S3 bucket."""
    try:
        Config.aws_s3.put_object(Bucket=bucket_name, Key=key, Body=content)
        Config.logger.info(f"Content saved to S3: s3://{bucket_name}/{key}")
    except Exception as e:
        Config.logger.error(f"Failed to save content to S3: {e}")
        raise


@insert_update_decorator(
    keys={
        "hash_key": "partition_key",
        "range_key": "mcp_function_call_uuid",
    },
    model_funct=get_mcp_function_call,
    count_funct=get_mcp_function_call_count,
    type_funct=get_mcp_function_call_type,
)
@purge_cache()
def insert_update_mcp_function_call(
    info: ResolveInfo, **kwargs: Dict[str, Any]
) -> None:

    partition_key = kwargs.get("partition_key")
    mcp_function_call_uuid = kwargs.get("mcp_function_call_uuid", str(uuid.uuid4()))

    if kwargs.get("entity") is None:
        cols = {
            "name": kwargs["name"],
            "mcp_type": kwargs["mcp_type"],
            "arguments": kwargs.get("arguments", {}),
            "updated_by": kwargs["updated_by"],
            "created_at": pendulum.now("UTC"),
            "updated_at": pendulum.now("UTC"),
        }
        for key in [
            "content_in_s3",
            "content",
            "status",
            "notes",
            "time_spent",
        ]:
            if key in kwargs:
                cols[key] = kwargs[key]

        try:
            MCPFunctionCallModel(
                partition_key,
                mcp_function_call_uuid,
                **cols,
            ).save()
        except Exception as e:
            # Check if exception is due to DynamoDB item size limit (400KB)
            if "Item size has exceeded the maximum allowed size" in str(
                e
            ) or "ValidationException" in str(type(e).__name__):
                Config.logger.warning(
                    f"DynamoDB maximum item size (400KB) exceeded for {mcp_function_call_uuid}. "
                    f"Offloading content to S3. Error: {str(e)}"
                )

                s3_key = f"mcp_content/{kwargs['mcp_function_call_uuid']}.json"
                _save_content_to_s3(
                    Serializer.json_dumps(cols.get("content")),
                    Config.funct_bucket_name,
                    s3_key,
                )
                cols.pop("content")
                cols["content_in_s3"] = True

                MCPFunctionCallModel(
                    partition_key,
                    mcp_function_call_uuid,
                    **cols,
                ).save()

            else:
                # Re-raise if it's not an item size exception
                raise

        return

    mcp_function_call = kwargs.get("entity")
    actions = [
        MCPFunctionCallModel.updated_by.set(kwargs["updated_by"]),
        MCPFunctionCallModel.updated_at.set(pendulum.now("UTC")),
    ]

    field_map = {
        "name": MCPFunctionCallModel.name,
        "mcp_type": MCPFunctionCallModel.mcp_type,
        "arguments": MCPFunctionCallModel.arguments,
        "content_in_s3": MCPFunctionCallModel.content_in_s3,
        "content": MCPFunctionCallModel.content,
        "status": MCPFunctionCallModel.status,
        "notes": MCPFunctionCallModel.notes,
        "time_spent": MCPFunctionCallModel.time_spent,
    }

    for key, field in field_map.items():
        if key in kwargs:
            actions.append(field.set(kwargs[key]))

    try:
        mcp_function_call.update(actions=actions)
    except Exception as e:
        # Check if exception is due to DynamoDB item size limit (400KB)
        if "Item size has exceeded the maximum allowed size" in str(
            e
        ) or "ValidationException" in str(type(e).__name__):
            Config.logger.warning(
                f"DynamoDB maximum item size (400KB) exceeded for {mcp_function_call_uuid}. "
                f"Offloading content to S3. Error: {str(e)}"
            )

            s3_key = f"mcp_content/{kwargs['mcp_function_call_uuid']}.json"
            _save_content_to_s3(
                Serializer.json_dumps(kwargs.get("content")),
                Config.funct_bucket_name,
                s3_key,
            )

            for key, field in field_map.items():
                if key in kwargs:
                    if key == "content":
                        actions.append(field.set(None))
                        actions.append(MCPFunctionCallModel.content_in_s3.set(True))
                    else:
                        actions.append(field.set(kwargs[key]))
        else:
            raise
    return


@delete_decorator(
    keys={
        "hash_key": "partition_key",
        "range_key": "mcp_function_call_uuid",
    },
    model_funct=get_mcp_function_call,
)
@purge_cache()
def delete_mcp_function_call(info: ResolveInfo, **kwargs: Dict[str, Any]) -> bool:

    kwargs["entity"].delete()
    return True
