#!/usr/bin/python
# -*- coding: utf-8 -*-
from __future__ import print_function

__author__ = "bibow"

import traceback
from typing import Any, Dict

from graphene import ResolveInfo


def load_mcp_configuration_into_models(
    info: ResolveInfo, **kwargs: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Load MCP configuration JSON into database models.
    This is the reverse of Config.fetch_mcp_configuration().

    Args:
        info (ResolveInfo): GraphQL ResolveInfo object for database operations
        kwargs (Dict[str, Any]): Dictionary containing:
            - mcp_configuration: Complete MCP configuration dictionary
            - updated_by: User identifier for update tracking
            - partition_key: Endpoint ID to load configuration for (from info.context)

    Returns:
        Dict[str, Any]: Dictionary with statistics about loaded items containing:
            - tools: Number of tools loaded
            - resources: Number of resources loaded
            - prompts: Number of prompts loaded
            - modules: Number of modules loaded
            - settings: Number of settings loaded

    Raises:
        Exception: If loading fails
    """
    try:
        from ..models.mcp_function import insert_update_mcp_function
        from ..models.mcp_module import insert_update_mcp_module
        from ..models.mcp_setting import insert_update_mcp_setting
        from .mcp_utility import get_mcp_configuration_by_module

        partition_key = info.context["partition_key"]
        info.context["logger"].info(
            f"Loading MCP configuration for endpoint: {partition_key}"
        )
        updated_by = kwargs["updated_by"]

        mcp_configuration = None
        if "module_name" in kwargs:
            mcp_configuration = get_mcp_configuration_by_module(
                kwargs.get("package_name"),
                kwargs["module_name"],
                source=kwargs.get("source"),
            )
            info.context["logger"].info(
                f"Loading MCP configuration for package: {kwargs.get('package_name', '')}, module: {kwargs['module_name']}"
            )
        else:
            if "mcp_configuration" in kwargs:
                mcp_configuration = kwargs["mcp_configuration"]
            else:
                raise Exception("No MCP configuration provided")

        stats = {"tools": 0, "resources": 0, "prompts": 0, "modules": 0, "settings": 0}

        # Load tools
        if "tools" in mcp_configuration:
            info.context["logger"].info(
                f"Loading {len(mcp_configuration['tools'])} tools"
            )
            for tool in mcp_configuration["tools"]:
                tool_data = {
                    "partition_key": partition_key,
                    "name": tool.get("name"),
                    "mcp_type": "tool",
                    "description": tool.get("description"),
                    "data": {
                        k: v
                        for k, v in tool.items()
                        if k not in ["name", "description", "annotations", "is_async"]
                    },
                    "annotations": tool.get("annotations"),
                    "is_async": tool.get("is_async", False),
                    "updated_by": updated_by,
                }
                info.context["logger"].info(
                    f"Loading tool '{tool.get('name')}' with data: {tool_data['data']}"
                )
                insert_update_mcp_function(info, **tool_data)
                stats["tools"] += 1

        # Load resources
        if "resources" in mcp_configuration:
            info.context["logger"].info(
                f"Loading {len(mcp_configuration['resources'])} resources"
            )
            for resource in mcp_configuration["resources"]:
                resource_data = {
                    "partition_key": partition_key,
                    "name": resource.get("name"),
                    "mcp_type": "resource",
                    "description": resource.get("description"),
                    "data": {
                        k: v
                        for k, v in resource.items()
                        if k not in ["name", "description", "annotations", "is_async"]
                    },
                    "annotations": resource.get("annotations"),
                    "is_async": resource.get("is_async", False),
                    "updated_by": updated_by,
                }
                insert_update_mcp_function(info, **resource_data)
                stats["resources"] += 1

        # Load prompts
        if "prompts" in mcp_configuration:
            info.context["logger"].info(
                f"Loading {len(mcp_configuration['prompts'])} prompts"
            )
            for prompt in mcp_configuration["prompts"]:
                prompt_data = {
                    "partition_key": partition_key,
                    "name": prompt.get("name"),
                    "mcp_type": "prompt",
                    "description": prompt.get("description"),
                    "data": {
                        k: v
                        for k, v in prompt.items()
                        if k not in ["name", "description", "annotations", "is_async"]
                    },
                    "annotations": prompt.get("annotations"),
                    "is_async": prompt.get("is_async", False),
                    "updated_by": updated_by,
                }
                insert_update_mcp_function(info, **prompt_data)
                stats["prompts"] += 1

        # Load module links as functions with module information
        if "module_links" in mcp_configuration:
            info.context["logger"].info(
                f"Loading {len(mcp_configuration['module_links'])} module links"
            )
            for link in mcp_configuration["module_links"]:
                # Only update the module-related fields, don't overwrite existing data
                link_data = {
                    "partition_key": partition_key,
                    "name": link.get("name"),
                    "mcp_type": link.get("type", "tool"),
                    "module_name": link.get("module_name"),
                    "class_name": link.get("class_name"),
                    "function_name": link.get("function_name"),
                    "return_type": link.get("return_type", "text"),
                    "is_async": link.get("is_async", False),
                    "updated_by": updated_by,
                    # Don't include 'data' field to avoid overwriting existing data
                }
                insert_update_mcp_function(info, **link_data)

        # Load modules
        if "modules" in mcp_configuration:
            info.context["logger"].info(
                f"Loading {len(mcp_configuration['modules'])} modules"
            )

            # Aggregate all settings from all modules and create one shared setting
            setting_insert_data = {
                "partition_key": partition_key,
                "setting": {},
                "updated_by": updated_by,
            }

            # Aggregate all module settings
            for module in mcp_configuration["modules"]:
                setting_insert_data["setting"] = dict(
                    setting_insert_data["setting"], **module.get("setting", {})
                )

            # Apply Config.setting overrides (after aggregating all module settings)
            from .config import Config

            for k, v in Config.setting.items():
                if k in setting_insert_data["setting"].keys():
                    setting_insert_data["setting"][k] = v

            # Apply variables overrides (highest priority)
            if "variables" in kwargs:
                for k, v in kwargs["variables"].items():
                    if k in setting_insert_data["setting"].keys():
                        setting_insert_data["setting"][k] = v

            # Create the shared setting and get the setting_id from the returned object
            mcp_setting = insert_update_mcp_setting(info, **setting_insert_data)
            setting_id = mcp_setting.setting_id
            stats["settings"] += 1

            for module in mcp_configuration["modules"]:
                # Create module with class information
                classes = [
                    {"class_name": module.get("class_name"), "setting_id": setting_id}
                ]

                module_data = {
                    "partition_key": partition_key,
                    "module_name": module.get("module_name"),
                    "package_name": module.get(
                        "package_name", module.get("module_name")
                    ),
                    "classes": classes,
                    "source": module.get("source", ""),
                    "updated_by": updated_by,
                }
                insert_update_mcp_module(info, **module_data)
                stats["modules"] += 1

        info.context["logger"].info(f"Successfully loaded MCP configuration: {stats}")
        return stats

    except Exception as e:
        log = traceback.format_exc()
        info.context["logger"].error(f"Failed to load MCP configuration: {log}")
        raise e
