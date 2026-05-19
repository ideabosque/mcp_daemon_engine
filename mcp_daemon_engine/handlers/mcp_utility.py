#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

import asyncio
import concurrent.futures
import functools
import os
import sys
import threading
import time
import traceback
import zipfile
from typing import Any, Dict, Optional, Sequence

import pendulum
from mcp.types import (
    EmbeddedResource,
    GetPromptResult,
    ImageContent,
    PromptMessage,
    ReadResourceResult,
    TextContent,
    TextResourceContents,
)

from silvaengine_utility import Invoker, Serializer

from .config import Config

# Global registry to track active background threads
_active_threads = []


def wait_for_background_threads(timeout=30):
    """Wait for all background threads to complete before shutdown."""
    if not _active_threads:
        return

    Config.logger.info(
        f"Waiting for {len(_active_threads)} background threads to complete..."
    )

    for thread in _active_threads[
        :
    ]:  # Copy list to avoid modification during iteration
        if thread.is_alive():
            Config.logger.info(
                f"Waiting for thread {thread.name if hasattr(thread, 'name') else 'unnamed'}..."
            )
            thread.join(timeout=timeout)
            if thread.is_alive():
                Config.logger.warning(
                    f"Thread {thread.name if hasattr(thread, 'name') else 'unnamed'} did not complete within {timeout}s"
                )

    _active_threads.clear()
    Config.logger.info("Background thread cleanup completed")


INSERT_UPDATE_MCP_FUNCTION_CALL = """mutation insertUpdateMcpFunctionCall(
    $arguments: JSONCamelCase,
    $contentInS3: Boolean,
    $content: String,
    $mcpFunctionCallUuid: String,
    $mcpType: String,
    $name: String,
    $notes: String,
    $status: String,
    $timeSpent: Int,
    $updatedBy: String!
) {
    insertUpdateMcpFunctionCall(
        arguments: $arguments,
        contentInS3: $contentInS3,
        content: $content,
        mcpFunctionCallUuid: $mcpFunctionCallUuid,
        mcpType: $mcpType,
        name: $name,
        notes: $notes,
        status: $status,
        timeSpent: $timeSpent,
        updatedBy: $updatedBy
    ) {
        mcpFunctionCall {
            partitionKey
            mcpFunctionCallUuid
            mcpType
            name
            arguments
            content
            status
            notes
            timeSpent
            updatedBy
            createdAt
            updatedAt
        }
    }
}"""


MCP_FUNCTION_CALL = """query mcpFunctionCall($mcpFunctionCallUuid: String!) {
    mcpFunctionCall(mcpFunctionCallUuid: $mcpFunctionCallUuid) {
        partitionKey
        mcpFunctionCallUuid
        mcpType
        name
        arguments
        content
        status
        notes
        timeSpent
        updatedBy
        createdAt
        updatedAt
    }
}"""


def _check_existing_function_call(
    partition_key: str,
    mcp_function_call_uuid: str,
) -> Dict[str, Any]:
    response = Config.mcp_core.mcp_core_graphql(
        **{
            "context": {
                "partition_key": partition_key,
            },
            "query": MCP_FUNCTION_CALL,
            "variables": {
                "mcpFunctionCallUuid": mcp_function_call_uuid,
            },
        }
    )
    response = Serializer.json_loads(response.get("body", response))

    if "errors" in response:
        Config.logger.error(f"GraphQL error: {response['errors']}")
        raise Exception(response["errors"])
    elif "data" in response:
        response = response.get("data", {})

    mcp_function_call = response["mcpFunctionCall"]

    return mcp_function_call


def _insert_update_mcp_function_call(
    partition_key: str, **kwargs: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Private helper function to insert/update MCP function call record
    """
    if kwargs.get("mcp_function_call_uuid"):
        Config.logger.info("Updating existing MCP function call")

        response = Config.mcp_core.mcp_core_graphql(
            **{
                "context": {
                    "partition_key": partition_key,
                },
                "query": INSERT_UPDATE_MCP_FUNCTION_CALL,
                "variables": {
                    "mcpFunctionCallUuid": kwargs["mcp_function_call_uuid"],
                    "content": kwargs.get("content"),
                    "status": kwargs["status"],
                    "timeSpent": kwargs.get("time_spent", None),
                    "notes": kwargs.get("notes", None),
                    "updatedBy": "mcp_daemon_engine",
                },
            }
        )
    else:
        Config.logger.info("Making GraphQL call to insert/update MCP function")
        response = Config.mcp_core.mcp_core_graphql(
            **{
                "context": {
                    "partition_key": partition_key,
                },
                "query": INSERT_UPDATE_MCP_FUNCTION_CALL,
                "variables": {
                    "name": kwargs["name"],
                    "mcpType": kwargs["mcp_type"],
                    "arguments": Serializer.json_normalize(
                        kwargs["arguments"], parser_number=False
                    ),
                    "updatedBy": "mcp_daemon_engine",
                },
            }
        )

    response = Serializer.json_loads(response.get("body", response))

    if "errors" in response:
        Config.logger.error(f"GraphQL error: {response['errors']}")
        raise Exception(response["errors"])
    elif "data" in response:
        response = response.get("data", {})

    mcp_function_call = response["insertUpdateMcpFunctionCall"]["mcpFunctionCall"]

    return mcp_function_call


def execute_decorator():
    def actual_decorator(original_function):
        @functools.wraps(original_function)
        def wrapper_function(*args, **kwargs):
            try:
                Config.logger.info("Starting execution of MCP function")
                mcp_function_call = None
                start_time = pendulum.now("UTC")
                partition_key = args[0]

                if kwargs.get("mcp_function_call_uuid"):
                    mcp_function_call = _check_existing_function_call(
                        partition_key, kwargs["mcp_function_call_uuid"]
                    )

                if partition_key != "default" and mcp_function_call is None:
                    Config.logger.info(f"Processing partition_key: {partition_key}")
                    mcp_type = original_function.__name__.replace(
                        "execute_", ""
                    ).replace("_function", "")
                    Config.logger.info(f"MCP type determined: {mcp_type}")

                    if mcp_type == "resource":
                        Config.logger.info("Processing resource type MCP")
                        resource = next(
                            (
                                resource
                                for resource in Config.fetch_mcp_configuration(
                                    partition_key
                                )["resources"]
                                if resource["uri"] == args[1]
                            ),
                            None,
                        )

                        if resource is None:
                            raise Exception(f"Resource not found for URI: {args[1]}")

                        name = resource["name"]
                        arguments = {"uri": args[1]}
                        Config.logger.info(
                            f"Resource name: {name}, arguments: {arguments}"
                        )
                    else:
                        name = args[1]
                        arguments = args[2]
                        Config.logger.info(
                            f"Function name: {name}, arguments: {arguments}"
                        )

                    mcp_function_call = _insert_update_mcp_function_call(
                        partition_key,
                        **{"name": name, "mcp_type": mcp_type, "arguments": arguments},
                    )

                Config.logger.info("Executing original function")
                result = original_function(*args, **kwargs)

                content = None
                if isinstance(result, list):
                    content = []
                    for item in result:
                        if isinstance(item, EmbeddedResource):
                            content.append(
                                item.model_dump(mode="json", exclude_none=True)
                            )
                        elif isinstance(item, TextContent):
                            content.append(
                                item.model_dump(mode="json", exclude_none=True)
                            )
                        elif isinstance(item, ImageContent):
                            content.append(
                                item.model_dump(mode="json", exclude_none=True)
                            )
                        else:
                            content.append(item)
                elif isinstance(result, (ReadResourceResult, GetPromptResult)):
                    # Handle MCP structured result types
                    content = result.model_dump(mode="json", exclude_none=True)
                else:
                    # Handle other types (strings, dicts, etc.)
                    content = result

                if mcp_function_call is not None:
                    time_spent = int(
                        pendulum.now("UTC").diff(start_time).in_seconds() * 1000
                    )
                    Config.logger.info(f"Function execution time: {time_spent}ms")

                    Config.logger.info("Updating MCP function call with results")
                    _insert_update_mcp_function_call(
                        partition_key,
                        **{
                            "mcp_function_call_uuid": mcp_function_call[
                                "mcpFunctionCallUuid"
                            ],
                            "content": content[0]["text"],
                            "status": "completed",
                            "time_spent": time_spent,
                            "updatedBy": "mcp_daemon_engine",
                        },
                    )

                Config.logger.info("Successfully completed MCP function execution")
                return result

            except Exception as e:
                log = traceback.format_exc()
                Config.logger.error(f"Error in MCP function execution: {log}")
                if mcp_function_call is not None:
                    Config.logger.info("Updating MCP function call with error status")
                    _insert_update_mcp_function_call(
                        mcp_function_call["partitionKey"],
                        **{
                            "mcp_function_call_uuid": mcp_function_call[
                                "mcpFunctionCallUuid"
                            ],
                            "notes": log,
                            "status": "failed",
                            "updatedBy": "mcp_daemon_engine",
                        },
                    )
                raise e

        return wrapper_function

    return actual_decorator


def get_mcp_configuration_with_retry(
    partition_key: str,
    max_retries: int = 1,
) -> Dict[str, Any] | Any:
    """
    Get MCP configuration with automatic retry on failure.

    Args:
        partition_key: Endpoint ID to fetch configuration for
        max_retries: Maximum number of retry attempts with cache refresh

    Returns:
        MCP configuration dictionary

    Raises:
        Exception: If configuration cannot be retrieved after retries
    """
    for attempt in range(max_retries + 1):
        try:
            force_refresh = attempt > 0  # Force refresh on retry attempts

            return Config.fetch_mcp_configuration(
                partition_key, force_refresh=force_refresh
            )
        except Exception as e:
            if attempt < max_retries:
                Config.logger.warning(
                    f"Failed to fetch MCP config for {partition_key} (attempt {attempt + 1}), "
                    f"retrying with cache refresh: {e}"
                )
                # Clear cache before retry
                Config.clear_mcp_configuration_cache(partition_key)
                continue
            else:
                Config.logger.error(
                    f"Failed to fetch MCP config for {partition_key} after {max_retries + 1} attempts: {e}"
                )
                raise


def _module_exists(module_name: str) -> bool:
    """Check if the module exists in the specified path."""
    module_dir = os.path.join(Config.funct_extract_path, module_name)
    if os.path.exists(module_dir) and os.path.isdir(module_dir):
        Config.logger.info(
            f"Module {module_name} found in {Config.funct_extract_path}."
        )
        return True
    Config.logger.info(
        f"Module {module_name} not found in {Config.funct_extract_path}."
    )
    return False


def _download_and_extract_package(package_name: str) -> None:
    """Download and extract the module from S3 if not already extracted."""
    key = f"{package_name}.zip"
    zip_path = f"{Config.funct_zip_path}/{key}"

    Config.logger.info(
        f"Downloading module from S3: bucket={Config.funct_bucket_name}, key={key}"
    )
    Config.aws_s3.download_file(Config.funct_bucket_name, key, zip_path)
    Config.logger.info(f"Downloaded {key} from S3 to {zip_path}")

    # Extract the ZIP file
    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        zip_ref.extractall(Config.funct_extract_path)
    Config.logger.info(f"Extracted module to {Config.funct_extract_path}")


def _get_module(package_name: str, module_name: str, source: str = None) -> type:
    try:
        """Get the module class from the package."""
        if source is None:
            return getattr(__import__(module_name), module_name)

        # Check if the module exists
        if not _module_exists(module_name):
            # Download and extract the module if it doesn't exist
            _download_and_extract_package(package_name)

        # Add the extracted module to sys.path
        module_path = f"{Config.funct_extract_path}"
        if module_path not in sys.path:
            sys.path.append(module_path)

        # Import the module and get the class
        module = __import__(module_name)
        return module
    except Exception as e:
        log = traceback.format_exc()
        Config.logger.error(log)
        raise e


def _get_class(
    package_name: str, module_name: str, class_name: str, source: str = None
) -> Optional[type]:
    try:
        # Import the module and get the class
        module = _get_module(package_name, module_name, source=source)
        return getattr(module, class_name)
    except Exception as e:
        log = traceback.format_exc()
        Config.logger.error(log)
        raise e


def _validate_nested_structure(
    schema: Dict[str, Any], data: Dict[str, Any], field_path: str = ""
) -> None:
    """
    Private function to recursively validate required fields in nested objects and arrays.

    Args:
        schema: JSONCamelCase schema definition for the data structure
        data: The actual data to validate
        field_path: Current field path for error reporting
    """
    import copy

    if schema.get("type") == "object" and "properties" in schema:
        # Handle object validation
        nested_required = schema.get("required", [])
        nested_properties = schema["properties"]

        for nested_key, nested_schema in nested_properties.items():
            nested_path = f"{field_path}.{nested_key}" if field_path else nested_key

            if nested_key not in data:
                if "default" in nested_schema:
                    default_value = nested_schema["default"]
                    if isinstance(default_value, (dict, list)):
                        data[nested_key] = copy.deepcopy(default_value)
                    else:
                        data[nested_key] = default_value
                elif nested_key in nested_required:
                    raise Exception(f"Missing required argument: {nested_path}")
            else:
                # Recursively validate nested structures
                _validate_nested_structure(nested_schema, data[nested_key], nested_path)

    elif schema.get("type") == "array" and "items" in schema:
        # Handle array validation
        items_schema = schema["items"]
        if isinstance(data, list):
            for i, item in enumerate(data):
                item_path = f"{field_path}[{i}]" if field_path else f"[{i}]"
                _validate_nested_structure(items_schema, item, item_path)


def _validate_and_set_defaults(
    tool_schema: Dict[str, Any], arguments: Dict[str, Any]
) -> None:
    """
    Private function to validate arguments and set default values based on tool schema.
    Handles nested objects and arrays with required field validation.
    """
    import copy

    if not tool_schema.get("inputSchema", {}).get("properties"):
        return

    schema_properties = tool_schema["inputSchema"]["properties"]
    required_fields = tool_schema["inputSchema"].get("required", [])

    # Handle top-level properties
    for key, schema in schema_properties.items():
        if key not in arguments:
            if "default" in schema:
                default_value = schema["default"]
                if isinstance(default_value, (dict, list)):
                    arguments[key] = copy.deepcopy(default_value)
                else:
                    arguments[key] = default_value
            elif key in required_fields:
                raise Exception(f"Missing required argument: {key}")
        else:
            # Validate provided arguments
            _validate_nested_structure(schema, arguments[key], key)


@execute_decorator()
def execute_tool_function(
    partition_key: str,
    name: str,
    arguments: Dict[str, Any],
    mcp_function_call_uuid: str = None,
) -> Sequence[TextContent | ImageContent | EmbeddedResource]:
    try:
        config = get_mcp_configuration_with_retry(partition_key)
        tool = next(
            (tool for tool in config["tools"] if tool["name"] == name),
            {},
        )

        if arguments is None:
            arguments = {}

        # Validate arguments and set defaults using the tool schema
        _validate_and_set_defaults(tool, arguments)

        module_link = next(
            (
                module_link
                for module_link in config["module_links"]
                if module_link["name"] == name and module_link["type"] == "tool"
            ),
            {},
        )
        module = next(
            (
                module
                for module in config["modules"]
                if (
                    module["module_name"] == module_link["module_name"]
                    and module["class_name"] == module_link["class_name"]
                )
            ),
            {},
        )
        tool_class = _get_class(
            module["package_name"],
            module["module_name"],
            module["class_name"],
            source=module.get("source"),
        )

        if tool_class is None:
            raise Exception(f"Failed to load tool class: {module['class_name']}")

        tool_obj = tool_class(
            Config.logger, **Serializer.json_normalize(module["setting"])
        )

        if hasattr(tool_obj, "endpoint_id") and hasattr(tool_obj, "part_id"):
            if "#" in partition_key:
                keys = partition_key.split("#")
                tool_obj.endpoint_id = keys[0]
                tool_obj.part_id = keys[1]
            else:
                tool_obj.endpoint_id = partition_key
                tool_obj.part_id = None

        tool_function = getattr(tool_obj, module_link["function_name"])

        if module_link.get("is_async", False):
            if Config.aws_lambda:
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(asyncio.run, tool_function(**arguments))
                    result = future.result()
            else:
                result = asyncio.run(tool_function(**arguments))
        else:
            result = tool_function(**arguments)

        return_type = module_link["return_type"]

        if return_type == "text":
            # Handle dict result by converting to JSON representation
            if isinstance(result, dict):
                return [TextContent(type="text", text=Serializer.json_dumps(result))]
            return [TextContent(type="text", text=str(result))]

        elif return_type == "image":
            # Handle image results
            if isinstance(result, dict):
                # Expected format: {"data": "base64_data", "mimeType": "image/png"}
                return [
                    ImageContent(
                        type="image",
                        data=result.get("data", ""),
                        mimeType=result.get("mimeType", "image/png"),
                    )
                ]
            elif isinstance(result, str):
                # Assume base64 encoded PNG if just string
                return [ImageContent(type="image", data=result, mimeType="image/png")]
            else:
                raise Exception(f"Invalid image result format: {type(result)}")

        elif return_type == "embedded_resource":
            return _create_embedded_resource_from_result(result)

        else:
            raise Exception(
                f"Invalid return type {return_type}. Supported types: text, image, resource"
            )

    except Exception as e:
        log = traceback.format_exc()
        Config.logger.error(log)
        raise e


def get_mcp_configuration_by_module(
    package_name: str, module_name: str, source: str = None
) -> Dict[str, Any]:
    """Get MCP configuration by module."""
    try:
        module = _get_module(package_name, module_name, source=source)
        return getattr(module, "MCP_CONFIGURATION")

    except Exception as e:
        log = traceback.format_exc()
        Config.logger.error(log)
        raise e


def _create_embedded_resource_from_result(result) -> list[EmbeddedResource]:
    """Convert function result to EmbeddedResource with proper TextResourceContents."""
    # Extract resource data and determine content
    resource_data = (
        result.get("resource", result) if isinstance(result, dict) else result
    )

    if isinstance(resource_data, dict) and "text" in resource_data:
        # Use existing text content
        text_content = str(resource_data["text"])
        mime_type = resource_data.get("mimeType")

        # Auto-detect JSON if no mimeType provided
        if not mime_type:
            try:
                Serializer.json_loads(text_content)
                mime_type = "application/json"
            except:
                mime_type = "text/plain"
    else:
        # Convert to JSON string (for dicts) or plain string
        if isinstance(resource_data, dict):
            text_content = Serializer.json_dumps(resource_data)
            mime_type = resource_data.get("mimeType", "application/json")
        else:
            text_content = str(resource_data)
            mime_type = "text/plain"

    return [
        EmbeddedResource(
            type="resource",
            resource=TextResourceContents(
                text=text_content, mimeType=mime_type or "text/plain"
            ),
        )
    ]


@execute_decorator()
def execute_resource_function(
    partition_key: str,
    uri: str,
) -> ReadResourceResult:
    try:
        config = get_mcp_configuration_with_retry(partition_key)
        resource = next(
            (resource for resource in config["resources"] if resource["uri"] == uri),
            {},
        )

        module_link = next(
            (
                module_link
                for module_link in config["module_links"]
                if module_link["name"] == resource["name"]
                and module_link["type"] == "resource"
            ),
            {},
        )

        module = next(
            (
                module
                for module in config["modules"]
                if (
                    module["module_name"] == module_link["module_name"]
                    and module["class_name"] == module_link["class_name"]
                )
            ),
            {},
        )

        resource_class = _get_class(
            module["package_name"],
            module["module_name"],
            module["class_name"],
            source=module.get("source"),
        )

        if resource_class is None:
            raise Exception(f"Failed to load resource class: {module['class_name']}")

        resource_obj = resource_class(
            Config.logger,
            **Serializer.json_normalize(module["setting"]),
        )

        if hasattr(resource_obj, "endpoint_id") and hasattr(resource_obj, "part_id"):
            if "#" in partition_key:
                keys = partition_key.split("#")
                resource_obj.endpoint_id = keys[0]
                resource_obj.part_id = keys[1]
            else:
                resource_obj.endpoint_id = partition_key
                resource_obj.part_id = None

        resource_function = getattr(resource_obj, module_link["function_name"])

        result = resource_function(uri)

        # Return properly structured ReadResourceResult according to MCP specification
        return ReadResourceResult(
            contents=[
                TextResourceContents(uri=uri, mimeType="text/plain", text=str(result))
            ]
        )

    except Exception as e:
        log = traceback.format_exc()
        Config.logger.error(log)
        raise e


@execute_decorator()
def execute_prompt_function(
    partition_key: str,
    name: str,
    arguments: Dict[str, Any],
) -> GetPromptResult:
    try:
        config = get_mcp_configuration_with_retry(partition_key)
        prompt = next(
            (prompt for prompt in config["prompts"] if prompt["name"] == name),
            {},
        )

        # Check if arguments have all required arguments
        if prompt.get("arguments"):
            for arg in prompt["arguments"]:
                if arg.get("required", False) and arg["name"] not in arguments.keys():
                    raise Exception(f"Missing required argument {arg['name']}")

        module_link = next(
            (
                module_link
                for module_link in config["module_links"]
                if module_link["name"] == name and module_link["type"] == "prompt"
            ),
            {},
        )

        module = next(
            (
                module
                for module in config["modules"]
                if (
                    module["module_name"] == module_link["module_name"]
                    and module["class_name"] == module_link["class_name"]
                )
            ),
            {},
        )

        prompt_class = _get_class(
            module["package_name"],
            module["module_name"],
            module["class_name"],
            source=module.get("source"),
        )

        if prompt_class is None:
            raise Exception(f"Failed to load prompt class: {module['class_name']}")

        prompt_obj = prompt_class(
            Config.logger,
            **Serializer.json_normalize(module["setting"]),
        )

        if hasattr(prompt_obj, "endpoint_id") and hasattr(prompt_obj, "part_id"):
            if "#" in partition_key:
                keys = partition_key.split("#")
                prompt_obj.endpoint_id = keys[0]
                prompt_obj.part_id = keys[1]
            else:
                prompt_obj.endpoint_id = partition_key
                prompt_obj.part_id = None

        prompt_function = getattr(prompt_obj, module_link["function_name"])

        result = prompt_function(name, **arguments)

        return GetPromptResult(
            description=prompt["description"],
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(type="text", text=result),
                )
            ],
        )

    except Exception as e:
        log = traceback.format_exc()
        Config.logger.error(log)
        raise e


# TODO: Rebuild the function to support async execution with proper thread management and cleanup.
def async_execute_tool_function(
    partition_key: str,
    name: str,
    arguments: Dict[str, Any],
):
    if arguments.get("mcp_function_call_uuid"):
        mcp_function_call = _check_existing_function_call(
            partition_key, arguments["mcp_function_call_uuid"]
        )

        if mcp_function_call["status"] == "completed":
            Config.logger.info(
                f"Tool function {name} already completed. Skipping execution."
            )
            return [TextContent(type="text", text=mcp_function_call["content"])]
        else:
            return [
                EmbeddedResource(
                    type="resource",
                    resource=TextResourceContents(
                        uri=f"mcp://function-call/{mcp_function_call['mcpFunctionCallUuid']}",
                        text=Serializer.json_dumps(
                            {
                                "mcp_function_call_uuid": mcp_function_call[
                                    "mcpFunctionCallUuid"
                                ],
                                "status": mcp_function_call["status"],
                                "notes": mcp_function_call.get("notes"),
                            }
                        ),
                        mimeType="application/json",
                    ),
                )
            ]

    Config.logger.info("Making GraphQL call to insert/update MCP function")
    mcp_function_call = _insert_update_mcp_function_call(
        partition_key,
        **{"name": name, "mcp_type": "tool", "arguments": arguments},
    )
    Config.logger.info("Successfully created MCP function call")

    params = {
        "name": name,
        "arguments": arguments,
        "mcp_function_call_uuid": mcp_function_call["mcpFunctionCallUuid"],
    }

    if Config.aws_lambda:
        # Invoke Lambda function asynchronously
        Config.logger.info("Invoking Lambda function asynchronously")
        endpoint_id = (
            partition_key.split("#")[0] if "#" in partition_key else partition_key
        )
        part_id = partition_key.split("#")[1] if "#" in partition_key else None
        context = {
            "partition_key": partition_key,
            "logger": Config.logger,
            "endpoint_id": endpoint_id,
            "part_id": part_id,
            "setting": Config.setting,
        }
        Invoker.invoke_funct_on_aws_lambda(
            context,
            "async_execute_tool_function",
            params=params,
            execute_mode=Config.setting.get("execute_mode"),
            aws_lambda=Config.aws_lambda,
            invocation_type="Event",
        )
    else:
        Config.logger.info("Dispatching execute_tool_function in a separate thread")
        thread = threading.Thread(
            target=execute_tool_function,
            args=(
                partition_key,
                name,
                arguments,
            ),
            kwargs={"mcp_function_call_uuid": mcp_function_call["mcpFunctionCallUuid"]},
            daemon=False,  # Changed to False so thread won't be killed when main process exits
        )
        thread.start()

        # Register thread for tracking
        _active_threads.append(thread)
        Config.logger.info(
            f"Tool function {name} started in background thread (active threads: {len(_active_threads)})"
        )

        # Clean up completed threads
        _active_threads[:] = [t for t in _active_threads if t.is_alive()]

    # Poll for function completion with 60 second timeout
    # Checks the status of the function call periodically and returns the result when complete
    # If timeout is reached, breaks the loop and returns a resource reference instead
    start_time = time.time()
    while time.time() - start_time <= 3:
        mcp_function_call = _check_existing_function_call(
            partition_key, mcp_function_call["mcpFunctionCallUuid"]
        )
        if mcp_function_call["status"] == "completed":
            Config.logger.info(f"Tool function {name} completed. Returning result.")
            return [TextContent(type="text", text=mcp_function_call["content"])]
        elif mcp_function_call["status"] == "failed":
            Config.logger.info(f"Tool function {name} failed. Returning error message.")
            break
        else:
            # Update the status to "in_process" if the current status is "initial"
            if mcp_function_call["status"] == "initial":
                mcp_function_call = _insert_update_mcp_function_call(
                    partition_key,
                    **{
                        "mcp_function_call_uuid": mcp_function_call[
                            "mcpFunctionCallUuid"
                        ],
                        "status": "in_process",
                    },
                )

            Config.logger.info(
                f"Tool function {name} not completed yet. Waiting for result."
            )
            time.sleep(0.5)

    Config.logger.warning(f"Tool function {name} timed out after 3 seconds")

    return [
        EmbeddedResource(
            type="resource",
            resource=TextResourceContents(
                uri=f"mcp://function-call/{mcp_function_call['mcpFunctionCallUuid']}",
                text=Serializer.json_dumps(
                    {
                        "mcp_function_call_uuid": mcp_function_call[
                            "mcpFunctionCallUuid"
                        ],
                        "status": mcp_function_call["status"],
                        "notes": mcp_function_call.get("notes"),
                    }
                ),
                mimeType="application/json",
            ),
        )
    ]
